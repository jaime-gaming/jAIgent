"""Command line interface for jaigent.

Usage::

    jaigent "find the latest Python release and write it to notes.md"
    jaigent chat
    jaigent tools
    jaigent config
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from jaigent import __version__
from jaigent import session as sessions
from jaigent.agent import Agent, AgentResult
from jaigent.approval import Approver, Mode
from jaigent.branding import ACCENT, MUTED, PROMPT_MARK, render_banner, render_logo
from jaigent.config import (
    API_KEY_ENV_VARS,
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    KNOWN_PROVIDERS,
    Settings,
)
from jaigent.errors import ConfigurationError, JaigentError
from jaigent.llm import get_provider
from jaigent.pricing import estimate
from jaigent.tools import ToolRegistry, build_default_registry

console = Console()
err_console = Console(stderr=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jaigent",
        description="An AI agent that searches the web and works with local files.",
        epilog="Bring your own API key: export OPENAI_API_KEY=... (or ANTHROPIC_API_KEY=...)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"jaigent {__version__}")
    parser.add_argument("--logo", action="store_true", help="Print the jaigent logo and exit.")
    # Also accepted before a subcommand, so `jaigent --no-color --logo` works.
    parser.add_argument("--no-color", action="store_true", help="Disable colour and rich output.")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--provider", choices=KNOWN_PROVIDERS, help="LLM backend to use.")
    common.add_argument(
        "-m", "--model", help="Model id, e.g. gpt-4o-mini or claude-3-5-sonnet-latest."
    )
    common.add_argument("--api-key", help="API key (prefer an env var or .env file).")
    common.add_argument("--base-url", help="Override the API root for OpenAI-compatible gateways.")
    common.add_argument("-w", "--workspace", help="Directory the file tools are confined to.")
    common.add_argument("-s", "--max-steps", type=int, help="Maximum tool-calling steps per turn.")
    common.add_argument("-t", "--temperature", type=float, help="Sampling temperature.")
    common.add_argument(
        "--search-backend", choices=("duckduckgo", "tavily"), help="Web search backend."
    )
    common.add_argument(
        "--allow-shell",
        action="store_true",
        default=None,
        help="Enable the run_command tool. Dangerous: the model can execute shell commands.",
    )
    common.add_argument(
        "-v", "--verbose", action="store_true", default=None, help="Trace tool calls."
    )
    common.add_argument("--no-color", action="store_true", help="Disable rich formatting.")
    common.add_argument(
        "--no-stream",
        action="store_true",
        default=None,
        help="Wait for the full answer instead of printing it as it arrives.",
    )
    common.add_argument(
        "--no-cost",
        action="store_true",
        default=None,
        help="Hide the token and cost estimate shown after each run.",
    )
    common.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=None,
        help="Apply file changes without asking. Implied when output is not a terminal.",
    )
    common.add_argument(
        "--ask",
        action="store_true",
        default=None,
        help="Show a diff and confirm before every file change or command.",
    )
    common.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Never modify anything; the agent may only read and search.",
    )

    sub = parser.add_subparsers(dest="command")

    run_cmd = sub.add_parser("run", parents=[common], help="Run a single task and exit.")
    run_cmd.add_argument("prompt", nargs="+", help="The task to perform.")

    chat_cmd = sub.add_parser("chat", parents=[common], help="Start an interactive session.")
    chat_cmd.add_argument(
        "--resume",
        nargs="?",
        const="last",
        metavar="ID",
        help="Resume a saved session: the most recent one, or a specific id.",
    )
    chat_cmd.add_argument(
        "--no-save", action="store_true", help="Do not persist this conversation to disk."
    )

    sub.add_parser("tools", parents=[common], help="List the tools available to the agent.")
    sub.add_parser("config", parents=[common], help="Show the resolved configuration.")

    sessions_cmd = sub.add_parser("sessions", parents=[common], help="List saved sessions.")
    sessions_cmd.add_argument(
        "--delete", metavar="ID", help="Delete a saved session by id, or 'all'."
    )

    init_cmd = sub.add_parser(
        "init", parents=[common], help="Set up jaigent interactively and write a .env file."
    )
    init_cmd.add_argument(
        "--force", action="store_true", help="Overwrite an existing .env without asking."
    )
    return parser


#: Recognised subcommands, used to detect the bare-prompt shorthand.
COMMANDS = ("run", "chat", "tools", "config", "sessions", "init")


def normalise_argv(argv: list[str]) -> list[str]:
    """Let ``jaigent "do the thing"`` mean ``jaigent run "do the thing"``.

    A leading token that is neither a known subcommand nor an option is treated
    as the start of a prompt.
    """
    if not argv:
        return argv
    first = argv[0]
    if first in COMMANDS or first.startswith("-"):
        return argv
    return ["run", *argv]


def resolve_approval(args: argparse.Namespace, settings: Settings) -> str:
    """Work out the approval policy from the flags, the environment and the tty.

    Explicit flags win. Otherwise ``ask`` is used for interactive terminals and
    ``auto`` when output is piped, so scripts never hang on a prompt.
    """
    if getattr(args, "dry_run", None):
        return "dry-run"
    if getattr(args, "ask", None):
        return "ask"
    if getattr(args, "yes", None):
        return "auto"
    if os.getenv("JAIGENT_APPROVAL"):
        return settings.approval
    return "ask" if sys.stdin.isatty() and sys.stdout.isatty() else "auto"


def resolve_settings(args: argparse.Namespace) -> Settings:
    """Merge CLI flags over environment configuration."""
    settings = Settings.from_env()
    workspace = Path(args.workspace).expanduser() if getattr(args, "workspace", None) else None

    # store_true flags mean "turn off"; None means "not specified".
    stream = False if getattr(args, "no_stream", None) else None
    show_cost = False if getattr(args, "no_cost", None) else None

    settings = settings.merged_with(
        provider=getattr(args, "provider", None),
        model=getattr(args, "model", None),
        api_key=getattr(args, "api_key", None),
        base_url=getattr(args, "base_url", None),
        workspace=workspace,
        max_steps=getattr(args, "max_steps", None),
        temperature=getattr(args, "temperature", None),
        search_backend=getattr(args, "search_backend", None),
        allow_shell=getattr(args, "allow_shell", None),
        verbose=getattr(args, "verbose", None),
        stream=stream,
        show_cost=show_cost,
    )
    return settings.merged_with(approval=resolve_approval(args, settings))


def build_agent(settings: Settings, *, sink: Callable[[str], None] | None = None) -> Agent:
    """Construct an agent wired to the console for streaming and approvals."""
    approver = Approver(
        Mode(settings.approval),
        console=console,
        workspace=settings.workspace,
    )
    return Agent(settings, on_text=sink, approver=approver)


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
def run_turn(agent: Agent, settings: Settings, prompt: str, *, plain: bool) -> AgentResult:
    """Run one turn, streaming the answer when enabled, then print the footer.

    Streaming and rich markdown are mutually exclusive: you cannot re-render
    text you have already printed. So when streaming we print raw text as it
    arrives; otherwise we buffer and render markdown at the end.
    """
    streaming = settings.stream and not plain

    if streaming:
        printer = _StreamPrinter(console)
        agent.on_text = printer
        result = agent.run(prompt)
        printer.finish()
        # A turn that ended in tool calls may have streamed nothing; show the answer.
        if not printer.wrote and result.output:
            _print_answer(result.output, plain=plain)
    else:
        agent.on_text = None
        with console.status(f"[{ACCENT}]working…[/]", spinner="dots") as status:
            if settings.verbose:
                status.stop()
            result = agent.run(prompt)
        _print_answer(result.output, plain=plain)

    _print_footer(result, settings)
    return result


class _StreamPrinter:
    """Writes streamed chunks straight to the console, tracking whether anything came."""

    def __init__(self, target: Console) -> None:
        self.target = target
        self.wrote = False

    def __call__(self, chunk: str) -> None:
        if chunk:
            self.wrote = True
            self.target.file.write(chunk)
            self.target.file.flush()

    def finish(self) -> None:
        if self.wrote:
            self.target.file.write("\n")
            self.target.file.flush()


def cmd_run(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        err_console.print('[red]No prompt given.[/] Try: jaigent "summarise README.md"')
        return 2

    agent = build_agent(settings)
    run_turn(agent, settings, prompt, plain=bool(args.no_color))
    return 0


HELP_TEXT = """\
/help                 show this list
/reset                clear the conversation
/tools                list available tools
/model <name>         switch model for the rest of the session
/workspace <path>     point the file tools somewhere else
/cost                 show tokens and spend for this session
/save                 write the session to disk now
/undo                 drop the last exchange
/exit                 quit"""


def cmd_chat(args: argparse.Namespace) -> int:  # noqa: C901 - a REPL is a dispatch table
    settings = resolve_settings(args)

    session = None
    if getattr(args, "resume", None):
        session = sessions.resolve(args.resume)
        if session is None:
            err_console.print(
                f"[red]No session matching {args.resume!r}.[/] "
                "Run [cyan]jaigent sessions[/] to see what is saved."
            )
            return 1
        settings = settings.merged_with(model=session.model or None)

    agent = build_agent(settings)
    if session is not None:
        agent.load_history(session.messages)
    else:
        session = sessions.Session.new(
            provider=settings.provider,
            model=settings.model,
            workspace=str(settings.workspace),
        )

    saving = not getattr(args, "no_save", False)

    console.print(
        render_banner(
            console,
            version=__version__,
            subtitle=f"{settings.provider}/{settings.model} · {settings.workspace}",
        )
    )
    if args.resume:
        console.print(
            f"[{MUTED}]resumed {session.id} · {session.turns} turn(s) · "
            f"{session.title or 'untitled'}[/]",
            highlight=False,
        )
    console.print(f"[{MUTED}]/help for commands · /exit to quit[/]\n", highlight=False)

    while True:
        try:
            prompt = console.input(f"[bold {ACCENT}]{PROMPT_MARK}[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            _finish_chat(session, agent, saving)
            return 0

        if not prompt:
            continue

        if prompt.startswith("/") or prompt in {"exit", "quit"}:
            outcome = _handle_slash(prompt, agent, settings, session)
            if outcome.quit:
                _finish_chat(session, agent, saving)
                return 0
            if outcome.settings is not None:
                settings = outcome.settings
            continue

        session.set_title_from(prompt)
        try:
            result = run_turn(agent, settings, prompt, plain=bool(args.no_color))
            session.touch(agent.history, result.usage)
            if saving:
                session.save()
        except JaigentError as exc:
            err_console.print(f"[red]error:[/] {exc}")
        except KeyboardInterrupt:
            console.print(f"\n[{MUTED}]interrupted[/]")


@dataclass(slots=True)
class SlashResult:
    """What the REPL should do after an in-chat command."""

    quit: bool = False
    settings: Settings | None = None


def _handle_slash(  # noqa: C901 - a dispatch table reads better than many functions
    prompt: str, agent: Agent, settings: Settings, session: sessions.Session
) -> SlashResult:
    """Run an in-chat command and say whether to quit or adopt new settings."""
    command, _, argument = prompt.partition(" ")
    command = command.lower()
    argument = argument.strip()

    if command in {"/exit", "/quit", "exit", "quit"}:
        return SlashResult(quit=True)

    if command == "/help":
        console.print(HELP_TEXT, highlight=False, style=MUTED)
    elif command == "/reset":
        agent.reset()
        session.messages = []
        console.print(f"[{MUTED}]conversation cleared[/]")
    elif command == "/tools":
        _print_tools(agent.tools)
    elif command == "/cost":
        cost = estimate(settings.model, session.usage)
        console.print(f"[{MUTED}]session total: {cost.summary()}[/]", highlight=False)
    elif command == "/save":
        path = session.save()
        console.print(f"[{MUTED}]saved to {path}[/]", highlight=False)
    elif command == "/undo":
        removed = _undo(agent)
        session.messages = agent.history
        console.print(
            f"[{MUTED}]{'dropped the last exchange' if removed else 'nothing to undo'}[/]"
        )
    elif command == "/model":
        if not argument:
            console.print(f"[{MUTED}]current model: {settings.model}[/]", highlight=False)
            return SlashResult()
        updated = settings.merged_with(model=argument)
        agent.settings = updated
        agent.provider = get_provider(updated)
        session.model = argument
        console.print(f"[{MUTED}]model is now {argument}[/]", highlight=False)
        return SlashResult(settings=updated)
    elif command == "/workspace":
        if not argument:
            console.print(f"[{MUTED}]workspace: {settings.workspace}[/]", highlight=False)
            return SlashResult()
        target = Path(argument).expanduser()
        if not target.is_dir():
            err_console.print(f"[red]{target} is not a directory[/]")
            return SlashResult()
        updated = settings.merged_with(workspace=target)
        agent.settings = updated
        agent.tools = build_default_registry(updated)
        agent.approver.workspace = updated.workspace
        session.workspace = str(updated.workspace)
        console.print(f"[{MUTED}]workspace is now {updated.workspace}[/]", highlight=False)
        return SlashResult(settings=updated)
    else:
        console.print(f"[{MUTED}]unknown command {command}. /help for the list[/]", highlight=False)
    return SlashResult()


def _undo(agent: Agent) -> bool:
    """Remove the most recent user turn and everything after it."""
    indices = [i for i, m in enumerate(agent.history) if m.get("role") == "user"]
    if not indices:
        return False
    agent.history = agent.history[: indices[-1]]
    return True


def _finish_chat(session: sessions.Session, agent: Agent, saving: bool) -> None:
    if saving and agent.history:
        session.touch(agent.history)
        session.save()
        console.print(f"\n[{MUTED}]session saved as {session.id}[/]", highlight=False)
    else:
        console.print(f"\n[{MUTED}]bye[/]")


def cmd_sessions(args: argparse.Namespace) -> int:
    """List, or delete, saved conversations."""
    target = getattr(args, "delete", None)
    if target:
        if target == "all":
            removed = 0
            for saved_session in sessions.list_sessions(limit=10_000):
                removed += int(saved_session.delete())
            console.print(f"[{MUTED}]deleted {removed} session(s)[/]")
            return 0
        found = sessions.resolve(target)
        if found is None or not found.delete():
            err_console.print(f"[red]No session matching {target!r}.[/]")
            return 1
        console.print(f"[{MUTED}]deleted {found.id}[/]")
        return 0

    saved = sessions.list_sessions()
    if not saved:
        console.print(
            f"[{MUTED}]No saved sessions yet. Start one with[/] [{ACCENT}]jaigent chat[/]",
            highlight=False,
        )
        return 0

    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=box.SIMPLE)
    table.add_column("ID", style=ACCENT, no_wrap=True)
    table.add_column("When", style=MUTED, no_wrap=True)
    table.add_column("Turns", justify="right", style=MUTED)
    table.add_column("Model", style=MUTED, no_wrap=True)
    table.add_column("Title", overflow="ellipsis")

    for session in saved:
        table.add_row(
            session.id,
            session.age(),
            str(session.turns),
            session.model or "—",
            session.title or "[dim]untitled[/]",
        )
    console.print(table)
    console.print(
        f"[{MUTED}]Resume with[/] [{ACCENT}]jaigent chat --resume <id>[/]"
        f"[{MUTED}], or just[/] [{ACCENT}]--resume[/] [{MUTED}]for the most recent.[/]",
        highlight=False,
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Interactive setup: choose a provider, store a key, verify it works."""
    console.print(render_logo(console, version=__version__))
    console.print()

    env_path = Path.cwd() / ".env"
    if env_path.exists() and not args.force:
        console.print(f"[yellow]{env_path} already exists.[/]")
        if not _confirm("Overwrite it?", default=False):
            console.print(f"[{MUTED}]Nothing changed.[/]")
            return 0

    console.print(f"[bold {ACCENT}]1.[/] Which provider?\n")
    for index, name in enumerate(KNOWN_PROVIDERS, start=1):
        console.print(f"   [{ACCENT}]{index}[/]  {name}  [{MUTED}]{DEFAULT_MODELS[name]}[/]")
    console.print()

    choice = console.input(f"[{ACCENT}]provider [1]:[/] ").strip() or "1"
    try:
        provider = KNOWN_PROVIDERS[int(choice) - 1]
    except (ValueError, IndexError):
        provider = choice if choice in KNOWN_PROVIDERS else KNOWN_PROVIDERS[0]

    key_var = API_KEY_ENV_VARS[provider]
    console.print(f"\n[bold {ACCENT}]2.[/] Paste your {provider} API key.")
    console.print(f"   [{MUTED}]Get one at {_KEY_URLS[provider]}[/]")
    console.print(f"   [{MUTED}]It is written to .env, which is git-ignored.[/]\n")

    api_key = console.input(f"[{ACCENT}]{key_var}:[/] ", password=True).strip()
    if not api_key:
        err_console.print("[red]No key entered. Run jaigent init again when you have one.[/]")
        return 1

    default_model = DEFAULT_MODELS[provider]
    console.print(f"\n[bold {ACCENT}]3.[/] Which model?")
    # Text, not markup: the default is shown in [brackets] that rich would eat.
    model = console.input(Text(f"model [{default_model}]: ", style=ACCENT)).strip() or default_model

    lines = [
        "# Written by `jaigent init`. This file is git-ignored — never commit it.",
        f"JAIGENT_PROVIDER={provider}",
        f"JAIGENT_MODEL={model}",
        f"{key_var}={api_key}",
        "",
    ]
    env_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"\n[green]✓[/] wrote {env_path}")

    console.print(f"\n[bold {ACCENT}]4.[/] Testing the key…")
    settings = Settings(
        provider=provider,
        model=model,
        api_key=api_key,
        # Honour a gateway URL if one is already configured.
        base_url=os.getenv("JAIGENT_BASE_URL") or DEFAULT_BASE_URLS[provider],
        max_steps=1,
    )
    try:
        agent = Agent(settings, tools=ToolRegistry())
        reply = agent.run("Reply with exactly: ready")
        console.print(f"[green]✓[/] {provider} responded: [{MUTED}]{reply.output[:60]}[/]")
        if reply.cost.usd is not None:
            console.print(f"[{MUTED}]  that test cost about {reply.cost.format_usd()}[/]")
    except JaigentError as exc:
        err_console.print(f"[yellow]![/] the key was saved but the test call failed:\n  {exc}")
        return 1

    console.print(f"\n[bold {ACCENT}]You're set.[/] Try:\n")
    console.print(f'   [{ACCENT}]jaigent "summarise the files in this folder"[/]')
    console.print(f"   [{ACCENT}]jaigent chat[/]\n")
    return 0


_KEY_URLS = {
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
}


def _confirm(question: str, *, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = console.input(f"[{ACCENT}]{question} {suffix}:[/] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not answer:
        return default
    return answer in {"y", "yes"}


def cmd_tools(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    _print_tools(build_default_registry(settings))
    if not settings.allow_shell:
        console.print("[dim]run_command is hidden; enable it with --allow-shell.[/]")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    table = Table(title="jaigent configuration", show_header=True, header_style="bold cyan")
    table.add_column("Setting")
    table.add_column("Value", overflow="fold")
    for key, value in settings.redacted().items():
        table.add_row(key, str(value))
    console.print(table)

    if not settings.api_key:
        console.print(
            "\n[yellow]No API key configured.[/] jaigent never ships with one — bring your own:"
            "\n  export OPENAI_API_KEY='sk-...'    # or ANTHROPIC_API_KEY"
            "\n  cp .env.example .env             # and fill it in"
        )
        return 1
    return 0


# ----------------------------------------------------------------------
# Rendering helpers
# ----------------------------------------------------------------------
def print_splash(parser: argparse.ArgumentParser) -> None:
    """The front door: logo, a couple of real examples, then the usage text."""
    console.print()
    console.print(render_logo(console, version=__version__))
    console.print()

    examples = (
        ('jaigent "summarise the README in this folder"', "run one task"),
        ("jaigent chat", "interactive session"),
        ("jaigent tools", "list what the agent can do"),
        ("jaigent config", "check your setup"),
    )
    width = max(len(command) for command, _ in examples)
    # Only pad and annotate when the notes actually fit; otherwise show bare commands.
    roomy = console.width >= width + max(len(note) for _, note in examples) + 6

    for command, note in examples:
        line = Text("  ")
        if roomy:
            line.append(command.ljust(width), style="green")
            line.append(f"   {note}", style="dim")
        else:
            line.append(command, style="green")
        console.print(line, overflow="ellipsis", no_wrap=True)

    console.print("\n[dim]Bring your own API key:[/] [cyan]export OPENAI_API_KEY='sk-...'[/]")
    console.print("[dim]Full options:[/] [cyan]jaigent --help[/]\n")


def _print_answer(text: str, *, plain: bool = False) -> None:
    if not text:
        console.print(f"[{MUTED}](the model returned an empty answer)[/]")
        return
    if plain:
        print(text)
    else:
        console.print(Markdown(text))


def _print_tools(registry) -> None:  # noqa: ANN001 - ToolRegistry, avoids an import cycle in typing
    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=box.SIMPLE)
    table.add_column("Tool", style=ACCENT, no_wrap=True)
    table.add_column("Description", overflow="fold")
    for tool in registry:
        name = f"{tool.name} [red]⚠[/]" if tool.dangerous else tool.name
        table.add_row(name, tool.description)
    console.print(table)


def _print_footer(result: AgentResult, settings: Settings) -> None:
    """The one-line summary after each turn: tools used, tokens, spend."""
    bits: list[str] = []
    if result.tool_calls:
        bits.append(f"{result.tool_calls} tool call{'s' if result.tool_calls != 1 else ''}")
    if settings.show_cost:
        summary = result.cost.summary()
        if result.cost.total_tokens:
            bits.append(summary)
    if result.stopped_early:
        bits.append("step budget exhausted")

    if bits:
        console.print(f"[{MUTED}]{' · '.join(bits)}[/]", highlight=False)


# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(normalise_argv(raw))

    # A subparser default can clobber a top-level --no-color, so trust the raw argv.
    if "--no-color" in raw or getattr(args, "no_color", False):
        console.no_color = True
        err_console.no_color = True
        args.no_color = True

    if getattr(args, "logo", False):
        console.print(render_logo(console, version=__version__))
        return 0

    if args.command is None:
        print_splash(parser)
        return 0

    handlers = {
        "run": cmd_run,
        "chat": cmd_chat,
        "tools": cmd_tools,
        "config": cmd_config,
        "sessions": cmd_sessions,
        "init": cmd_init,
    }
    try:
        return handlers[args.command](args)
    except ConfigurationError as exc:
        err_console.print(f"[red]configuration error:[/] {exc}")
        return 78  # EX_CONFIG
    except JaigentError as exc:
        err_console.print(f"[red]error:[/] {exc}")
        return 1
    except KeyboardInterrupt:
        err_console.print("\n[dim]interrupted[/]")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
