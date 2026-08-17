"""Command line interface for jaigent.

Usage::

    jaigent "find the latest Python release and write it to notes.md"
    jaigent chat
    jaigent tools
    jaigent config
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from jaigent import __version__
from jaigent.agent import Agent
from jaigent.config import KNOWN_PROVIDERS, Settings
from jaigent.errors import ConfigurationError, JaigentError
from jaigent.tools import build_default_registry

console = Console()
err_console = Console(stderr=True)

BANNER = "[bold cyan]jaigent[/] [dim]v{version}[/]"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jaigent",
        description="An AI agent that searches the web and works with local files.",
        epilog="Bring your own API key: export OPENAI_API_KEY=... (or ANTHROPIC_API_KEY=...)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"jaigent {__version__}")

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

    sub = parser.add_subparsers(dest="command")

    run_cmd = sub.add_parser("run", parents=[common], help="Run a single task and exit.")
    run_cmd.add_argument("prompt", nargs="+", help="The task to perform.")

    sub.add_parser("chat", parents=[common], help="Start an interactive session.")
    sub.add_parser("tools", parents=[common], help="List the tools available to the agent.")
    sub.add_parser("config", parents=[common], help="Show the resolved configuration.")
    return parser


#: Recognised subcommands, used to detect the bare-prompt shorthand.
COMMANDS = ("run", "chat", "tools", "config")


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


def resolve_settings(args: argparse.Namespace) -> Settings:
    """Merge CLI flags over environment configuration."""
    settings = Settings.from_env()
    workspace = Path(args.workspace).expanduser() if getattr(args, "workspace", None) else None
    return settings.merged_with(
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
    )


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        err_console.print('[red]No prompt given.[/] Try: jaigent "summarise README.md"')
        return 2

    agent = Agent(settings)
    with console.status("[dim]thinking…[/]", spinner="dots") as status:
        if settings.verbose:
            status.stop()
        result = agent.run(prompt)

    _print_answer(result.output, plain=args.no_color)
    if settings.verbose:
        _print_trace(result)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    agent = Agent(settings)

    console.print(
        Panel(
            BANNER.format(version=__version__)
            + f"\n[dim]{settings.provider}/{settings.model} · workspace {settings.workspace}[/]"
            + "\n[dim]Type your request. /reset clears history, /exit quits.[/]",
            border_style="cyan",
        )
    )

    while True:
        try:
            prompt = console.input("[bold green]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/]")
            return 0

        if not prompt:
            continue
        if prompt in {"/exit", "/quit", "exit", "quit"}:
            console.print("[dim]bye[/]")
            return 0
        if prompt == "/reset":
            agent.reset()
            console.print("[dim]history cleared[/]")
            continue
        if prompt == "/tools":
            _print_tools(agent.tools)
            continue

        try:
            with console.status("[dim]thinking…[/]", spinner="dots") as status:
                if settings.verbose:
                    status.stop()
                result = agent.run(prompt)
            _print_answer(result.output, plain=args.no_color)
        except JaigentError as exc:
            err_console.print(f"[red]error:[/] {exc}")
        except KeyboardInterrupt:
            console.print("\n[dim]interrupted[/]")


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
def _print_answer(text: str, *, plain: bool = False) -> None:
    if not text:
        console.print("[dim](the model returned an empty answer)[/]")
        return
    if plain:
        print(text)
    else:
        console.print(Markdown(text))


def _print_tools(registry) -> None:  # noqa: ANN001 - ToolRegistry, avoids an import cycle in typing
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Tool", style="green", no_wrap=True)
    table.add_column("Description", overflow="fold")
    for tool in registry:
        name = f"{tool.name} [red]⚠[/]" if tool.dangerous else tool.name
        table.add_row(name, tool.description)
    console.print(table)


def _print_trace(result) -> None:  # noqa: ANN001 - AgentResult
    if not result.steps:
        return
    err_console.print(
        f"[dim]{result.tool_calls} tool call(s)"
        + (
            f" · {result.usage.get('total_tokens')} tokens"
            if result.usage.get("total_tokens")
            else ""
        )
        + ("[/] [yellow](step budget exhausted)[/]" if result.stopped_early else "[/]")
    )


# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(normalise_argv(raw))

    if getattr(args, "no_color", False):
        console.no_color = True

    if args.command is None:
        parser.print_help()
        return 0

    handlers = {"run": cmd_run, "chat": cmd_chat, "tools": cmd_tools, "config": cmd_config}
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
