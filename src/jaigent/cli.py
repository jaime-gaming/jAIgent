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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from jaigent import (
    __version__,
    commands,
    failover,
    gateway,
    models,
    paths,
    pricing,
    router,
    schedule,
    settings_store,
    skills,
)
from jaigent import session as sessions
from jaigent.agent import Agent, AgentResult
from jaigent.approval import Approver, Mode
from jaigent.branding import (
    ACCENT,
    ACCENT_DIM,
    MUTED,
    PROMPT_MARK,
    render_banner,
    render_logo,
)
from jaigent.checkpoint import CheckpointStore, checkpoint_dir
from jaigent.config import (
    API_KEY_ENV_VARS,
    APPROVAL_MODES,
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    KNOWN_PROVIDERS,
    Settings,
)
from jaigent.errors import ConfigurationError, JaigentError, ToolError
from jaigent.llm import get_provider
from jaigent.pricing import estimate
from jaigent.tools import ToolRegistry, build_default_registry
from jaigent.ui import Thinking, glyph, result_line, supports_unicode, tool_line

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
        "--no-checkpoints",
        action="store_true",
        default=None,
        help="Do not snapshot files before changing them. Disables undo and rewind.",
    )
    common.add_argument(
        "--no-failover",
        action="store_true",
        default=None,
        help="Fail immediately instead of retrying or trying another provider.",
    )
    common.add_argument(
        "--retries",
        type=int,
        metavar="N",
        help="Attempts per provider before failing over. 1 disables retrying.",
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

    # ---------------------------------------------------------------- models
    models_cmd = sub.add_parser("models", parents=[common], help="Browse known models.")
    models_cmd.add_argument("search", nargs="?", help="Filter by id, name or provider.")
    models_cmd.add_argument(
        "--only", dest="only_provider", metavar="PROVIDER", help="Show one provider only."
    )

    # -------------------------------------------------------------- settings
    settings_cmd = sub.add_parser(
        "settings", parents=[common], help="Read and write persistent settings."
    )
    settings_sub = settings_cmd.add_subparsers(dest="settings_action")
    settings_sub.add_parser("list", help="Show stored settings and where they came from.")

    set_cmd = settings_sub.add_parser("set", help="Store a setting.")
    set_cmd.add_argument("key")
    set_cmd.add_argument("value")
    set_cmd.add_argument(
        "--project",
        action="store_true",
        help="Write to ./.jaigent/settings.json instead of your home directory.",
    )

    unset_cmd = settings_sub.add_parser("unset", help="Remove a stored setting.")
    unset_cmd.add_argument("key")
    unset_cmd.add_argument("--project", action="store_true", help="Act on the project file.")

    settings_sub.add_parser("path", help="Print the settings file locations.")

    # ---------------------------------------------------------------- skills
    skills_cmd = sub.add_parser("skills", parents=[common], help="Manage reusable skills.")
    skills_sub = skills_cmd.add_subparsers(dest="skills_action")
    skills_sub.add_parser("list", help="List available skills.")

    show_skill = skills_sub.add_parser("show", help="Print a skill in full.")
    show_skill.add_argument("name")

    new_skill = skills_sub.add_parser("new", help="Create a skill.")
    new_skill.add_argument("name")
    new_skill.add_argument("-d", "--description", default="", help="One-line summary.")
    new_skill.add_argument(
        "-b", "--body", default="", help="Instructions. Omit to open a starter template."
    )
    new_skill.add_argument(
        "--user", action="store_true", help="Save to ~/.jaigent/skills instead of the project."
    )

    remove_skill = skills_sub.add_parser("remove", help="Delete a skill.")
    remove_skill.add_argument("name")

    # -------------------------------------------------------------- schedule
    schedule_cmd = sub.add_parser("schedule", parents=[common], help="Run tasks on a timer.")
    schedule_sub = schedule_cmd.add_subparsers(dest="schedule_action")
    schedule_sub.add_parser("list", parents=[common], help="Show scheduled tasks.")

    add_task = schedule_sub.add_parser("add", parents=[common], help="Schedule a prompt.")
    add_task.add_argument("prompt", help="What the agent should do.")
    add_task.add_argument(
        "-e",
        "--every",
        required=True,
        help="Interval: 30m, 2h, hourly, daily, 'daily at 09:00', weekly.",
    )

    for action, helptext in (
        ("remove", "Delete a scheduled task."),
        ("pause", "Stop a task running."),
        ("resume", "Start a paused task again."),
        ("show", "Show a task and its last result."),
    ):
        task_cmd = schedule_sub.add_parser(action, parents=[common], help=helptext)
        task_cmd.add_argument("id")

    run_tasks = schedule_sub.add_parser("run", parents=[common], help="Run whatever is due.")
    run_tasks.add_argument("--id", help="Run one task now, whether or not it is due.")
    run_tasks.add_argument(
        "--watch", action="store_true", help="Stay running and execute tasks as they fall due."
    )
    run_tasks.add_argument(
        "--interval", type=int, default=60, help="Seconds between checks when watching."
    )

    # -------------------------------------------------------------- commands
    commands_cmd = sub.add_parser(
        "commands", parents=[common], help="Manage custom slash commands."
    )
    commands_sub = commands_cmd.add_subparsers(dest="commands_action")
    commands_sub.add_parser("list", parents=[common], help="List custom commands.")

    show_command = commands_sub.add_parser("show", parents=[common], help="Print a command.")
    show_command.add_argument("name")

    new_command = commands_sub.add_parser("new", parents=[common], help="Create a command.")
    new_command.add_argument("name")
    new_command.add_argument("-d", "--description", default="", help="One-line summary.")
    new_command.add_argument("--template", default="", help="Prompt template.")
    new_command.add_argument("--user", action="store_true", help="Save to your home directory.")

    remove_command = commands_sub.add_parser("remove", parents=[common], help="Delete a command.")
    remove_command.add_argument("name")

    # ------------------------------------------------------------------ keys
    keys_cmd = sub.add_parser("keys", parents=[common], help="Manage jaigent API keys.")
    keys_sub = keys_cmd.add_subparsers(dest="keys_action")
    keys_sub.add_parser("list", parents=[common], help="List issued keys.")

    new_key = keys_sub.add_parser("new", parents=[common], help="Create a key.")
    new_key.add_argument("name", nargs="?", default="default", help="Label for the key.")

    revoke = keys_sub.add_parser("revoke", parents=[common], help="Revoke a key.")
    revoke.add_argument("id", help="Key id or name.")

    # ----------------------------------------------------------------- serve
    serve_cmd = sub.add_parser(
        "serve", parents=[common], help="Expose the agent as an OpenAI-compatible API."
    )
    serve_cmd.add_argument("--host", default="127.0.0.1", help="Interface to bind.")
    serve_cmd.add_argument("--port", type=int, default=8787, help="Port to listen on.")
    serve_cmd.add_argument(
        "--no-auth", action="store_true", help="Accept unauthenticated requests (local only)."
    )

    # ----------------------------------------------------------------- route
    route_cmd = sub.add_parser(
        "route", parents=[common], help="Show which model auto mode would pick."
    )
    route_cmd.add_argument("prompt", nargs="+", help="The task to classify.")

    # ----------------------------------------------------------- checkpoints
    sub.add_parser("undo", parents=[common], help="Revert the most recent file change.")

    cp_cmd = sub.add_parser("checkpoints", parents=[common], help="Browse the undo history.")
    cp_cmd.add_argument("--clear", action="store_true", help="Delete every checkpoint.")

    rewind_cmd = sub.add_parser("rewind", parents=[common], help="Restore a checkpoint.")
    rewind_cmd.add_argument("id", help="Checkpoint id, or a prefix of one.")

    # ---------------------------------------------------------------- doctor
    sub.add_parser(
        "doctor", parents=[common], help="Check the install, keys and provider reachability."
    )
    return parser


#: Recognised subcommands, used to detect the bare-prompt shorthand.
COMMANDS = (
    "run",
    "chat",
    "commands",
    "keys",
    "serve",
    "route",
    "undo",
    "checkpoints",
    "rewind",
    "doctor",
    "tools",
    "config",
    "sessions",
    "init",
    "models",
    "settings",
    "skills",
    "schedule",
)


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
    if first.startswith("/"):
        # A custom slash command used straight from the shell.
        return ["run", *argv]
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
    checkpoints = False if getattr(args, "no_checkpoints", None) else None
    failover_enabled = False if getattr(args, "no_failover", None) else None

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
        checkpoints=checkpoints,
        failover=failover_enabled,
        retries=getattr(args, "retries", None),
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
    """Run one turn with a live status line, then print the footer.

    The animated line shows the elapsed time, a rotating verb and the tool
    currently running. It is torn down the moment the first token of the answer
    arrives, so streamed text is never interleaved with the animation.
    """
    streaming = settings.stream and not plain
    status = Thinking(console, animate=not plain and not settings.verbose)

    def on_tool(name: str, arguments: dict, output: str) -> None:
        status.tool_started(name)
        if settings.verbose:
            status.stop()
            console.print(tool_line(name, _preview_args(arguments)))
            first = (output or "").splitlines()[0] if output else ""
            console.print(result_line(first[:150], ok=not output.startswith("ERROR")))
        status.thinking_again()

    def on_route(routing) -> None:  # noqa: ANN001 - jaigent.router.Routing
        status.update(detail=routing.model)
        if settings.verbose:
            console.print(f"[{MUTED}]  {routing.summary()}[/]", highlight=False)

    agent.on_tool_call = on_tool
    agent.on_route = on_route

    printer = _StreamPrinter(console, status) if streaming else None
    agent.on_text = printer

    status.start()
    try:
        result = agent.run(prompt)
    finally:
        status.stop()

    if printer is not None:
        printer.finish()
        if not printer.wrote and result.output:
            _print_answer(result.output, plain=plain)
    else:
        _print_answer(result.output, plain=plain)

    _print_footer(result, settings)
    return result


def _preview_args(arguments: dict, limit: int = 70) -> str:
    """Compact one-line rendering of tool arguments for verbose mode."""
    parts = []
    for key, value in arguments.items():
        text = str(value).replace("\n", "\\n")
        parts.append(f"{key}={text[:32] + '…' if len(text) > 32 else text}")
    joined = " ".join(parts)
    return joined if len(joined) <= limit else joined[:limit] + "…"


class _StreamPrinter:
    """Writes streamed chunks straight to the console.

    Stops the animation on the first chunk, so the spinner does not fight with
    the text being printed underneath it.
    """

    def __init__(self, target: Console, status: Thinking | None = None) -> None:
        self.target = target
        self.status = status
        self.wrote = False

    def __call__(self, chunk: str) -> None:
        if not chunk:
            return
        if not self.wrote and self.status is not None:
            self.status.stop()
        self.wrote = True
        self.target.file.write(chunk)
        self.target.file.flush()

    def finish(self) -> None:
        if self.wrote:
            self.target.file.write("\n")
            self.target.file.flush()


def expand_command(prompt: str, settings: Settings) -> str:
    """Turn ``/name args`` into the command's prompt template.

    Unknown slash commands are passed through untouched, so a prompt that
    happens to start with a slash still reaches the model.
    """
    match = commands.resolve(prompt)
    if match is None:
        return prompt
    command, arguments = match
    return command.render(arguments, workspace=str(settings.workspace))


def cmd_run(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        err_console.print('[red]No prompt given.[/] Try: jaigent "summarise README.md"')
        return 2

    if prompt.startswith("/"):
        expanded = expand_command(prompt, settings)
        if expanded == prompt and commands.resolve(prompt) is None:
            known = ", ".join(f"/{n}" for n in sorted(commands.discover())) or "(none defined)"
            err_console.print(f"[red]Unknown command {prompt.split()[0]}.[/] Available: {known}")
            return 1
        prompt = expanded

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
/revert               undo the agent's last file change on disk
/checkpoints          list restorable file checkpoints
/rewind <id>          restore a checkpoint by id
/diff                 show what the last change would revert
/status               provider, model, workspace and session at a glance
/approve <mode>       ask, auto or dry-run
/commands             list custom commands
/doctor               check keys, storage and providers
/exit                 quit

Custom commands from .jaigent/commands are available too — /commands to see them."""


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
            if outcome.prompt:
                session.set_title_from(outcome.prompt)
                try:
                    result = run_turn(agent, settings, outcome.prompt, plain=bool(args.no_color))
                    session.touch(agent.history, result.usage)
                    if saving:
                        session.save()
                except JaigentError as exc:
                    err_console.print(f"[red]error:[/] {exc}")
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
    #: A custom command expanded into a prompt the agent should now run.
    prompt: str | None = None


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
    elif command == "/revert":
        store = agent.checkpoints
        if store is None:
            console.print(f"[{MUTED}]checkpoints are disabled[/]")
            return SlashResult()
        checkpoint = store.latest()
        if checkpoint is None:
            console.print(f"[{MUTED}]nothing to revert[/]")
            return SlashResult()
        _restore(store, checkpoint, plain=False)
    elif command == "/checkpoints":
        store = agent.checkpoints
        if store is None:
            console.print(f"[{MUTED}]checkpoints are disabled[/]")
            return SlashResult()
        history = store.history(limit=10)
        if not history:
            console.print(f"[{MUTED}]no checkpoints yet[/]")
            return SlashResult()
        for checkpoint in history:
            console.print(
                f"  [{ACCENT}]{checkpoint.id}[/]  [{MUTED}]{checkpoint.age():>9}  "
                f"{checkpoint.tool or '-'}  {checkpoint.summary()}[/]",
                highlight=False,
            )
    elif command == "/rewind":
        store = agent.checkpoints
        if store is None:
            console.print(f"[{MUTED}]checkpoints are disabled[/]")
            return SlashResult()
        if not argument:
            console.print(f"[{MUTED}]usage: /rewind <id> — /checkpoints for the list[/]")
            return SlashResult()
        checkpoint = store.get(argument)
        if checkpoint is None:
            err_console.print(f"[red]No checkpoint matching {argument!r}.[/]")
            return SlashResult()
        _restore(store, checkpoint, plain=False)
    elif command == "/diff":
        store = agent.checkpoints
        checkpoint = store.latest() if store is not None else None
        if store is None or checkpoint is None:
            console.print(f"[{MUTED}]nothing to compare[/]")
            return SlashResult()
        rows = [row for row in store.diff_summary(checkpoint) if row[1] != "unchanged"]
        if not rows:
            console.print(f"[{MUTED}]no pending changes to revert[/]")
            return SlashResult()
        for changed, action in rows:
            console.print(f"  [{MUTED}]{action:>9}[/]  {changed}", highlight=False)
    elif command == "/status":
        _print_status(agent, settings, session)
    elif command == "/approve":
        modes = APPROVAL_MODES
        if argument not in modes:
            console.print(
                f"[{MUTED}]approval is {settings.approval}. Choose one of: {', '.join(modes)}[/]",
                highlight=False,
            )
            return SlashResult()
        updated = settings.merged_with(approval=argument)
        agent.settings = updated
        agent.approver.mode = Mode(argument)
        console.print(f"[{MUTED}]approval is now {argument}[/]", highlight=False)
        return SlashResult(settings=updated)
    elif command == "/commands":
        found = commands.discover()
        if not found:
            console.print(f"[{MUTED}]no custom commands yet — add one under .jaigent/commands[/]")
            return SlashResult()
        for name in sorted(found):
            console.print(
                f"  [{ACCENT}]/{name}[/]  [{MUTED}]{found[name].description}[/]",
                highlight=False,
            )
    elif command == "/doctor":
        _run_doctor(settings, plain=False)
    else:
        custom = commands.discover().get(command.lstrip("/"))
        if custom is not None:
            expanded = custom.render(argument, workspace=str(settings.workspace))
            return SlashResult(prompt=expanded)
        known = ", ".join(f"/{n}" for n in sorted(commands.discover()))
        extra = f" Custom: {known}" if known else ""
        console.print(
            f"[{MUTED}]unknown command {command}. /help for the list.{extra}[/]",
            highlight=False,
        )
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
    # The file holds a live API key, so it must not be world-readable.
    paths.write_private(env_path, "\n".join(lines))
    console.print(f"\n[green]✓[/] wrote {env_path} [dim](owner-only)[/]")

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


def cmd_models(args: argparse.Namespace) -> int:
    """Browse the curated catalogue of tool-calling models."""
    entries = models.search(args.search) if args.search else list(models.CATALOGUE)
    if getattr(args, "only_provider", None):
        wanted = args.only_provider.strip().lower()
        entries = [m for m in entries if m.provider == wanted]

    if not entries:
        console.print(f"[{MUTED}]No models match that filter.[/]")
        return 1

    settings = resolve_settings(args)
    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=box.SIMPLE)
    table.add_column("Model", style=ACCENT, no_wrap=True)
    table.add_column("Provider", style=MUTED, no_wrap=True)
    table.add_column("Context", style=MUTED, no_wrap=True)
    table.add_column("Notes", overflow="fold")

    for model in entries:
        price = pricing.price_for(model.id)
        note = model.note
        if price:
            note = f"{note} · ${price[0]:g}/${price[1]:g} per Mtok".strip(" ·")
        marker = " ←" if model.id == settings.model else ""
        table.add_row(f"{model.id}{marker}", model.provider, model.context, note)

    console.print(table)
    console.print(
        f"[{MUTED}]Any model id works with[/] [{ACCENT}]--model[/][{MUTED}]; this list is "
        f"only the curated set. Providers:[/] [{ACCENT}]{', '.join(KNOWN_PROVIDERS)}[/]",
        highlight=False,
    )
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    """Read and write the persistent settings files."""
    action = getattr(args, "settings_action", None) or "list"
    scope = "project" if getattr(args, "project", False) else "user"

    if action == "path":
        console.print(f"[{MUTED}]user:   [/]{settings_store.user_settings_path()}")
        console.print(f"[{MUTED}]project:[/]{settings_store.project_settings_path()}")
        return 0

    if action == "set":
        path = settings_store.set_value(args.key, args.value, scope=scope)
        console.print(f"[green]✓[/] {args.key} = {args.value}  [{MUTED}]({scope}: {path})[/]")
        return 0

    if action == "unset":
        if settings_store.unset_value(args.key, scope=scope):
            console.print(f"[green]✓[/] removed {args.key} from {scope} settings")
            return 0
        console.print(f"[{MUTED}]{args.key} was not set in {scope} settings[/]")
        return 1

    rows = settings_store.describe()
    if not rows:
        console.print(
            f"[{MUTED}]No stored settings. Set one with[/] "
            f"[{ACCENT}]jaigent settings set model gpt-4o[/]",
            highlight=False,
        )
        return 0

    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=box.SIMPLE)
    table.add_column("Setting", style=ACCENT, no_wrap=True)
    table.add_column("Value", overflow="fold")
    table.add_column("From", style=MUTED, no_wrap=True)
    for key, value, source in rows:
        table.add_row(key, str(value), source)
    console.print(table)
    console.print(
        f"[{MUTED}]Precedence: CLI flags → environment → project file → user file → defaults.[/]"
    )
    return 0


SKILL_TEMPLATE = """\
Describe the steps the agent should follow.

Be specific about inputs, the order of operations, and what the finished
result looks like. This text is handed to the model verbatim when the
skill is loaded.
"""


def cmd_skills(args: argparse.Namespace) -> int:
    """List, show, create and delete skills."""
    action = getattr(args, "skills_action", None) or "list"
    available = skills.discover()

    if action == "list":
        if not available:
            console.print(
                f"[{MUTED}]No skills yet. Create one with[/] "
                f"[{ACCENT}]jaigent skills new changelog -d 'Write a changelog'[/]",
                highlight=False,
            )
            return 0

        table = Table(show_header=True, header_style=f"bold {ACCENT}", box=box.SIMPLE)
        table.add_column("Skill", style=ACCENT, no_wrap=True)
        table.add_column("Scope", style=MUTED, no_wrap=True)
        table.add_column("Description", overflow="fold")
        for skill in sorted(available.values(), key=lambda s: s.name):
            table.add_row(skill.name, skill.scope, skill.description or "[dim]—[/]")
        console.print(table)
        console.print(f"[{MUTED}]The agent loads these on demand with the load_skill tool.[/]")
        return 0

    if action == "show":
        found_skill = available.get(args.name.strip().lower())
        if found_skill is None:
            err_console.print(f"[red]No skill named {args.name!r}.[/]")
            return 1
        console.print(
            Panel(
                Markdown(found_skill.body.strip()),
                title=f"[bold {ACCENT}]{found_skill.name}[/]",
                subtitle=f"[{MUTED}]{found_skill.path}[/]",
                border_style=ACCENT_DIM,
            )
        )
        return 0

    if action == "new":
        scope = "user" if getattr(args, "user", False) else "project"
        body = args.body or SKILL_TEMPLATE
        description = args.description or f"The {args.name} procedure."
        try:
            path = skills.create_skill(args.name, description, body, scope=scope)
        except ToolError as exc:
            err_console.print(f"[red]{exc}[/]")
            return 1
        console.print(f"[green]✓[/] created {path}")
        if not args.body:
            console.print(f"[{MUTED}]Edit it to describe the procedure.[/]")
        return 0

    if action == "remove":
        doomed = available.get(args.name.strip().lower())
        if doomed is None:
            err_console.print(f"[red]No skill named {args.name!r}.[/]")
            return 1
        doomed.path.unlink()
        console.print(f"[green]✓[/] removed {doomed.path}")
        return 0

    return 0


def cmd_schedule(args: argparse.Namespace) -> int:  # noqa: C901 - dispatch table
    """Manage and execute scheduled tasks."""
    action = getattr(args, "schedule_action", None) or "list"

    if action == "add":
        settings = resolve_settings(args)
        try:
            task = schedule.add(
                args.prompt,
                args.every,
                workspace=str(settings.workspace),
                model=settings.model,
            )
        except ConfigurationError as exc:
            err_console.print(f"[red]{exc}[/]")
            return 1
        when = datetime.fromtimestamp(task.next_run).strftime("%Y-%m-%d %H:%M")
        console.print(f"[green]✓[/] {task.id}: {task.prompt}")
        console.print(f"[{MUTED}]  {task.interval} · first run {when}[/]", highlight=False)
        console.print(
            f"\n[{MUTED}]Run due tasks with[/] [{ACCENT}]jaigent schedule run[/]"
            f"[{MUTED}], or keep a worker alive with[/] [{ACCENT}]--watch[/][{MUTED}].[/]",
            highlight=False,
        )
        return 0

    if action in {"remove", "pause", "resume", "show"}:
        target = schedule.get(args.id)
        if target is None:
            err_console.print(f"[red]No task matching {args.id!r}.[/]")
            return 1
        task = target

        if action == "remove":
            schedule.remove(task.id)
            console.print(f"[green]✓[/] removed {task.id}")
        elif action == "pause":
            schedule.set_enabled(task.id, False)
            console.print(f"[{MUTED}]{task.id} paused[/]")
        elif action == "resume":
            schedule.set_enabled(task.id, True)
            console.print(f"[{MUTED}]{task.id} resumed[/]")
        else:
            _show_task(task)
        return 0

    if action == "run":
        return _run_scheduled(args)

    tasks = schedule.load_all()
    if not tasks:
        console.print(
            f"[{MUTED}]No scheduled tasks. Add one with[/]\n"
            f'  [{ACCENT}]jaigent schedule add "check the news" --every 2h[/]',
            highlight=False,
        )
        return 0

    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=box.SIMPLE)
    table.add_column("ID", style=ACCENT, no_wrap=True)
    table.add_column("Every", style=MUTED, no_wrap=True)
    table.add_column("Next", style=MUTED, no_wrap=True)
    table.add_column("Runs", justify="right", style=MUTED)
    table.add_column("Last", style=MUTED, no_wrap=True)
    table.add_column("Prompt", overflow="ellipsis")

    for task in tasks:
        status = task.last_status or "—"
        colour = {"ok": "green", "error": "red"}.get(status, MUTED)
        runs = f"{task.runs}" + (f" ({task.failures}✗)" if task.failures else "")
        table.add_row(
            task.id,
            task.interval,
            task.due_in(),
            runs,
            f"[{colour}]{status}[/]",
            task.prompt,
        )
    console.print(table)
    return 0


def _show_task(task: schedule.Task) -> None:
    console.print(f"[bold {ACCENT}]{task.id}[/]  {task.prompt}")
    console.print(f"[{MUTED}]interval:  {task.interval}[/]", highlight=False)
    console.print(f"[{MUTED}]workspace: {task.workspace}[/]", highlight=False)
    console.print(f"[{MUTED}]next run:  {task.due_in()}[/]", highlight=False)
    console.print(
        f"[{MUTED}]history:   {task.runs} run(s), {task.failures} failure(s)[/]",
        highlight=False,
    )
    if task.last_output:
        console.print(
            Panel(
                task.last_output[:1500],
                title=f"[{MUTED}]last result ({task.last_status})[/]",
                border_style=ACCENT_DIM,
            )
        )


def run_task(task: schedule.Task, args: argparse.Namespace) -> bool:
    """Execute one scheduled task. Returns whether it succeeded.

    Scheduled runs are non-interactive: approval is forced to ``auto`` because
    there is nobody to answer a prompt, and streaming is off so the log stays
    readable.
    """
    base = resolve_settings(args)
    settings = base.merged_with(
        workspace=Path(task.workspace) if task.workspace else None,
        model=task.model or None,
        approval="auto",
        stream=False,
    )

    started = datetime.now().strftime("%H:%M:%S")
    console.print(f"[{MUTED}][{started}][/] [bold {ACCENT}]{task.id}[/] {task.prompt}")

    try:
        agent = Agent(settings, approver=Approver(Mode.AUTO, workspace=settings.workspace))
        result = agent.run(task.prompt)
    except JaigentError as exc:
        task.record("error", str(exc))
        schedule.update(task)
        err_console.print(f"[red]  failed:[/] {exc}")
        return False

    task.record("ok", result.output)
    schedule.update(task)

    summary = result.output.strip().splitlines()[0][:120] if result.output else "(no output)"
    console.print(f"[green]  ✓[/] {summary}")
    if settings.show_cost and result.cost.total_tokens:
        console.print(f"[{MUTED}]    {result.cost.summary()}[/]", highlight=False)
    return True


def _run_scheduled(args: argparse.Namespace) -> int:
    """``schedule run``: one pass, a single task, or a watch loop."""
    if getattr(args, "id", None):
        task = schedule.get(args.id)
        if task is None:
            err_console.print(f"[red]No task matching {args.id!r}.[/]")
            return 1
        return 0 if run_task(task, args) else 1

    if not args.watch:
        due = schedule.due_tasks()
        if not due:
            console.print(f"[{MUTED}]Nothing due.[/]")
            return 0
        failures = sum(not run_task(task, args) for task in due)
        return 1 if failures else 0

    interval = max(5, int(args.interval))
    console.print(
        f"[{ACCENT}]watching[/] [{MUTED}]· checking every {interval}s · Ctrl-C to stop[/]",
        highlight=False,
    )
    try:
        while True:
            for task in schedule.due_tasks():
                run_task(task, args)
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print(f"\n[{MUTED}]stopped[/]")
        return 0


COMMAND_TEMPLATE = """\
Describe what the agent should do. Use $ARGUMENTS for everything the user
types after the command name, or $1 and $2 for individual words.

For example:
    Review $ARGUMENTS for correctness problems first, style second.
"""


def cmd_commands(args: argparse.Namespace) -> int:
    """List, show, create and delete custom slash commands."""
    action = getattr(args, "commands_action", None) or "list"
    available = commands.discover()

    if action == "list":
        if not available:
            console.print(
                f"[{MUTED}]No custom commands yet. Create one with[/]\n"
                f"  [{ACCENT}]jaigent commands new review -d 'Review the diff'[/]",
                highlight=False,
            )
            return 0

        table = Table(show_header=True, header_style=f"bold {ACCENT}", box=box.SIMPLE)
        table.add_column("Command", style=ACCENT, no_wrap=True)
        table.add_column("Scope", style=MUTED, no_wrap=True)
        table.add_column("Description", overflow="fold")
        for command in sorted(available.values(), key=lambda c: c.name):
            table.add_row(f"/{command.name}", command.scope, command.description or "[dim]—[/]")
        console.print(table)
        console.print(
            f"[{MUTED}]Use them in chat as[/] [{ACCENT}]/name args[/][{MUTED}], "
            f"or from the shell as[/] [{ACCENT}]jaigent /name args[/]",
            highlight=False,
        )
        return 0

    if action == "show":
        found = available.get(args.name.strip().lstrip("/").lower())
        if found is None:
            err_console.print(f"[red]No command named {args.name!r}.[/]")
            return 1
        console.print(
            Panel(
                found.template,
                title=f"[bold {ACCENT}]/{found.name}[/]",
                subtitle=f"[{MUTED}]{found.path}[/]",
                border_style=ACCENT_DIM,
            )
        )
        return 0

    if action == "new":
        scope = "user" if getattr(args, "user", False) else "project"
        try:
            path = commands.create_command(
                args.name,
                args.description or f"The {args.name} command.",
                args.template or COMMAND_TEMPLATE,
                scope=scope,
            )
        except ToolError as exc:
            err_console.print(f"[red]{exc}[/]")
            return 1
        console.print(f"[green]{glyph('check')}[/] created {path}")
        if not args.template:
            console.print(f"[{MUTED}]Edit it to write the prompt template.[/]")
        return 0

    if action == "remove":
        doomed = available.get(args.name.strip().lstrip("/").lower())
        if doomed is None:
            err_console.print(f"[red]No command named {args.name!r}.[/]")
            return 1
        doomed.path.unlink()
        console.print(f"[green]{glyph('check')}[/] removed {doomed.path}")
        return 0

    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    """Create, list and revoke the keys that authenticate `jaigent serve`."""
    action = getattr(args, "keys_action", None) or "list"

    if action == "new":
        key = gateway.create_key(args.name)
        console.print(
            Panel(
                Text(key.secret or "", style=f"bold {ACCENT}"),
                title=f"[bold {ACCENT}]{key.name}[/]",
                subtitle=f"[{MUTED}]copy it now — it is not stored in plain text[/]",
                border_style=ACCENT_DIM,
            )
        )
        console.print(
            f"[{MUTED}]Use it against[/] [{ACCENT}]jaigent serve[/][{MUTED}]:[/]\n"
            f'  [{ACCENT}]OpenAI(base_url="http://localhost:8787/v1", '
            f'api_key="{key.secret}")[/]',
            highlight=False,
        )
        return 0

    if action == "revoke":
        revoked = gateway.revoke_key(args.id)
        if revoked is None:
            err_console.print(f"[red]No key matching {args.id!r}.[/]")
            return 1
        console.print(f"[green]{glyph('check')}[/] revoked {revoked.name} ({revoked.preview})")
        return 0

    keys = gateway.load_keys()
    if not keys:
        console.print(
            f"[{MUTED}]No API keys yet. Create one with[/] [{ACCENT}]jaigent keys new my-app[/]",
            highlight=False,
        )
        return 0

    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=box.SIMPLE)
    table.add_column("Name", style=ACCENT, no_wrap=True)
    table.add_column("Key", style=MUTED, no_wrap=True)
    table.add_column("Calls", justify="right", style=MUTED)
    table.add_column("Last used", style=MUTED, no_wrap=True)
    table.add_column("Status", no_wrap=True)

    for key in keys:
        last = (
            datetime.fromtimestamp(key.last_used).strftime("%Y-%m-%d %H:%M")
            if key.last_used
            else "never"
        )
        state = "[red]revoked[/]" if key.revoked else "[green]active[/]"
        table.add_row(key.name, key.preview, str(key.calls), last, state)
    console.print(table)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the OpenAI-compatible gateway."""
    settings = resolve_settings(args)
    require_key = not getattr(args, "no_auth", False)

    def factory(model: str | None = None, instructions: str | None = None) -> Agent:
        """Build a fresh agent per request so callers never share state."""
        request_settings = settings.merged_with(
            model=model or None,
            approval="auto",  # nobody is at a terminal to approve anything
            stream=False,
        )
        return Agent(
            request_settings,
            instructions=instructions,
            approver=Approver(Mode.AUTO, workspace=request_settings.workspace),
        )

    config = gateway.ServerConfig(
        host=args.host, port=args.port, require_key=require_key, verbose=settings.verbose
    )
    try:
        server = gateway.build_server(factory, config)
    except ConfigurationError as exc:
        err_console.print(f"[red]{exc}[/]")
        return 78
    except OSError as exc:
        err_console.print(f"[red]Could not bind {args.host}:{args.port} — {exc}[/]")
        return 1

    console.print(render_logo(console, version=__version__))
    console.print()
    console.print(
        f"  [{ACCENT}]{glyph('arrow')}[/] API      [bold]http://{args.host}:{args.port}/v1[/]",
        highlight=False,
    )
    console.print(
        f"  [{ACCENT}]{glyph('arrow')}[/] Model    [bold]{settings.model}[/] "
        f"[{MUTED}](auto selects per request)[/]",
        highlight=False,
    )
    console.print(
        f"  [{ACCENT}]{glyph('arrow')}[/] Auth     "
        + (
            f"[bold]{len([k for k in gateway.load_keys() if not k.revoked])} active key(s)[/]"
            if require_key
            else "[yellow]disabled — anyone who can reach this port can use it[/]"
        ),
        highlight=False,
    )
    console.print(f"\n[{MUTED}]Ctrl-C to stop.[/]\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print(f"\n[{MUTED}]stopped[/]")
    finally:
        server.server_close()
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    """Explain which model auto mode would choose, and why."""
    settings = resolve_settings(args)
    prompt = " ".join(args.prompt).strip()
    routing = router.choose_model(
        prompt,
        settings.provider,
        fallback=DEFAULT_MODELS.get(settings.provider, ""),
    )

    colour = {"simple": "green", "standard": ACCENT, "complex": "red"}[routing.difficulty.value]
    console.print()
    console.print(f"  [{MUTED}]prompt[/]      {prompt[:70]}", highlight=False)
    console.print(
        f"  [{MUTED}]difficulty[/]  [{colour}]{routing.difficulty.value}[/] "
        f"[{MUTED}](score {routing.score})[/]",
        highlight=False,
    )
    console.print(f"  [{MUTED}]signals[/]     {routing.reason}", highlight=False)
    console.print(
        f"  [{MUTED}]model[/]       [bold {ACCENT}]{routing.model}[/] "
        f"[{MUTED}]via {settings.provider}[/]\n",
        highlight=False,
    )
    return 0


def _restore(store: CheckpointStore, checkpoint, *, plain: bool) -> int:  # noqa: ANN001
    """Show what a rewind would do, then do it."""
    rows = store.diff_summary(checkpoint)
    actionable = [(path, action) for path, action in rows if action != "unchanged"]

    if not actionable:
        console.print(f"[{MUTED}]Nothing to revert — those files already match.[/]")
        return 0

    for path, action in actionable:
        colour = {"delete": "red", "recreate": "green"}.get(action, ACCENT)
        console.print(f"  [{colour}]{action:9}[/] {path}", highlight=False)

    changed = store.restore(checkpoint)
    console.print(
        f"\n[green]{glyph('check')}[/] reverted {len(changed)} file(s) "
        f"[{MUTED}]to {checkpoint.age()} ({checkpoint.label})[/]",
        highlight=False,
    )
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    """Revert the most recent file change the agent made."""
    settings = resolve_settings(args)
    store = CheckpointStore(settings.workspace)
    checkpoint = store.latest()

    if checkpoint is None:
        console.print(
            f"[{MUTED}]Nothing to undo. Checkpoints are written when the agent changes a file.[/]"
        )
        return 0
    return _restore(store, checkpoint, plain=bool(args.no_color))


def cmd_rewind(args: argparse.Namespace) -> int:
    """Restore any checkpoint by id."""
    settings = resolve_settings(args)
    store = CheckpointStore(settings.workspace)
    checkpoint = store.get(args.id)

    if checkpoint is None:
        err_console.print(
            f"[red]No checkpoint matching {args.id!r}.[/] "
            f"Run [cyan]jaigent checkpoints[/] to list them."
        )
        return 1
    return _restore(store, checkpoint, plain=bool(args.no_color))


def cmd_checkpoints(args: argparse.Namespace) -> int:
    """List the undo history for this workspace."""
    settings = resolve_settings(args)
    store = CheckpointStore(settings.workspace)

    if getattr(args, "clear", False):
        removed = store.clear()
        console.print(f"[green]{glyph('check')}[/] cleared {removed} checkpoint(s)")
        return 0

    checkpoints = store.history()
    if not checkpoints:
        console.print(
            f"[{MUTED}]No checkpoints yet. They are written automatically before the "
            f"agent changes a file.[/]"
        )
        return 0

    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=box.SIMPLE)
    table.add_column("ID", style=ACCENT, no_wrap=True)
    table.add_column("When", style=MUTED, no_wrap=True)
    table.add_column("Tool", style=MUTED, no_wrap=True)
    table.add_column("Files", overflow="ellipsis")

    for checkpoint in checkpoints:
        table.add_row(checkpoint.id, checkpoint.age(), checkpoint.tool, checkpoint.summary())
    console.print(table)

    size = store.size()
    console.print(
        f"[{MUTED}]{len(checkpoints)} checkpoint(s), {size / 1024:.1f} KB. "
        f"Revert with[/] [{ACCENT}]jaigent undo[/] [{MUTED}]or[/] "
        f"[{ACCENT}]jaigent rewind <id>[/]",
        highlight=False,
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose the installation: config, keys, storage and reachability."""
    return _run_doctor(resolve_settings(args), plain=bool(getattr(args, "no_color", False)))


def _run_doctor(settings: Settings, *, plain: bool) -> int:
    """Print the health report. Returns 1 when anything is wrong."""
    if not plain:
        console.print(render_logo(console, version=__version__))
        console.print()

    problems = 0

    def row(ok: bool, label: str, detail: str = "") -> None:
        nonlocal problems
        if not ok:
            problems += 1
        mark = glyph("check") if ok else glyph("cross")
        colour = "green" if ok else "red"
        console.print(f"  [{colour}]{mark}[/] {label:22} [{MUTED}]{detail}[/]", highlight=False)

    console.print(f"[bold {ACCENT}]Environment[/]")
    row(sys.version_info >= (3, 10), "python", f"{sys.version.split()[0]} on {sys.platform}")
    row(True, "jaigent", __version__)
    row(True, "config home", str(paths.user_home()))
    row(True, "workspace", str(settings.workspace))

    console.print(f"\n[bold {ACCENT}]Provider[/]")
    row(settings.provider in KNOWN_PROVIDERS, "provider", settings.provider)
    row(
        bool(settings.api_key),
        "api key",
        "set" if settings.api_key else "missing — run jaigent init",
    )
    row(True, "model", settings.model)

    chain = failover.available_providers(settings)
    row(
        len(chain) > 1 or not settings.failover,
        "failover",
        f"{len(chain)} provider(s) usable: {', '.join(chain[:4])}"
        if settings.failover
        else "disabled",
    )

    console.print(f"\n[bold {ACCENT}]Storage[/]")
    for label, path in (
        ("settings", settings_store.user_settings_path()),
        ("sessions", sessions.session_dir()),
        ("skills", dict(skills.skills_dirs())["project"]),
        ("checkpoints", checkpoint_dir(settings.workspace)),
    ):
        row(True, label, f"{path} {'' if path.exists() else '(not created yet)'}")

    writable = True
    try:
        paths.user_home().mkdir(parents=True, exist_ok=True)
        probe = paths.user_home() / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        writable = False
        row(False, "writable", str(exc))
    if writable:
        row(True, "writable", "config directory is writable")

    console.print(f"\n[bold {ACCENT}]Features[/]")
    row(True, "tools", f"{len(build_default_registry(settings))} available")
    row(True, "skills", f"{len(skills.discover())} defined")
    row(True, "commands", f"{len(commands.discover())} defined")
    row(True, "unicode", "yes" if supports_unicode() else "no — using ASCII fallbacks")

    if problems:
        console.print(f"\n[red]{problems} problem(s) found.[/] See above.\n")
        return 1
    console.print(f"\n[green]{glyph('check')} Everything looks healthy.[/]\n")
    return 0


def _print_status(agent: Agent, settings: Settings, session: sessions.Session) -> None:
    """A compact snapshot of the session, for /status."""
    cost = estimate(settings.model, session.usage)
    store = agent.checkpoints
    rows = [
        ("provider", settings.provider),
        ("model", settings.model),
        ("workspace", str(settings.workspace)),
        ("approval", settings.approval),
        ("session", session.id),
        ("messages", str(len(agent.history))),
        ("usage", cost.summary()),
        ("checkpoints", str(len(store.history(limit=1000))) if store else "disabled"),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        console.print(f"  [{MUTED}]{label:>{width}}[/]  {value}", highlight=False)


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
        "models": cmd_models,
        "settings": cmd_settings,
        "skills": cmd_skills,
        "schedule": cmd_schedule,
        "commands": cmd_commands,
        "keys": cmd_keys,
        "serve": cmd_serve,
        "route": cmd_route,
        "undo": cmd_undo,
        "checkpoints": cmd_checkpoints,
        "rewind": cmd_rewind,
        "doctor": cmd_doctor,
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
