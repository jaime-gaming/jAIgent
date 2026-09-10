"""Command line interface for jAIgent.

Usage::

    jaigent "find the latest Python release and write it to notes.md"
    jaigent chat
    jaigent tools
    jaigent config
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.box import ASCII as ASCII_BOX
from rich.box import ROUNDED as ROUNDED_BOX
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from jaigent import (
    __version__,
    commands,
    failover,
    feedback,
    gateway,
    models,
    paths,
    plugins,
    pricing,
    router,
    schedule,
    settings_store,
    skills,
    updater,
)
from jaigent import session as sessions
from jaigent.agent import Agent, AgentResult
from jaigent.approval import Approver, Mode
from jaigent.branding import (
    ACCENT,
    ACCENT_DIM,
    MUTED,
    render_banner,
    render_logo,
)
from jaigent.checkpoint import AmbiguousCheckpoint, CheckpointStore, checkpoint_dir
from jaigent.config import (
    API_KEY_ENV_VARS,
    APPROVAL_MODES,
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    KEY_URLS,
    KNOWN_PROVIDERS,
    LOCAL_PROVIDERS,
    Settings,
    key_for_provider,
)
from jaigent.errors import ConfigurationError, JaigentError, ToolError
from jaigent.pricing import estimate
from jaigent.tools import ToolRegistry, build_default_registry
from jaigent.ui import (
    Thinking,
    activity_line,
    glyph,
    phrase_for_tool,
    plan_lines,
    prompt_mark,
    result_line,
    supports_unicode,
    tool_line,
)

console = Console()
err_console = Console(stderr=True)


def _table_box():  # noqa: ANN202
    """Rounded tables when the console can draw them; ASCII otherwise."""
    return ROUNDED_BOX if supports_unicode() else ASCII_BOX


#: A chat slash command is ``/name`` or ``/name args``. A filesystem path such
#: as ``/tmp/notes.md`` is *not* a command — sending that to the model as a
#: slash would swallow a pasted path.
_SLASH_HEAD = re.compile(r"^/([a-z][a-z0-9_-]*)(\s|$)", re.I)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jaigent",
        description="All your agents in one place.",
        epilog="Bring your own API key: export OPENAI_API_KEY=... (or ANTHROPIC_API_KEY=...)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"jaigent {__version__}")
    parser.add_argument("--logo", action="store_true", help="Print the jAIgent logo and exit.")
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
    sub.add_parser(
        "providers",
        parents=[common],
        help="List providers and where to get an API key for each.",
    )

    sessions_cmd = sub.add_parser("sessions", parents=[common], help="List saved sessions.")
    sessions_cmd.add_argument(
        "--delete", metavar="ID", help="Delete a saved session by id, or 'all'."
    )
    sessions_cmd.add_argument(
        "--show",
        metavar="ID",
        help="Print the transcript of a saved session (id or prefix).",
    )
    sessions_cmd.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Show at most N sessions. Default: every saved session.",
    )

    init_cmd = sub.add_parser(
        "init", parents=[common], help="Set up jaigent interactively and write a .env file."
    )
    init_cmd.add_argument(
        "--force", action="store_true", help="Overwrite an existing .env without asking."
    )
    init_cmd.add_argument(
        "--no-dotenv",
        action="store_true",
        help="Store the key in ~/.jaigent/secrets.env only, not a project .env.",
    )

    # ---------------------------------------------------------------- models
    models_cmd = sub.add_parser("models", parents=[common], help="Browse known models.")
    models_cmd.add_argument("search", nargs="?", help="Filter by id, name or provider.")
    models_cmd.add_argument(
        "--only", dest="only_provider", metavar="PROVIDER", help="Show one provider only."
    )
    models_cmd.add_argument(
        "--free", action="store_true", help="Only show models that can be used at no cost."
    )
    models_cmd.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch the live model list from every provider you have a key for.",
    )
    models_cmd.add_argument(
        "--offline",
        action="store_true",
        help="Do not contact providers; show the catalogue and any cached list.",
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

    # -------------------------------------------------------------- plugins
    plugins_cmd = sub.add_parser("plugins", parents=[common], help="Manage local tool plugins.")
    plugins_sub = plugins_cmd.add_subparsers(dest="plugins_action")
    plugins_sub.add_parser("list", help="List available plugins.")

    new_plugin = plugins_sub.add_parser("new", help="Create a starter plugin.")
    new_plugin.add_argument("name")
    new_plugin.add_argument(
        "--user", action="store_true", help="Save to ~/.jaigent/plugins instead of the project."
    )

    remove_plugin = plugins_sub.add_parser("remove", help="Delete a plugin.")
    remove_plugin.add_argument("name")

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

    # ------------------------------------------------------------------ auth
    auth_cmd = sub.add_parser(
        "auth",
        parents=[common],
        help="Store a provider API key in ~/.jaigent/secrets.env (owner-only).",
    )
    auth_sub = auth_cmd.add_subparsers(dest="auth_action")
    auth_sub.add_parser("list", help="Show stored provider keys (masked).")
    auth_set = auth_sub.add_parser("set", help="Save a provider key.")
    auth_set.add_argument("provider", help="openai, anthropic, gemini, …")
    auth_set.add_argument("key", nargs="?", help="The secret. Omit to be prompted.")
    auth_unset = auth_sub.add_parser("unset", help="Remove a stored provider key.")
    auth_unset.add_argument("provider")

    # ------------------------------------------------------------------ keys
    keys_cmd = sub.add_parser("keys", parents=[common], help="Manage jAIgent API keys.")
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
    route_cmd.add_argument(
        "--free",
        action="store_true",
        help="Pick a free model from any provider you have a key for.",
    )

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

    # ---------------------------------------------------------------- update
    update_cmd = sub.add_parser(
        "update", parents=[common], help="Check for a new version and install it."
    )
    update_cmd.add_argument(
        "--check",
        action="store_true",
        help="Only report whether an update exists; install nothing.",
    )
    update_cmd.add_argument(
        "--force",
        action="store_true",
        help="Force reinstallation/upgrade even if already on the latest version.",
    )
    update_cmd.add_argument(
        "--beta",
        action="store_true",
        default=None,
        help="Install from the beta branch. `jaigent beta join` makes it permanent.",
    )
    update_cmd.add_argument(
        "--stable",
        action="store_true",
        help="Install from main even if the beta setting is on.",
    )

    # ---------------------------------------------------------------- beta
    beta_cmd = sub.add_parser("beta", parents=[common], help="Join or leave the beta channel.")
    beta_sub = beta_cmd.add_subparsers(dest="beta_action")
    beta_sub.add_parser("join", help="Get updates from the beta branch.")
    beta_sub.add_parser("leave", help="Go back to stable updates.")
    beta_sub.add_parser("status", help="Show whether the beta channel is on.")

    # ---------------------------------------------------------------- feedback
    feedback_cmd = sub.add_parser(
        "feedback", parents=[common], help="Send feedback to the maintainers."
    )
    feedback_cmd.add_argument(
        "message",
        nargs="*",
        help="What to send. Asked interactively when omitted.",
    )
    feedback_cmd.add_argument(
        "--no-open",
        action="store_true",
        help="Print the issue link instead of opening a browser.",
    )

    # ---------------------------------------------------------------- mcp
    mcp_cmd = sub.add_parser(
        "mcp",
        parents=[common],
        help="Start an MCP (Model Context Protocol) server over stdio for ChatGPT and Claude.",
    )
    mcp_cmd.add_argument(
        "--allow-write",
        action="store_true",
        default=None,
        help="Expose write tools (write_file, edit_file, delete_file) "
        "in addition to read-only ones.",
    )
    mcp_cmd.add_argument(
        "--client",
        choices=("generic", "claude", "chatgpt"),
        default="generic",
        help="Tune titles and the printed config snippet for a specific client.",
    )
    mcp_cmd.add_argument(
        "--print-config",
        choices=("claude", "chatgpt"),
        dest="print_config",
        help="Print a ready-to-paste Claude Desktop or ChatGPT connector snippet and exit.",
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
    "update",
    "tools",
    "config",
    "sessions",
    "init",
    "models",
    "settings",
    "skills",
    "plugins",
    "providers",
    "schedule",
    "mcp",
    "auth",
    "beta",
    "feedback",
)


#: Shared flags that take a value, so a leading one consumes the next token.
_VALUE_FLAGS = frozenset(
    {
        "--provider",
        "-m",
        "--model",
        "--api-key",
        "--base-url",
        "-w",
        "--workspace",
        "-s",
        "--max-steps",
        "-t",
        "--temperature",
        "--search-backend",
        "--retries",
    }
)

#: Top-level-only options; these are not shared with the subparsers.
_TOP_LEVEL_ONLY = frozenset({"-h", "--help", "--version", "--logo"})


def _split_leading_options(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split ``argv`` into leading shared options and the rest.

    Returns ``([], argv)`` when a top-level-only option comes first, so
    ``--help`` and ``--version`` keep working.
    """
    options: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _TOP_LEVEL_ONLY:
            return [], argv
        if not token.startswith("-"):
            break
        options.append(token)
        # "--workspace /tmp" needs its value moved too; "--workspace=/tmp" does not.
        if token in _VALUE_FLAGS and "=" not in token and index + 1 < len(argv):
            index += 1
            options.append(argv[index])
        index += 1
    return options, argv[index:]


def normalise_argv(argv: list[str]) -> list[str]:
    """Let ``jaigent "do the thing"`` mean ``jaigent run "do the thing"``.

    A leading token that is neither a known subcommand nor an option is treated
    as the start of a prompt.

    Shared options are also accepted *before* the subcommand, which is what most
    people type. argparse puts them on the subparser, so ``jaigent -w /tmp tools``
    would otherwise read ``/tmp`` as the command name and fail with a confusing
    "invalid choice" error.
    """
    if not argv:
        return argv

    first = argv[0]
    if first in COMMANDS:
        return argv

    if first.startswith("-"):
        options, rest = _split_leading_options(argv)
        if not options or not rest:
            return argv  # nothing to move, or options only
        command = rest[0] if rest[0] in COMMANDS else "run"
        remainder = rest[1:] if rest[0] in COMMANDS else rest
        return [command, *options, *remainder]

    # A bare prompt, or a custom slash command used straight from the shell.
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


def _resolve_workspace(raw: str | None) -> Path | None:
    """Validate ``--workspace`` up front rather than failing later.

    An unusable workspace otherwise surfaces as a confusing sandbox error on the
    first file tool call, long after the mistake was made.
    """
    if not raw:
        return None
    workspace = Path(raw).expanduser()
    if not workspace.exists():
        raise ConfigurationError(
            f"Workspace {workspace} does not exist. Create it first, or point "
            "--workspace somewhere that does."
        )
    if not workspace.is_dir():
        raise ConfigurationError(
            f"Workspace {workspace} is a file, not a directory. --workspace takes "
            "the directory the agent should work in."
        )
    return workspace


def resolve_settings(args: argparse.Namespace) -> Settings:
    """Merge CLI flags over environment configuration."""
    settings = Settings.from_env()
    workspace = _resolve_workspace(getattr(args, "workspace", None))

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
    return Agent(
        settings,
        on_text=sink,
        approver=approver,
        # ask_user renders on the same console as everything else, so its
        # question panel and the live status line never fight over the screen.
        tools=build_default_registry(settings, console=console),
    )


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
def _retry_summary(error: str) -> str:
    """The human reason a provider call is being retried: rate limit, wobble, …"""
    low = error.lower()
    if "http 429" in low or "rate limit" in low or "rate_limit" in low:
        return "hit a rate limit"
    if "http 408" in low or "timed out" in low or "timeout" in low:
        return "timed out"
    if "connection" in low or "could not reach" in low:
        return "couldn't be reached"
    if "overloaded" in low or "temporarily unavailable" in low:
        return "is overloaded"
    match = re.search(r"http (\d{3})", error)
    if match:
        return f"returned HTTP {match.group(1)}"
    first = error.strip().splitlines()[0] if error.strip() else "failed"
    return first[:80]


def run_turn(agent: Agent, settings: Settings, prompt: str, *, plain: bool) -> AgentResult:
    """Run one turn with a live status line, then print the footer.

    At any moment exactly one thing owns the screen: the status animation
    while the model thinks or a tool runs, a live markdown block while text
    streams, or a question panel while the user is asked something. Each one
    yields cleanly to the next, so the status line never vanishes mid-turn
    and streamed text is never interleaved with the animation.

    Every tool call leaves one quiet trace line behind — what it did, and
    whether it worked — so a turn reads as a record, not a long silence
    followed by an answer. Full argument dumps stay in ``--verbose``.

    Anything that asks the user a question (an approval diff, ``ask_user``)
    pauses the animation first: a spinner running under a prompt reads as a
    glitch, not as activity.

    Rate limits and provider switches are announced as they happen: failover
    used to be completely silent, so a slow turn looked identical to a stuck
    one and nobody knew which provider actually answered.
    """
    streaming = settings.stream and not plain
    status = Thinking(console, animate=not plain and not settings.verbose)
    printer = _StreamPrinter(console, status) if streaming else None

    def stream_started() -> bool:
        return printer is not None and printer.wrote

    def resume_status() -> None:
        # The spinner spins whenever no live answer block owns the screen —
        # including during tools that run after narration has streamed. It
        # used to stay off for the rest of the turn past the first token, so
        # a slow tool after a "Let me check…" left a frozen screen.
        if printer is not None and printer.live_active:
            return
        if not status.running:
            status.start()

    paused_for_prompt = False

    def pause_for_prompt() -> None:
        """Stop every animation: a question is about to own the screen."""
        nonlocal paused_for_prompt
        if printer is not None:
            printer.suspend()
        status.stop()
        paused_for_prompt = True

    def resume_after_prompt() -> None:
        nonlocal paused_for_prompt
        if paused_for_prompt:
            paused_for_prompt = False
            resume_status()

    def on_stream_boundary() -> None:
        """Narration streamed before a tool call ends here; the answer that
        follows is a new paragraph block, not a continuation."""
        if printer is None or not printer.wrote:
            return
        printer.separate()

    def announce(line: str) -> None:
        """Print a notice without fighting the animation or the stream.

        The live block is suspended first, so the notice lands between
        rendered paragraphs in chronological order instead of above them.
        """
        if printer is not None:
            printer.suspend()
        status.stop()
        console.print(line, highlight=False)
        resume_status()

    def on_tool_start(name: str, arguments: dict) -> None:
        # Name the tool while it runs. Doing this from on_tool_call meant the
        # verb only changed once the work was already finished.
        if name == "ask_user":
            # The question panel replaces the status line for as long as it
            # is on screen; anything animating underneath it would garble both.
            pause_for_prompt()
        else:
            if stream_started():
                on_stream_boundary()
            status.tool_started(name, arguments)
            # The spinner owns the screen again for the duration of the tool,
            # even when narration has already streamed this turn.
            resume_status()

    def on_tool(name: str, arguments: dict, output: str) -> None:
        if printer is not None:
            # The live block is done; whatever is printed next goes below it.
            printer.suspend()
        failed = output.startswith("ERROR")
        # Raw streams (pipes, --no-color) print chunks straight through with
        # no live region to suspend, so a trace line printed mid-stream would
        # land in the middle of the text — or the redirected file.
        trace_ok = not (printer is not None and printer.wrote and not printer.live_mode)
        if settings.verbose:
            status.stop()
            console.print(tool_line(name, _preview_args(arguments)))
            first = (output or "").splitlines()[0] if output else ""
            console.print(result_line(first[:150], ok=not failed))
            if name == "write_todos" and not failed and trace_ok:
                _print_todo_plan(arguments)
            resume_status()
        elif name == "write_todos" and not failed and trace_ok:
            # The live plan view replaces the trace line: the header carries
            # the same action and outcome, with the checklist underneath.
            _print_todo_plan(arguments)
        elif name != "ask_user" and trace_ok:
            # The quiet trace: one line per tool call, left above the answer.
            # ask_user leaves its own summary line instead.
            action, detail = phrase_for_tool(name, arguments)
            console.print(activity_line(action, detail, ok=not failed))
        # Back to Thinking before the line comes back, so a resumed status
        # never flashes the finished tool's phrase for one frame.
        status.thinking_again()
        resume_after_prompt()

    def on_route(routing) -> None:  # noqa: ANN001 - jaigent.router.Routing
        status.update(detail=routing.model)
        if settings.verbose:
            console.print(f"[{MUTED}]  {routing.summary()}[/]", highlight=False)

    announced: set[str] = set()
    failed_in_order: list[str] = []

    def on_failover(attempt) -> None:  # noqa: ANN001 - jaigent.failover.Attempt
        if attempt.provider not in failed_in_order:
            failed_in_order.append(attempt.provider)
        if attempt.provider in announced:
            return
        announced.add(attempt.provider)
        if attempt.retried:
            announce(f"[yellow]{attempt.provider} {_retry_summary(attempt.error)} — retrying…[/]")
        else:
            announce(
                f"[yellow]{attempt.provider} {_retry_summary(attempt.error)} — "
                "trying the next provider…[/]"
            )

    def on_provider(name: str) -> None:
        # Only a switch is worth mentioning: the primary answering first try
        # is the unremarkable case, and a provider answering after its own
        # retry was already announced above.
        if failed_in_order and name != failed_in_order[-1] and name not in announced:
            announced.add(name)
            announce(f"[{MUTED}]Continuing on {name}…[/]")

    agent.on_tool_start = on_tool_start
    agent.on_tool_call = on_tool
    agent.on_route = on_route
    agent.on_failover = on_failover
    agent.on_provider = on_provider
    agent.on_approval = lambda name, arguments: pause_for_prompt()
    agent.on_text = printer

    status.start()
    try:
        result = agent.run(prompt)
    finally:
        status.stop()
        if printer is not None:
            # A failed or interrupted turn leaves a live block mid-render;
            # settle it so the error lands below finished text, not inside it.
            printer.suspend()

    if printer is not None:
        printer.finish()
        if not printer.wrote and result.output:
            console.print()
            _print_answer(result.output, plain=plain)
    else:
        console.print()
        _print_answer(result.output, plain=plain)

    _print_footer(result, settings)
    _print_limit_panel(result, settings)
    console.print()
    return result


def _print_limit_panel(result: AgentResult, settings: Settings) -> None:
    """Explain an early stop: what hit the limit, and what to do next.

    The footer already names the limit in a few words; this is the version
    for someone who does not know what a step budget is.
    """
    if not result.stopped_early:
        return
    cap = float(getattr(settings, "budget", 0) or 0)
    if cap > 0 and result.cost.usd is not None and result.cost.usd >= cap:
        console.print(
            Panel(
                f"This run hit your ${cap:.2f} spend cap, so it stopped before "
                "spending more.\n"
                "Raise it with [cyan]jaigent settings set budget <amount>[/] "
                "(0 disables it), then ask again.",
                title="[yellow]Spend cap reached[/]",
                border_style="yellow",
            )
        )
        return
    console.print(
        Panel(
            f"I used all {settings.max_steps} tool steps before finishing.\n"
            "Try [cyan]/compact[/] to free context, break the task into smaller "
            "pieces, or raise the limit with [cyan]--max-steps[/].",
            title="[yellow]Out of steps[/]",
            border_style="yellow",
        )
    )


def friendly_error(exc: Exception, settings: Settings | None = None) -> tuple[str, str]:
    """Translate a run failure into plain language plus a next step.

    Returns ``(headline, advice)``; advice is empty when there is nothing
    useful to suggest. Configuration errors are already written for humans,
    so they pass through untouched.
    """
    from jaigent.errors import ConfigurationError

    text = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, ConfigurationError):
        return text, ""
    low = text.lower()
    provider = settings.provider if settings is not None else ""

    def key_advice() -> str:
        target = provider or "openai"
        where = KEY_URLS.get(target, "")
        get = f" (get one at {where})" if where else ""
        return (
            f"Check it with `jaigent auth list`, then store a fresh one: "
            f"`jaigent auth set {target} …`{get}"
        )

    if (
        "http 401" in low
        or "unauthorized" in low
        or "invalid api key" in low
        or "incorrect api key" in low
        or "authentication" in low
    ):
        return f"Your {provider or 'provider'} key was rejected.", key_advice()
    if "http 429" in low or "rate limit" in low or "rate_limit" in low:
        return (
            "The provider is rate-limiting requests.",
            "Wait a minute and try again. Adding another provider's key "
            "(`jaigent auth set anthropic …`) lets jAIgent switch over "
            "automatically next time.",
        )
    if "quota" in low or "billing" in low or "out of credit" in low or "insufficient" in low:
        return (
            "Your provider account is out of credit.",
            "Top up the account, or point jAIgent at a provider with credit "
            "(`jaigent auth set …`, then `/provider <name>`).",
        )
    if "http 404" in low or ("model" in low and "not found" in low):
        model = settings.model if settings is not None else ""
        return (
            f"The model {model!r} wasn't found." if model else "That model wasn't found.",
            f"See what's available: `jaigent models --only {provider}`."
            if provider
            else "See what's available: `jaigent models`.",
        )
    if (
        "context" in low
        or "too many tokens" in low
        or "input is too long" in low
        or "max_tokens" in low
    ):
        return (
            "The conversation grew too long for the model.",
            "Run /compact to shrink older turns, or /reset to start fresh.",
        )
    if (
        "timed out" in low
        or "timeout" in low
        or "connection" in low
        or "could not reach" in low
        or "temporarily unavailable" in low
        or "overloaded" in low
    ):
        return (
            "The provider couldn't be reached.",
            "Check your connection and try again.",
        )
    if "every provider failed" in low:
        return (
            "Every provider failed.",
            "Check your keys (`jaigent auth list`) and your connection, then try again.",
        )
    return text, ""


def _print_run_error(exc: JaigentError, settings: Settings | None) -> None:
    """A failed turn, explained like a person would explain it."""
    headline, advice = friendly_error(exc, settings)
    # Text, not markup: the headline can carry paths with brackets.
    err_console.print(Text(headline, style="red"))
    if advice:
        err_console.print(Text(advice, style=MUTED))
    if headline != str(exc).strip():
        detail = str(exc).strip().replace("\n", " ")
        if len(detail) > 300:
            detail = detail[:300] + "…"
        err_console.print(Text(f"Detail: {detail}", style="dim"))


def _preview_args(arguments: dict, limit: int = 70) -> str:
    """Compact one-line rendering of tool arguments for verbose mode."""
    parts = []
    for key, value in arguments.items():
        text = str(value).replace("\n", "\\n")
        parts.append(f"{key}={text[:32] + '…' if len(text) > 32 else text}")
    joined = " ".join(parts)
    return joined if len(joined) <= limit else joined[:limit] + "…"


def _print_todo_plan(arguments: dict) -> None:
    """The live task plan, printed every time ``write_todos`` runs."""
    todos = arguments.get("todos")
    if not isinstance(todos, list) or not todos:
        return
    rows = [item for item in todos if isinstance(item, dict)]
    if not rows:
        return
    for line in plan_lines(rows):
        console.print(line)


class _StreamPrinter:
    """Streams assistant text, rendering markdown live as it arrives.

    On a terminal with colour, chunks accumulate into a buffer that is
    re-rendered as markdown inside a live region several times a second —
    what the user watches is already formatted, so there is no raw-markup
    flash and no end-of-turn cursor walk-back to get wrong.

    A reply can stream narration and *then* make tool calls ("Let me check
    the files…", files are read, then the real answer streams). Each stretch
    of text is its own live block: starting a tool suspends the live region,
    leaving the rendered text on screen, and the next chunk starts a fresh
    block below a blank line. Tools, approval prompts and the status spinner
    therefore always own a clean screen.

    Anything that cannot render live — pipes, ``--no-color``,
    ``markdown=False`` — falls back to writing raw chunks straight through,
    with paragraph breaks between stretches. What lands in a redirected file
    is the source.
    """

    #: Seconds between live re-renders. Parsing markdown costs O(buffer) per
    #: render, so re-rendering every token would turn long answers quadratic.
    REFRESH_INTERVAL = 0.08

    def __init__(
        self, target: Console, status: Thinking | None = None, *, markdown: bool = True
    ) -> None:
        self.target = target
        self.status = status
        self.wrote = False
        self.markdown = markdown
        #: Whether chunks render live, or pass through raw.
        self.live_mode = bool(markdown and target.is_terminal and not target.no_color)
        self._parts: list[str] = []
        self._block: list[str] = []
        self._live: Live | None = None
        self._gap_before_next = False
        self._pending_separator = False
        self._last_render = 0.0

    @property
    def live_active(self) -> bool:
        """Whether a live answer block currently owns the screen."""
        return self._live is not None

    def separate(self) -> None:
        """End the current stretch: the next chunk starts a new paragraph block."""
        if self.live_mode:
            self.suspend()
        else:
            self._pending_separator = True

    def suspend(self) -> None:
        """Leave the rendered text on screen and free it for other output.

        Idempotent: safe to call when nothing is showing, and a no-op for
        raw streams, which own no region.
        """
        if self._live is None:
            return
        self._render(force=True)
        self._live.stop()
        self._live = None
        self._block = []
        self._gap_before_next = True

    def __call__(self, chunk: str) -> None:
        if not chunk:
            return
        if not self.live_mode:
            self._write_raw(chunk)
            return
        if self._live is None:
            if self.status is not None:
                self.status.stop()
            # Breathing room: after the prompt line for the first block, and
            # between blocks after that. Exactly one blank line in both cases.
            if not self.wrote or self._gap_before_next:
                self.target.print()
            self._gap_before_next = False
            self._live = Live(
                self._renderable(chunk),
                console=self.target,
                transient=False,
                refresh_per_second=12,
                # Taller than the window must scroll, not crop with an ellipsis.
                vertical_overflow="visible",
            )
            self._live.start()
            self._last_render = time.monotonic()
        self.wrote = True
        self._parts.append(chunk)
        self._block.append(chunk)
        # Newlines redraw immediately — a list or fence taking shape is the
        # interesting part — while mid-line tokens wait for the next tick.
        if "\n" in chunk:
            self._render(force=True)
        else:
            self._render()

    def _write_raw(self, chunk: str) -> None:
        """The fallback path: chunks straight through, no live region."""
        if not self.wrote:
            if self.status is not None:
                self.status.stop()
            # Breathing room between the user's line and the answer.
            self.target.file.write("\n")
        elif self._pending_separator:
            # Narration before a tool call, then the answer: keep them apart.
            self._pending_separator = False
            for piece in ("\n\n",):
                self._parts.append(piece)
                self.target.file.write(piece)
        self.wrote = True
        self._parts.append(chunk)
        self.target.file.write(chunk)
        self.target.file.flush()

    def _renderable(self, extra: str = "") -> Markdown:
        return _markdown("".join(self._block) + extra)

    def _render(self, *, force: bool = False) -> None:
        if self._live is None:
            return
        now = time.monotonic()
        if not force and now - self._last_render < self.REFRESH_INTERVAL:
            return
        self._last_render = now
        self._live.update(self._renderable(), refresh=True)

    @property
    def text(self) -> str:
        """Everything streamed so far."""
        return "".join(self._parts)

    def finish(self) -> None:
        if not self.wrote:
            return
        if not self.live_mode:
            self.target.file.write("\n")
            self.target.file.flush()
            return
        # Settle the last block: what is on screen is already the rendered
        # answer, so there is nothing to erase and redraw.
        self.suspend()


def looks_like_slash_command(text: str) -> bool:
    """Whether ``text`` is a ``/name`` command rather than a path or prompt.

    ``/help``, ``/model gpt-4o`` and ``/review the diff`` qualify. ``/tmp/a.md``
    and ``/home/user/notes`` do not — they are ordinary prompts.
    """
    stripped = (text or "").strip()
    if stripped in {"exit", "quit"}:
        return True
    if not stripped.startswith("/"):
        return False
    first = stripped.split()[0]
    if "/" in first[1:]:
        return False
    return bool(_SLASH_HEAD.match(stripped))


def _markdown(text: str) -> Markdown:
    """Rendered markdown with OSC-8 hyperlinks for ``[label](url)`` links."""
    return Markdown(text, hyperlinks=True, justify="left")


def _link(label: str, target: str) -> Text:
    """Clickable text. ``target`` is a URL or ``file://`` URI."""
    return Text(label, style=f"link {target}")


def _path_link(path: Path | str) -> Text:
    """A filesystem path the terminal can open on click."""
    resolved = Path(path).expanduser().resolve()
    try:
        uri = resolved.as_uri()
    except ValueError:
        uri = str(resolved)
    return _link(str(path), uri)


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

    if looks_like_slash_command(prompt):
        expanded = expand_command(prompt, settings)
        if expanded == prompt and commands.resolve(prompt) is None:
            known = ", ".join(f"/{n}" for n in sorted(commands.discover())) or "(none defined)"
            err_console.print(f"[red]Unknown command {prompt.split()[0]}.[/] Available: {known}")
            return 1
        prompt = expanded

    agent = build_agent(settings)
    run_turn(agent, settings, prompt, plain=bool(args.no_color))
    return 0


#: The chat commands, in help-table order. ``/help`` renders these as two
#: clean columns; the notes below the table are the prose part.
CHAT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "show this list"),
    ("/reset", "clear the conversation"),
    ("/tools", "list available tools"),
    ("/model <name>", "switch model for the rest of the session"),
    ("/provider <name>", "switch provider (and its key) for the session"),
    ("/key [provider] [key]", "store a provider API key (prompted if omitted)"),
    ("/workspace <path>", "point the file tools somewhere else"),
    ("/cost", "show tokens and spend for this session"),
    ("/save", "write the session to disk now"),
    ("/undo", "drop the last exchange"),
    ("/revert", "undo the agent's last file change on disk"),
    ("/checkpoints", "list restorable file checkpoints"),
    ("/rewind <id>", "restore a checkpoint by id"),
    ("/diff", "show what the last change would revert"),
    ("/status", "provider, model, workspace and session at a glance"),
    ("/approve <mode>", "ask, auto or dry-run"),
    ("/commands", "list custom commands"),
    ("/doctor", "check keys, storage and providers"),
    ("/compact", "shrink older turns into a short summary"),
    ("/memory", "show project memory (off unless settings.memory)"),
    ("/settings", "show the live session settings"),
    ("/sessions", "list saved chats (newest first)"),
    ("/resume <id>", "switch this REPL to an old session"),
    ("/exit", "quit"),
)

HELP_NOTES = """\
Custom commands from .jaigent/commands are available too — /commands to see them.

End a line with \\\\ to keep typing. Empty Enter does not send. Paths like
/tmp/notes.md are prompts, not commands."""


def _print_help() -> None:
    """The chat command list: aligned columns, then the fine print."""
    table = Table(
        show_header=False,
        box=None,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        show_edge=False,
    )
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    for name, description in CHAT_COMMANDS:
        # Text, not markup: command names contain [provider]-style brackets,
        # which rich would otherwise swallow as style tags.
        table.add_row(Text(name, style=f"bold {ACCENT}"), Text(description, style=MUTED))
    console.print(table)
    console.print()
    console.print(HELP_NOTES, highlight=False, style=MUTED)


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
        try:
            settings = settings.merged_with(**_session_overrides(session, args, settings))
        except ConfigurationError as exc:
            err_console.print(f"[red]{exc}[/]")
            return 78

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

    # Keep the opening screen welcoming. The provider, model, workspace and
    # approval policy are still available through the explicit /settings and
    # /status commands, but they are implementation details rather than a
    # welcome message.
    console.print(render_banner(console, version=__version__))
    if args.resume:
        console.print(
            f"[{MUTED}]resumed {session.id} · {session.turns} turn(s) · "
            f"{session.title or 'untitled'}[/]",
            highlight=False,
        )
        _print_transcript(session, last=8)
    console.print(f"[{MUTED}]Ready when you are. Ask me to read, explain or update your files.[/]")
    console.print(
        Text.assemble(
            ("Type ", MUTED),
            ("/help", f"bold {ACCENT}"),
            (" for commands · Ctrl-D or ", MUTED),
            ("/exit", f"bold {ACCENT}"),
            (" to leave.", MUTED),
        )
    )
    console.print()

    # A session is intentionally not written after every turn. This makes the
    # close prompt meaningful: the user decides whether this conversation is
    # kept, instead of a hidden auto-save defeating the choice.
    dirty = False

    restore_close_handlers = _install_close_handlers(session, agent, saving)
    try:
        while True:
            try:
                prompt = _read_chat_prompt()
            except (EOFError, KeyboardInterrupt):
                _finish_chat(session, agent, saving, dirty=dirty)
                return 0

            if not prompt:
                continue

            if looks_like_slash_command(prompt):
                outcome = _handle_slash(prompt, agent, settings, session)
                if outcome.quit:
                    _finish_chat(session, agent, saving, dirty=dirty)
                    return 0
                if outcome.session is not None:
                    session = outcome.session
                    dirty = False
                if outcome.saved:
                    dirty = False
                if outcome.changed:
                    dirty = True
                if outcome.settings is not None:
                    settings = outcome.settings
                if outcome.prompt:
                    session.set_title_from(outcome.prompt)
                    try:
                        result = run_turn(
                            agent, settings, outcome.prompt, plain=bool(args.no_color)
                        )
                        session.touch(agent.history, result.usage)
                        dirty = True
                    except JaigentError as exc:
                        _print_run_error(exc, settings)
                    except KeyboardInterrupt:
                        # Mirror the normal branch: without this, Ctrl-C during
                        # a custom-command run escaped to main() and quit the
                        # chat without offering to save.
                        console.print(f"\n[{MUTED}]interrupted[/]")
                continue

            session.set_title_from(prompt)
            try:
                result = run_turn(agent, settings, prompt, plain=bool(args.no_color))
                session.touch(agent.history, result.usage)
                dirty = True
            except JaigentError as exc:
                _print_run_error(exc, settings)
            except KeyboardInterrupt:
                console.print(f"\n[{MUTED}]interrupted[/]")
    finally:
        restore_close_handlers()


def _session_overrides(
    session: sessions.Session, args: argparse.Namespace, settings: Settings
) -> dict[str, object]:
    """Settings to adopt when starting chat on a saved session.

    Precedence is explicit flags first, then the session, then everything
    else: ``--resume x --model foo`` used to start on the session's model,
    silently ignoring the flag. Provider and model travel together, the base
    URL only follows when the provider actually changes (a custom
    ``--base-url`` survives resuming), and switching to a provider with no
    key explains itself instead of reusing the old backend's key.
    """
    updates: dict[str, object] = {}
    explicit_provider = getattr(args, "provider", None)
    adopt_provider = bool(session.provider) and explicit_provider is None
    effective = session.provider if adopt_provider else settings.provider
    # The session's model only makes sense on its own backend: with
    # `--provider` overriding the backend, a stored claude id would 404.
    if (
        session.model
        and getattr(args, "model", None) is None
        and session.provider in ("", effective)
    ):
        updates["model"] = session.model
    if adopt_provider and session.provider != settings.provider:
        updates["provider"] = session.provider
        default_url = DEFAULT_BASE_URLS.get(session.provider)
        if default_url and getattr(args, "base_url", None) is None:
            updates["base_url"] = default_url
        key = key_for_provider(session.provider)
        if key:
            updates["api_key"] = key
        else:
            raise ConfigurationError(
                f"This session ran on {session.provider!r}, but no API key is "
                f"stored for it.\n  Resume on {settings.provider!r} instead: "
                f"jaigent chat --resume {getattr(args, 'resume', 'last')} "
                f"--provider {settings.provider}\n  Or store a key: "
                f"jaigent auth set {session.provider} <key>"
            )
    if session.workspace and getattr(args, "workspace", None) is None:
        workspace = Path(session.workspace)
        if workspace.is_dir():
            updates["workspace"] = workspace
    return updates


def _install_close_handlers(
    session: sessions.Session, agent: Agent, saving: bool
) -> Callable[[], None]:
    """Auto-save the session if the terminal itself goes away (SIGHUP/SIGTERM).

    Asking is impossible — the terminal is already gone — so an unsaved
    conversation is kept quietly rather than lost. Closing the window and
    finding the chat under ``jaigent sessions`` beats retyping it.
    Returns a function that restores the previous handlers.
    """
    import signal as _signal

    previous: dict[int, Any] = {}
    if not saving:
        return lambda: None

    def _save_and_exit(signum: int, frame: Any) -> None:
        with contextlib.suppress(Exception):
            if agent.history:
                session.touch(agent.history)
                session.save()
        raise SystemExit(128 + int(signum))

    for name in ("SIGHUP", "SIGTERM"):
        number = getattr(_signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = _signal.getsignal(number)
            _signal.signal(number, _save_and_exit)
        except (OSError, ValueError, RuntimeError):
            continue

    def restore() -> None:
        for number, handler in previous.items():
            with contextlib.suppress(Exception):
                _signal.signal(number, handler)

    return restore


@dataclass(slots=True)
class SlashResult:
    """What the REPL should do after an in-chat command."""

    quit: bool = False
    settings: Settings | None = None
    #: A custom command expanded into a prompt the agent should now run.
    prompt: str | None = None
    #: Swap the live conversation for another saved session.
    session: sessions.Session | None = None
    #: The command changed session data that should be offered at close.
    changed: bool = False
    #: ``/save`` wrote the current state; clear the close prompt.
    saved: bool = False


def _handle_slash(  # noqa: C901 - a dispatch table reads better than many functions
    prompt: str, agent: Agent, settings: Settings, session: sessions.Session
) -> SlashResult:
    """Run an in-chat command and say whether to quit or adopt new settings."""
    command, _, argument = prompt.partition(" ")
    command = command.lower()
    argument = argument.strip()
    changed = False
    saved = False

    if command in {"/exit", "/quit", "exit", "quit"}:
        return SlashResult(quit=True)

    if command == "/help":
        _print_help()
    elif command == "/reset":
        agent.reset()
        session.messages = []
        changed = True
        console.print(f"[{MUTED}]conversation cleared[/]")
    elif command == "/tools":
        _print_tools(agent.tools)
    elif command == "/cost":
        cost = estimate(settings.model, session.usage)
        console.print(f"[{MUTED}]session total: {cost.summary()}[/]", highlight=False)
    elif command == "/save":
        session.touch(agent.history)
        path = session.save()
        saved = True
        console.print(f"[{MUTED}]saved to {path}[/]", highlight=False)
    elif command == "/undo":
        removed = _undo(agent)
        session.messages = agent.history
        changed = removed
        console.print(
            f"[{MUTED}]{'dropped the last exchange' if removed else 'nothing to undo'}[/]"
        )
    elif command == "/model":
        if not argument:
            console.print(f"[{MUTED}]current model: {settings.model}[/]", highlight=False)
            return SlashResult()
        agent.set_model(argument)
        session.model = argument
        console.print(f"[{MUTED}]model is now {argument}[/]", highlight=False)
        return SlashResult(settings=agent.settings, changed=True)
    elif command == "/provider":
        if not argument:
            console.print(
                f"[{MUTED}]current provider: {settings.provider}  "
                f"(one of: {', '.join(KNOWN_PROVIDERS)})[/]",
                highlight=False,
            )
            return SlashResult()
        try:
            agent.set_provider(argument)
        except ConfigurationError as exc:
            err_console.print(f"[red]{exc}[/]")
            return SlashResult()
        session.provider = agent.settings.provider
        session.model = agent.settings.model
        console.print(
            f"[{MUTED}]provider is now {agent.settings.provider} ({agent.settings.model})[/]",
            highlight=False,
        )
        return SlashResult(settings=agent.settings, changed=True)
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
        return SlashResult(settings=updated, changed=True)
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
        store.discard(checkpoint)
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
        try:
            checkpoint = store.get(argument)
        except AmbiguousCheckpoint as exc:
            err_console.print(f"[red]{exc}[/]")
            return SlashResult()
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
        for changed_path, action in rows:
            console.print(f"  [{MUTED}]{action:>9}[/]  {changed_path}", highlight=False)
    elif command == "/status":
        _print_status(agent, settings, session)
    elif command == "/settings":
        _print_live_settings(settings)
    elif command == "/sessions":
        _print_sessions_table(sessions.list_sessions())
    elif command == "/resume":
        return _slash_resume(argument, agent, settings, session)
    elif command == "/key":
        return _slash_key(argument, agent, settings)
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
        return SlashResult(settings=updated, changed=True)
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
    elif command == "/compact":
        dropped = agent.compact()
        session.messages = agent.history
        if dropped:
            changed = True
            console.print(f"[{MUTED}]compacted {dropped} older message(s)[/]")
        else:
            console.print(f"[{MUTED}]nothing to compact[/]")
    elif command == "/memory":
        if not settings.memory:
            console.print(
                f"[{MUTED}]memory is off. Turn it on with[/] "
                f"[{ACCENT}]jaigent settings set memory true[/]",
                highlight=False,
            )
            return SlashResult()
        from jaigent.memory import load_memory

        notes = load_memory(settings.workspace).strip()
        console.print(notes or f"[{MUTED}]memory is empty[/]")
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
    return SlashResult(changed=changed, saved=saved)


def _undo(agent: Agent) -> bool:
    """Remove the most recent user turn and everything after it."""
    indices = [i for i, m in enumerate(agent.history) if m.get("role") == "user"]
    if not indices:
        return False
    agent.history = agent.history[: indices[-1]]
    return True


def _finish_chat(
    session: sessions.Session,
    agent: Agent,
    saving: bool,
    *,
    dirty: bool | None = None,
) -> None:
    """Leave chat, offering to keep an unsaved conversation.

    EOF and Ctrl-D are the terminal's normal "close" signal for this REPL. A
    real desktop pop-up cannot be shown after the terminal window has already
    been killed, so the confirmation is deliberately rendered in the terminal
    while it is still available. ``dirty`` is optional for callers outside the
    REPL; the interactive loop passes it explicitly.
    """
    if dirty is None:
        dirty = bool(agent.history) or session.path.is_file()

    if not saving or not dirty:
        console.print(f"\n[{MUTED}]bye[/]")
        return

    console.print("\n[yellow]You have an unsaved conversation.[/]", highlight=False)
    try:
        answer = console.input("Save it before leaving? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in {"", "y", "yes"}:
        session.touch(agent.history)
        session.save()
        console.print(f"[{MUTED}]session saved as {session.id}[/]", highlight=False)
    else:
        console.print(f"[{MUTED}]changes discarded; bye[/]", highlight=False)


def cmd_sessions(args: argparse.Namespace) -> int:
    """List, show, or delete saved conversations."""
    target = getattr(args, "delete", None)
    if target:
        if target == "all":
            removed = 0
            for saved_session in sessions.list_sessions():
                removed += int(saved_session.delete())
            console.print(f"[{MUTED}]deleted {removed} session(s)[/]")
            return 0
        found = sessions.resolve(target)
        if found is None or not found.delete():
            err_console.print(f"[red]No session matching {target!r}.[/]")
            return 1
        console.print(f"[{MUTED}]deleted {found.id}[/]")
        return 0

    show = getattr(args, "show", None)
    if show:
        found = sessions.resolve(show)
        if found is None:
            err_console.print(
                f"[red]No session matching {show!r}.[/] "
                f"Run [{ACCENT}]jaigent sessions[/] to see what is saved."
            )
            return 1
        console.print(
            f"[bold {ACCENT}]{found.id}[/]  [{MUTED}]{found.title or 'untitled'} · "
            f"{found.turns} turn(s) · {found.age()}[/]",
            highlight=False,
        )
        _print_transcript(found)
        console.print(
            f"\n[{MUTED}]Resume with[/] [{ACCENT}]jaigent chat --resume {found.id}[/]",
            highlight=False,
        )
        return 0

    saved = sessions.list_sessions(limit=getattr(args, "limit", None))
    if not saved:
        console.print(
            f"[{MUTED}]No saved sessions yet. Start one with[/] [{ACCENT}]jaigent chat[/]",
            highlight=False,
        )
        return 0

    _print_sessions_table(saved)
    console.print(
        f"[{MUTED}]Open one with[/] [{ACCENT}]jaigent chat --resume <id>[/]"
        f"[{MUTED}]  ·  read it with[/] [{ACCENT}]jaigent sessions --show <id>[/]"
        f"[{MUTED}]  ·  in chat:[/] [{ACCENT}]/resume <id>[/]",
        highlight=False,
    )
    return 0


def _print_sessions_table(saved: list) -> None:  # noqa: ANN001
    """Render the session catalogue. Shared by ``jaigent sessions`` and ``/sessions``."""
    if not saved:
        console.print(f"[{MUTED}]No saved sessions yet.[/]")
        return
    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
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


def _print_transcript(session: sessions.Session, *, last: int | None = None) -> None:
    """Print user/assistant turns. ``last`` keeps only the newest N pairs."""
    rows = session.transcript()
    if last is not None:
        rows = rows[-last:]
    if not rows:
        console.print(f"[{MUTED}](empty transcript)[/]")
        return
    for role, text in rows:
        label = "you" if role == "user" else "jAI"
        style = ACCENT if role == "user" else MUTED
        console.print(f"[bold {style}]{label}[/]", highlight=False)
        preview = text if last is None else (text if len(text) <= 1200 else text[:1200] + "…")
        if role == "assistant" and last is None:
            console.print(_markdown(preview))
        else:
            console.print(Text(preview, style=MUTED))
        console.print()


def _slash_resume(
    argument: str, agent: Agent, settings: Settings, current: sessions.Session
) -> SlashResult:
    """``/resume <id>`` — load another saved chat into this REPL."""
    if not argument:
        _print_sessions_table(sessions.list_sessions())
        console.print(f"[{MUTED}]usage: /resume <id>[/]")
        return SlashResult()
    found = sessions.resolve(argument)
    if found is None:
        err_console.print(f"[red]No session matching {argument!r}.[/]")
        return SlashResult()
    if found.id == current.id:
        console.print(f"[{MUTED}]already in {current.id}[/]")
        return SlashResult()
    if agent.history:
        current.touch(agent.history)
        current.save()
    if found.provider and found.provider != agent.settings.provider:
        # set_provider rebuilds the owned provider; assigning settings by
        # hand left the old backend answering while /status named the new
        # one. Without a key for that backend the chat stays where it is —
        # the conversation is what is being resumed, not the billing.
        try:
            agent.set_provider(found.provider)
        except ConfigurationError:
            if found.provider.strip().lower() not in KNOWN_PROVIDERS:
                reason = f"unknown provider {found.provider!r}"
            else:
                reason = f"no usable key for {found.provider!r}"
            console.print(
                f"[{MUTED}]{reason} — staying on "
                f"{agent.settings.provider} ({agent.settings.model})[/]",
                highlight=False,
            )
        else:
            settings = agent.settings
    if found.model and (not found.provider or found.provider == agent.settings.provider):
        # Only the session's own backend can run its model; after a failed
        # provider switch the current model stays too.
        agent.set_model(found.model)
        settings = agent.settings
    if found.workspace:
        workspace = Path(found.workspace)
        if workspace.is_dir() and workspace != agent.settings.workspace:
            settings = agent.settings.merged_with(workspace=workspace)
            agent.settings = settings
            agent.tools = build_default_registry(settings, console=console)
            agent.approver.workspace = settings.workspace
    agent.load_history(found.messages)
    console.print(
        f"[{MUTED}]resumed {found.id} · {found.turns} turn(s) · {found.title or 'untitled'}[/]",
        highlight=False,
    )
    _print_transcript(found, last=6)
    return SlashResult(settings=settings, session=found)


def _clean_secret(raw: str | None) -> str:
    """Strip quotes and a Bearer prefix that people often paste with a key."""
    key = (raw or "").strip()
    for quote in ("'", '"'):
        if len(key) >= 2 and key.startswith(quote) and key.endswith(quote):
            key = key[1:-1].strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def _read_key(prompt: str) -> str:
    """Read a secret from the terminal.

    Input is visible on purpose: hidden ``password=True`` prompts swallow
    pastes on many consoles (Windows Terminal, some multiplexers), which
    made ``jaigent init`` look like it refused the key.
    """
    try:
        return _clean_secret(console.input(f"[{ACCENT}]{prompt}:[/] "))
    except (EOFError, KeyboardInterrupt):
        return ""


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
        where = KEY_URLS.get(name) or "no key needed"
        def_mod = DEFAULT_MODELS.get(name, "")
        console.print(f"   [{ACCENT}]{index}[/]  {name:<12}  [{MUTED}]{def_mod}  {where}[/]")
    console.print()

    try:
        choice = console.input(f"[{ACCENT}]provider [1]:[/] ").strip() or "1"
    except EOFError:
        err_console.print("[red]No input available. Run `jaigent init` in a terminal.[/]")
        return 1
    try:
        # A bare index wraps: "0" used to silently select the *last* provider.
        index = int(choice) - 1
        if not 0 <= index < len(KNOWN_PROVIDERS):
            raise IndexError(choice)
        provider = KNOWN_PROVIDERS[index]
    except (ValueError, IndexError):
        if choice in KNOWN_PROVIDERS:
            provider = choice
        else:
            provider = KNOWN_PROVIDERS[0]
            # Text, not markup: the user's answer can contain square brackets.
            console.print(
                Text(f"! {choice} is not a provider — using {provider} (1)."),
                style="yellow",
            )

    key_var = API_KEY_ENV_VARS.get(provider, "JAIGENT_API_KEY")
    if provider in LOCAL_PROVIDERS:
        console.print(f"\n[bold {ACCENT}]2.[/] {provider} runs locally and needs no API key.")
        api_key = "jaigent-local"
    else:
        console.print(f"\n[bold {ACCENT}]2.[/] Paste your {provider} API key.")
        key_url = KEY_URLS.get(provider)
        if key_url:
            console.print(Text.assemble(("   Get one at ", MUTED), _link(key_url, key_url)))
        console.print(f"   [{MUTED}]It is written to .env, which is git-ignored.[/]\n")

        cli_key = getattr(args, "api_key", None)
        api_key = _clean_secret(cli_key) if cli_key else _read_key(key_var)
        if not api_key:
            console.print(f"[{MUTED}]Nothing was pasted - one more try.[/]")
            api_key = _read_key(key_var)
        if not api_key:
            err_console.print("[red]No key entered. Run jaigent init again when you have one.[/]")
            return 1

    default_model = DEFAULT_MODELS.get(provider, "gpt-4o-mini")
    console.print(f"\n[bold {ACCENT}]3.[/] Which model?")
    # Text, not markup: the default is shown in [brackets] that rich would eat.
    try:
        model = (
            console.input(Text(f"model [{default_model}]: ", style=ACCENT)).strip() or default_model
        )
    except EOFError:
        err_console.print("[red]No input available. Run `jaigent init` in a terminal.[/]")
        return 1

    if model != default_model and model not in {
        entry.id for entry in models.for_provider(provider)
    }:
        # Text, not markup: the model id is user input and may hold brackets.
        console.print(Text(f"'{model}' is not in the {provider} catalogue."), style="yellow")
        if _confirm("Use it anyway?"):
            console.print(f"   [{MUTED}]A custom model needs a base URL that serves it.[/]")
        else:
            console.print(f"   [{MUTED}]Using {default_model} instead.[/]")
            model = default_model

    from jaigent.secrets import set_key as store_provider_key

    if provider not in LOCAL_PROVIDERS:
        secret_path = store_provider_key(provider, api_key)
        console.print(
            f"\n[green]{glyph('check')}[/] stored {key_var} in {secret_path} [dim](owner-only)[/]"
        )

    write_dotenv = not getattr(args, "no_dotenv", False) and paths.can_write_project_dotenv(
        env_path.parent
    )
    if not getattr(args, "no_dotenv", False) and not write_dotenv:
        console.print(
            f"[yellow]![/] this folder ({env_path.parent}) is not a place to write "
            f".env — key stays in the user secrets file. "
            f"Run [cyan]jaigent init[/] from your project directory for a local .env."
        )
        try:
            settings_store.set_value("provider", provider, scope="user")
            settings_store.set_value("model", model, scope="user")
        except ConfigurationError:
            pass

    if write_dotenv:
        lines = [
            "# Written by `jaigent init`. This file is git-ignored — never commit it.",
            f"JAIGENT_PROVIDER={provider}",
            f"JAIGENT_MODEL={model}",
            f"{key_var}={api_key}",
            "",
        ]
        try:
            paths.write_private(env_path, "\n".join(lines))
        except OSError as exc:
            console.print(
                f"[yellow]![/] could not write {env_path}: {exc}. "
                f"The key is already in the user secrets file."
            )
        else:
            console.print(f"[green]{glyph('check')}[/] wrote {env_path} [dim](owner-only)[/]")
    elif provider in LOCAL_PROVIDERS:
        console.print()

    console.print(f"\n[bold {ACCENT}]4.[/] Testing the key…")
    settings = Settings(
        provider=provider,
        model=model,
        api_key=api_key,
        # Honour a gateway URL if one is already configured.
        base_url=os.getenv("JAIGENT_BASE_URL") or DEFAULT_BASE_URLS.get(provider, ""),
        max_steps=1,
    )
    try:
        agent = Agent(settings, tools=ToolRegistry())
        reply = agent.run("Reply with exactly: ready")
        console.print(
            f"[green]{glyph('check')}[/] {provider} responded: [{MUTED}]{reply.output[:60]}[/]"
        )
        if reply.cost.usd is not None:
            console.print(f"[{MUTED}]  that test cost about {reply.cost.format_usd()}[/]")
    except JaigentError as exc:
        err_console.print(f"[yellow]![/] the key was saved but the test call failed:\n  {exc}")
        return 1

    console.print(f"\n[bold {ACCENT}]You're set.[/] Try:\n")
    console.print(f'   [{ACCENT}]jaigent "summarise the files in this folder"[/]')
    console.print(f"   [{ACCENT}]jaigent chat[/]\n")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    """List every provider and where to mint a key for it."""
    del args
    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
    table.add_column("Provider", style=ACCENT, no_wrap=True)
    table.add_column("Env var", style=MUTED, no_wrap=True)
    table.add_column("Default model", style=MUTED, no_wrap=True)
    table.add_column("Get a key", overflow="fold")
    for name in KNOWN_PROVIDERS:
        url = KEY_URLS.get(name) or "(local, no key)"
        key_env = API_KEY_ENV_VARS.get(name, "JAIGENT_API_KEY")
        def_model = DEFAULT_MODELS.get(name, "")
        cell = _link(url, url) if url.startswith("http") else Text(url, style=MUTED)
        table.add_row(name, key_env, def_model, cell)
    console.print(table)
    console.print(
        f"[{MUTED}]Pick one with[/] [{ACCENT}]--provider[/][{MUTED}] or[/] "
        f"[{ACCENT}]jaigent init[/][{MUTED}]. OpenRouter is the usual "
        f"one-key-many-models option.[/]",
        highlight=False,
    )
    return 0


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
    live: list = []
    if getattr(args, "refresh", False) and not getattr(args, "offline", False):
        spinner = (
            console.status("Gathering models...", spinner="dots")
            if not getattr(args, "no_color", False)
            else nullcontext()
        )
        with spinner:
            live = models.gather_available()
        if not live:
            console.print(f"[{MUTED}]No live models returned. Showing the catalogue.[/]")
    pool = models.combined(live=live)
    entries = models.search(args.search) if args.search else list(pool)
    if args.search:
        needle = args.search.strip().lower()
        entries = [
            m
            for m in pool
            if needle in m.id.lower() or needle in m.label.lower() or needle in m.provider.lower()
        ]
    if getattr(args, "only_provider", None):
        wanted = args.only_provider.strip().lower()
        entries = [m for m in entries if m.provider == wanted]
    if getattr(args, "free", False):
        entries = [m for m in entries if m.free]

    if not entries:
        console.print(f"[{MUTED}]No models match that filter.[/]")
        return 1

    settings = resolve_settings(args)
    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
    table.add_column("Model", style=ACCENT, no_wrap=True)
    table.add_column("Provider", style=MUTED, no_wrap=True)
    table.add_column("Context", style=MUTED, no_wrap=True)
    table.add_column("Notes", overflow="fold")

    for model in entries:
        price = pricing.price_for(model.id)
        note = model.note
        if model.free:
            note = f"free · {note}".strip(" ·")
        if price and not model.free:
            note = f"{note} · ${price[0]:g}/${price[1]:g} per Mtok".strip(" ·")
        marker = f" {glyph('arrow_left')}" if model.id == settings.model else ""
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
        user = settings_store.user_settings_path()
        project = settings_store.project_settings_path()
        console.print(Text.assemble(("user:    ", MUTED), _path_link(user)))
        console.print(Text.assemble(("project: ", MUTED), _path_link(project)))
        return 0

    if action == "set":
        path = settings_store.set_value(args.key, args.value, scope=scope)
        # Text, not markup: the value and path are user-controlled and can
        # hold brackets rich would swallow.
        console.print(
            Text.assemble(
                (f"{glyph('check')} ", "green"),
                f"{args.key} = {args.value}  ",
                (f"({scope}: {path})", MUTED),
            )
        )
        return 0

    if action == "unset":
        if settings_store.unset_value(args.key, scope=scope):
            console.print(f"[green]{glyph('check')}[/] removed {args.key} from {scope} settings")
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

    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
    table.add_column("Setting", style=ACCENT, no_wrap=True)
    table.add_column("Value", overflow="fold")
    table.add_column("From", style=MUTED, no_wrap=True)
    for key, value, source in rows:
        table.add_row(key, str(value), source)
    console.print(table)
    console.print(
        f"[{MUTED}]Precedence: CLI flags {glyph('arrow')} environment "
        f"{glyph('arrow')} project file {glyph('arrow')} user file "
        f"{glyph('arrow')} defaults.[/]"
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

        table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
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
                _markdown(found_skill.body.strip()),
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
        console.print(f"[green]{glyph('check')}[/] created {path}")
        if not args.body:
            console.print(f"[{MUTED}]Edit it to describe the procedure.[/]")
        return 0

    if action == "remove":
        doomed = available.get(args.name.strip().lower())
        if doomed is None:
            err_console.print(f"[red]No skill named {args.name!r}.[/]")
            return 1
        if doomed.scope == "builtin":
            err_console.print("[red]Cannot remove a skill that ships with jAIgent.[/]")
            return 1
        doomed.path.unlink()
        console.print(f"[green]{glyph('check')}[/] removed {doomed.path}")
        return 0

    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    """List, create and delete local tool plugins."""
    action = getattr(args, "plugins_action", None) or "list"
    available = plugins.discover()

    if action == "list":
        if not available:
            console.print(
                f"[{MUTED}]No plugins yet. Create one with[/] "
                f"[{ACCENT}]jaigent plugins new hello[/]",
                highlight=False,
            )
            return 0
        table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
        table.add_column("Plugin", style=ACCENT, no_wrap=True)
        table.add_column("Scope", style=MUTED, no_wrap=True)
        table.add_column("Path", overflow="fold")
        for plugin in sorted(available.values(), key=lambda p: p.name):
            table.add_row(plugin.name, plugin.scope, str(plugin.path))
        console.print(table)
        console.print(
            f"[{MUTED}]A plugin is local Python that registers tools. "
            f"Only files you put in .jaigent/plugins are loaded.[/]"
        )
        return 0

    if action == "new":
        scope = "user" if getattr(args, "user", False) else "project"
        try:
            path = plugins.create_plugin(args.name, scope=scope)
        except ToolError as exc:
            err_console.print(f"[red]{exc}[/]")
            return 1
        console.print(f"[green]{glyph('check')}[/] created {path}")
        console.print(f"[{MUTED}]Edit register() to add tools.[/]")
        return 0

    if action == "remove":
        doomed = available.get(args.name.strip().lower())
        if doomed is None:
            err_console.print(f"[red]No plugin named {args.name!r}.[/]")
            return 1
        doomed.path.unlink()
        console.print(f"[green]{glyph('check')}[/] removed {doomed.path}")
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
        console.print(f"[green]{glyph('check')}[/] {task.id}: {task.prompt}")
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
            console.print(f"[green]{glyph('check')}[/] removed {task.id}")
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

    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
    table.add_column("ID", style=ACCENT, no_wrap=True)
    table.add_column("Every", style=MUTED, no_wrap=True)
    table.add_column("Next", style=MUTED, no_wrap=True)
    table.add_column("Runs", justify="right", style=MUTED)
    table.add_column("Last", style=MUTED, no_wrap=True)
    table.add_column("Prompt", overflow="ellipsis")

    for task in tasks:
        status = task.last_status or "—"
        colour = {"ok": "green", "error": "red"}.get(status, MUTED)
        runs = f"{task.runs}" + (f" ({task.failures}{glyph('cross')})" if task.failures else "")
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
        agent = Agent(
            settings,
            tools=build_default_registry(settings, interactive=False),
            approver=Approver(Mode.AUTO, workspace=settings.workspace),
        )
        result = agent.run(task.prompt)
    except JaigentError as exc:
        task.record("error", str(exc))
        schedule.update(task)
        err_console.print(f"[red]  failed:[/] {exc}")
        return False

    task.record("ok", result.output)
    schedule.update(task)

    # Whitespace-only output used to crash here: `"  ".strip().splitlines()`
    # is `[]`, so `[0]` raised IndexError after a successfully recorded run.
    stripped = result.output.strip()
    summary = stripped.splitlines()[0][:120] if stripped else "(no output)"
    console.print(f"[green]  {glyph('check')}[/] {summary}")
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

        table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
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


def cmd_auth(args: argparse.Namespace) -> int:
    """Store provider API keys in the private user secrets file."""
    from jaigent.secrets import listed_keys, set_key, unset_key

    action = getattr(args, "auth_action", None) or "list"

    if action == "set":
        provider = args.provider.strip().lower()
        secret = (
            _clean_secret(args.key)
            if args.key
            else _read_key(API_KEY_ENV_VARS.get(provider, "JAIGENT_API_KEY"))
        )
        if not secret:
            err_console.print("[red]No key entered.[/]")
            return 1
        try:
            path = set_key(provider, secret)
        except ConfigurationError as exc:
            err_console.print(f"[red]{exc}[/]")
            return 1
        console.print(f"[green]{glyph('check')}[/] stored {provider} key in {path}")
        return 0

    if action == "unset":
        if unset_key(args.provider):
            console.print(f"[green]{glyph('check')}[/] removed {args.provider} key")
            return 0
        console.print(f"[{MUTED}]no stored key for {args.provider}[/]")
        return 1

    rows = listed_keys()
    if not rows:
        console.print(
            f"[{MUTED}]No stored provider keys. Save one with[/] "
            f"[{ACCENT}]jaigent auth set openai sk-...[/]",
            highlight=False,
        )
        return 0
    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
    table.add_column("Provider", style=ACCENT, no_wrap=True)
    table.add_column("Env var", style=MUTED, no_wrap=True)
    table.add_column("Key", style=MUTED, no_wrap=True)
    for provider, env_var, masked in rows:
        table.add_row(provider, env_var, masked)
    console.print(table)
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

    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
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
            # Non-interactive for the same reason: the server's stdin may be a
            # tty, but no user is watching a given request, so ask_user must
            # degrade to best-judgment instead of blocking on input.
            tools=build_default_registry(request_settings, interactive=False),
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
    if not prompt:
        err_console.print(
            "[red]Nothing to route.[/] Give it a prompt: "
            '[cyan]jaigent route "refactor the parser"[/]'
        )
        return 2
    if getattr(args, "free", False) or settings.model.strip().lower() == "free":
        routing = router.choose_free_model(
            prompt,
            usable=failover.available_providers(settings),
            fallback_provider=settings.provider,
            fallback_model=DEFAULT_MODELS.get(settings.provider, ""),
        )
    else:
        routing = router.choose_model(
            prompt,
            settings.provider,
            fallback=DEFAULT_MODELS.get(settings.provider, ""),
        )

    via = routing.provider or settings.provider
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
        f"  [{MUTED}]model[/]       [bold {ACCENT}]{routing.model}[/] [{MUTED}]via {via}[/]\n",
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

    # Walk back past checkpoints that would change nothing. Re-running the same
    # task writes identical content, so the newest checkpoint often reverts to a
    # state the file is already in — and stopping there means the user presses
    # undo, sees nothing happen, and has silently spent one anyway.
    skipped = 0
    while True:
        checkpoint = store.latest()
        if checkpoint is None:
            break

        if any(action != "unchanged" for _, action in store.diff_summary(checkpoint)):
            if skipped:
                console.print(
                    f"[{MUTED}]skipped {skipped} checkpoint(s) that would have changed nothing[/]"
                )
            code = _restore(store, checkpoint, plain=bool(args.no_color))
            # Consume it, so undoing again steps back another change rather than
            # restoring this same checkpoint forever.
            store.discard(checkpoint)
            return code

        store.discard(checkpoint)
        skipped += 1

    if skipped:
        console.print(
            f"[{MUTED}]Nothing to undo: the last {skipped} recorded change(s) already "
            "match what is on disk.[/]"
        )
    else:
        console.print(
            f"[{MUTED}]Nothing to undo. Checkpoints are written when the agent changes a file.[/]"
        )
    return 0


def cmd_rewind(args: argparse.Namespace) -> int:
    """Restore any checkpoint by id."""
    settings = resolve_settings(args)
    store = CheckpointStore(settings.workspace)
    try:
        checkpoint = store.get(args.id)
    except AmbiguousCheckpoint as exc:
        err_console.print(f"[red]{exc}[/]")
        return 1

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

    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
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


def _report_fetch_failure(reason: str, detail: str, install: updater.Install) -> int:
    """Explain why no release info is available, with a next step. Returns 1 or 0."""
    if reason == "no-releases":
        # No release exists to be newer than: for a source checkout this is a
        # clean bill of health, not an error.
        if install.kind == "source":
            console.print(
                f"\n[green]{glyph('check')} You're up to date.[/] "
                f"[{MUTED}]No releases published yet, and the checkout matches.[/]\n"
            )
            return 0
        err_console.print(
            "\n[yellow]No releases published yet — there is nothing to update to.[/]\n"
            f"  If you installed from git, refresh it directly:\n"
            f"  [cyan]pip install --upgrade git+{updater.REPO_URL}.git[/]\n"
        )
        return 1
    if reason == "rate-limited":
        err_console.print(
            "\n[yellow]GitHub's rate limit was hit — could not check for a newer release.[/] "
            "Try again in a few minutes, or see:\n"
            f"  https://github.com/{updater.REPO}/releases\n"
        )
        return 1
    extra = f" ({detail})" if detail else ""
    err_console.print(
        f"\n[red]Could not reach GitHub{extra} — could not find a newer release.[/] "
        "Check your connection, or see:\n"
        f"  https://github.com/{updater.REPO}/releases\n"
    )
    return 1


def _beta_state() -> tuple[bool, str]:
    """The beta channel as ``(enabled, where)``. Never raises.

    Project settings win over user settings, so the stored opt-in and the
    effective channel can disagree — every `beta` message goes through here
    so none of them can claim the channel is on while it is off, or blame
    the environment for a project file.
    """
    raw = os.getenv("JAIGENT_BETA", "")
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True, f"JAIGENT_BETA={raw.strip()}"
    try:
        rows = settings_store.describe()
    except (JaigentError, OSError):
        return updater.beta_enabled(), "unreadable settings"
    for key, value, source in rows:
        if key == "beta":
            if value:
                return True, f"{source} settings"
            return False, f"{source} settings set it false"
    return False, "default"


def cmd_beta(args: argparse.Namespace) -> int:
    """Join, leave, or show the beta channel."""
    action = getattr(args, "beta_action", None) or "status"

    if action == "join":
        path = settings_store.set_value("beta", True, scope="user")
        enabled, where = _beta_state()
        # Text, not markup: the path can hold brackets rich would swallow.
        if enabled:
            console.print(
                Text.assemble(
                    (f"{glyph('check')} beta channel is ", "green"),
                    ("on", "bold"),
                    (f" ({path})", MUTED),
                )
            )
        else:
            console.print(
                Text.assemble(
                    (f"{glyph('check')} beta choice stored ", "green"),
                    (f"({path})", MUTED),
                    (f" — still off: {where}", "yellow"),
                )
            )
            return 0
        console.print(
            f"[{MUTED}]`jaigent update` now pulls from the `beta` branch. "
            "Beta builds may break — report anything odd with[/] "
            f"[{ACCENT}]jaigent feedback[/][{MUTED}].[/]"
        )
        console.print(f"[{MUTED}]Run[/] [{ACCENT}]jaigent update[/] [{MUTED}]to switch now.[/]")
        return 0

    if action == "leave":
        removed = settings_store.unset_value("beta", scope="user")
        enabled, where = _beta_state()
        if not enabled:
            if removed:
                console.print(
                    f"[green]{glyph('check')}[/] beta channel is [bold]off[/] "
                    f"[{MUTED}]— updates pull from `main` again.[/]"
                )
            else:
                console.print(f"[{MUTED}]beta channel is already off.[/]")
            console.print(
                f"[{MUTED}]Run[/] [{ACCENT}]jaigent update[/] [{MUTED}]to switch back now.[/]"
            )
            return 0
        # Still on via the environment or the project file — name it so the
        # user knows where to go, instead of blaming JAIGENT_BETA always.
        if removed:
            console.print(f"[green]{glyph('check')}[/] removed from your user settings, but")
        if where.startswith("JAIGENT_BETA"):
            hint = "unset it to leave"
        else:
            hint = "remove it there to leave"
        console.print(f"[yellow]The channel stays on ({where}): {hint}.[/]")
        return 0

    enabled, where = _beta_state()
    if enabled:
        console.print(
            f"beta channel is [bold green]on[/] [{MUTED}]({where})[/]\n"
            f"[{MUTED}]`jaigent update` pulls from the `beta` branch. "
            "Leave with[/] "
            f"[{ACCENT}]jaigent beta leave[/][{MUTED}].[/]"
        )
    elif where == "default":
        console.print(
            f"[{MUTED}]beta channel is off — updates pull from `main`. "
            "Join with[/] "
            f"[{ACCENT}]jaigent beta join[/][{MUTED}].[/]"
        )
    else:
        console.print(f"[{MUTED}]beta channel is off ({where}) — updates pull from `main`.[/]")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    """Send feedback to the maintainers as a GitHub issue."""
    message = " ".join(getattr(args, "message", None) or []).strip()
    if not message:
        try:
            message = console.input("feedback: ").strip()
        except EOFError:
            err_console.print("[red]Nothing to send. Pass feedback text or run in a terminal.[/]")
            return 1
        if not message:
            err_console.print("[red]Nothing to send.[/]")
            return 1
    delivery = feedback.deliver(message, open_browser=not args.no_open)
    if delivery.method == "gh":
        console.print(f"[green]{glyph('check')}[/] feedback sent: {delivery.url}")
        return 0
    if delivery.opened:
        console.print(f"[{MUTED}]Finish sending it in your browser:[/]")
    else:
        console.print(f"[{MUTED}]Send it from here:[/]")
    # Text, not markup: the URL carries a query string rich would style.
    console.print(Text(delivery.url))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Check the published version and upgrade in place."""
    plain = bool(getattr(args, "no_color", False))
    force = bool(getattr(args, "force", False))
    use_beta = (
        False
        if getattr(args, "stable", False)
        else (True if getattr(args, "beta", None) else updater.beta_enabled())
    )
    channel = updater.channel_name(beta=use_beta)
    install = updater.detect_install()

    console.print(f"  [{MUTED}]installed[/]  {__version__} ({install.describe()})", highlight=False)
    console.print(f"  [{MUTED}]location[/]   {install.location}", highlight=False)
    console.print(f"  [{MUTED}]channel[/]    {channel}", highlight=False)
    if use_beta and install.kind == "binary":
        console.print(
            f"  [{MUTED}]note[/]       binaries follow releases, so --beta installs "
            "the latest pre-release binary",
            highlight=False,
        )

    with console.status("Checking GitHub...", spinner="dots") if not plain else nullcontext():
        fetched = updater.fetch_latest_detailed(beta=use_beta)
        # The source check must compare against the same channel the update
        # would install — comparing a beta checkout against main always
        # reports "not synced" and offers a useless pull.
        sync = updater.inspect_source(branch=channel)
    release = fetched.release
    updater.record_check(release)

    if sync.available:
        console.print(f"  [{MUTED}]source[/]     {sync.summary()}", highlight=False)
        if sync.branch:
            console.print(f"  [{MUTED}]branch[/]     {sync.branch}", highlight=False)
        if sync.local_sha:
            console.print(f"  [{MUTED}]local sha[/]  {sync.local_sha[:12]}", highlight=False)
        if sync.remote_sha:
            console.print(f"  [{MUTED}]{channel} sha[/]   {sync.remote_sha[:12]}", highlight=False)

    version_newer = bool(release is not None and release.is_newer)
    source_behind = bool(sync.available and sync.update_available)

    if release is None and not source_behind and not force:
        return _report_fetch_failure(fetched.reason, fetched.detail, install)

    if release is not None:
        tag = f"  [{MUTED}]latest[/]     {release.version}"
        if release.prerelease:
            tag += "  (pre-release)"
        if version_newer:
            tag += f"  {glyph('arrow_left')} new"
        console.print(tag, highlight=False)
        if version_newer:
            console.print(f"  {release.url}", highlight=False)

    if not version_newer and not source_behind and not force:
        if sync.ahead_only:
            console.print(f"\n[green]{glyph('check')} {sync.summary_cap()}.[/]\n")
        elif sync.available and sync.remote_sha:
            extra = " (working tree has local changes)" if sync.dirty else ""
            console.print(
                f"\n[green]{glyph('check')} You're up to date. "
                f"Version and source are in sync.{extra}[/]\n"
            )
        elif sync.available:
            # The version matches, but the source was never compared — saying
            # "in sync" here would be a guess, not a fact.
            console.print(
                f"\n[green]{glyph('check')} You're on the latest release ({__version__}).[/]"
            )
            console.print(
                f"[{MUTED}]Source could not be compared: {sync.error or 'unknown reason'}.[/]\n",
                highlight=False,
            )
            if channel == updater.BETA_BRANCH and sync.error.startswith("no "):
                console.print(
                    f"[{MUTED}]The beta channel needs a 'beta' branch on GitHub. Create it "
                    f"from this checkout with[/] [cyan]git push origin HEAD:beta[/]\n",
                    highlight=False,
                )
        else:
            console.print(f"\n[green]{glyph('check')} You're up to date.[/]\n")
        return 0

    if source_behind and not version_newer:
        if sync.behind:
            plural = "s" if sync.behind != 1 else ""
            detail = f"{sync.behind} commit{plural} behind {channel}"
        else:
            detail = f"not the same commit as {channel}"
        console.print(
            f"\n[{MUTED}]The published version matches, but this checkout is {detail}.[/]",
            highlight=False,
        )

    if args.check:
        if version_newer:
            console.print(
                f"\n[{MUTED}]Run [cyan]jaigent update[/] to install it.[/]", highlight=False
            )
        elif source_behind:
            console.print(
                f"\n[{MUTED}]Run [cyan]jaigent update[/] to sync source and reinstall.[/]",
                highlight=False,
            )
        return 0

    if not install.upgradable:
        err_console.print(
            f"\n[yellow]This is an {install.describe()}, so it cannot be upgraded automatically.[/]"
        )
        return 1

    # A binary beta update installs one pinned pre-release, not a branch, so
    # the prompt names the version it will actually fetch.
    pinned = use_beta and install.kind == "binary" and release is not None and version_newer
    if use_beta and not pinned:
        target = updater.BETA_BRANCH
    elif release is not None and version_newer:
        target = release.version
    else:
        target = channel
    pin = release.version if pinned and release is not None else None
    command = updater.upgrade_summary(install, beta=use_beta, version=pin)
    if not getattr(args, "yes", False) and sys.stdin.isatty():
        console.print()
        try:
            answer = console.input(
                Text.assemble(
                    ("  Update to ", ""),
                    (target, ACCENT),
                    ("? This runs: ", ""),
                    (command, MUTED),
                    ("\n  [Y/n] ", ""),
                )
            )
        except (EOFError, KeyboardInterrupt):
            console.print(f"[{MUTED}]cancelled[/]")
            return 0
        if answer.strip().lower() not in {"", "y", "yes"}:
            console.print(f"[{MUTED}]cancelled[/]")
            return 0

    console.print(f"\n[{MUTED}]$ {command}[/]", highlight=False)
    try:
        with console.status("Updating jAIgent...", spinner="dots") if not plain else nullcontext():
            output = updater.perform_update(install, beta=use_beta, version=pin)
    except updater.UpdateError as exc:
        # Text, not markup: the detail is installer output and can hold brackets.
        err_console.print(Text(f"\n{exc}", style="red"))
        return 1

    if output:
        console.print(f"[{MUTED}]{output[-500:]}[/]", highlight=False)

    # An upgrade command exits 0 in several situations that changed nothing:
    # pip with no newer version on the index, an "Already up to date" pull, a
    # package that is not on PyPI and therefore silently reinstalled from git,
    # a PATH where an older jaigent still comes first. Asking the installed CLI
    # what it is costs one process start and is the only honest way to report.
    expected = release.version if release is not None and version_newer else __version__
    with console.status("Verifying...", spinner="dots") if not plain else nullcontext():
        check = updater.verify_update(install, expected=expected)

    if check.updated and not check.shadowed:
        console.print(
            f"\n[green]{glyph('check')} Updated to {check.reported}.[/] "
            f"Run [cyan]jaigent --version[/] to verify.\n"
        )
        return 0

    # A source sync can move the code without moving the version number the
    # release check was aiming at (a beta bump, or commits between releases).
    # The version changing at all proves the new code is what now runs.
    if (
        install.kind == "source"
        and check.reported
        and check.reported != check.before
        and not check.shadowed
    ):
        console.print(
            f"\n[green]{glyph('check')} Source is now in sync with {channel} "
            f"({check.reported}).[/]\n"
        )
        return 0

    # One short line per fact, on purpose: rich wraps at the terminal width and
    # a sentence broken in the middle is much harder to act on.
    details: list[str] = []
    if check.reported is None:
        details.append(check.error or f"Could not read the version from {check.line()}.")
    elif check.shadowed:
        details.append(f"Your shell runs [yellow]{check.shell_line()}[/].")
        if check.updated:
            details.append(f"{check.reported} is installed, but in a copy it does not start.")
        else:
            details.append("That is an older copy this update did not touch.")
        for other in check.other_lines():
            details.append(f"  also on PATH: {other}")
        own = check.own_location
        details.append(f"Remove [cyan]{check.resolved}[/],")
        if own:
            details.append(f"or put [cyan]{own}[/] ahead of it on your PATH.")
        else:
            # A pip install has no path to promote: the file the shell starts
            # belongs to another installation entirely.
            details.append("or reinstall with the standalone installer so your shell runs it.")
    else:
        verb = "still reports" if check.reported == __version__ else "reports"
        details.append(f"[cyan]jaigent[/] {verb} [yellow]{check.line()}[/].")
        details.append(f"{expected} did not get installed.")
        for other in check.other_lines():
            details.append(f"  also on PATH: {other}")
        if check.elsewhere:
            details.append(f"Note: your shell runs {check.resolved},")
            details.append(f"not the copy at {check.own_location} that was replaced.")

    # Whichever way it went unchanged, a pip or pipx install has one extra
    # thing worth knowing while the package is not on PyPI.
    if install.kind in {"pip", "pipx"}:
        details.append("If jaigent is not on PyPI yet, pip has nothing newer to install:")
        details.append(f"[cyan]pip install --upgrade git+{updater.REPO_URL}.git[/]")

    err_console.print(
        "\n[yellow]The upgrade command finished, but nothing changed:[/]\n  "
        + "\n  ".join(details)
        + "\n"
    )
    return 1


def cmd_mcp(args: argparse.Namespace) -> int:
    """Start an MCP server over stdio for ChatGPT and Claude."""
    from jaigent.config import _env_flag  # noqa: PLC0415 - keep mcp imports lazy
    from jaigent.mcp import client_config, run_mcp

    if getattr(args, "print_config", None):
        try:
            console.print(client_config(args.print_config), highlight=False)
        except ToolError as exc:
            err_console.print(f"[red]{exc}[/]")
            return 1
        return 0

    settings = resolve_settings(args)
    allow_write = bool(getattr(args, "allow_write", None)) or _env_flag("JAIGENT_MCP_WRITE")
    client = getattr(args, "client", None) or "generic"
    return run_mcp(settings, allow_write=allow_write, client=client)


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
    row(True, "plugins", f"{len(plugins.discover())} defined")
    row(True, "commands", f"{len(commands.discover())} defined")
    row(
        True,
        "unicode",
        "yes" if supports_unicode() else "no — using ASCII fallbacks",
    )

    install = updater.detect_install()
    row(True, "install", f"{install.describe()} - {install.location}")
    pending = updater.cached_notice()
    row(
        not pending,
        "version",
        pending or f"{__version__} (latest known)",
    )
    sync = updater.inspect_source(timeout=3.0, fetch_remote=False)
    if sync.available:
        # Offline or a dirty tree is information, not a broken install.
        ok = True if not sync.remote_sha else sync.synced
        row(ok, "source", sync.summary())

    if problems:
        console.print(f"\n[red]{problems} problem(s) found.[/] See above.\n")
        return 1
    console.print(f"\n[green]{glyph('check')} Everything looks healthy.[/]\n")
    return 0


def _read_chat_prompt() -> str:
    """Read a chat line. Trailing backslash continues; empty Enter sends nothing."""
    lines: list[str] = []
    while True:
        mark = prompt_mark() if not lines else glyph("ellipsis")
        raw = console.input(f"[bold {ACCENT}]{mark}[/] ")
        # Backslash escaping: an odd run continues the line (consuming one),
        # pairs collapse to one so `C:\\` can still be typed. The old test
        # only recognised a lone trailing backslash: three in a row sent the
        # line instead of continuing, and two sent both backslashes along.
        tail = len(raw) - len(raw.rstrip("\\"))
        if tail % 2 == 1:
            lines.append(raw[: len(raw) - (tail + 1) // 2])
            continue
        if tail:
            raw = raw[: len(raw) - tail // 2]
        lines.append(raw)
        break
    return "\n".join(lines).strip()


def _describe_approval(mode: str) -> str:
    """``ask`` means nothing to a newcomer; "Ask me first" does."""
    return {
        "ask": "Ask me first",
        "auto": "Just do it",
        "dry-run": "Show me, don't touch",
    }.get(mode, mode)


def _print_live_settings(settings: Settings) -> None:
    """The session knobs in plain language, for ``/settings``.

    Labels read as what they are ("Working folder", "File changes") and
    values as what they mean ("Ask me first", "Saved"), with the commands
    that change them right underneath.
    """
    table = Table(
        show_header=False,
        box=_table_box(),
        pad_edge=False,
        show_edge=True,
        border_style=ACCENT_DIM,
    )
    table.add_column("Setting", style=ACCENT, no_wrap=True)
    table.add_column("Value", overflow="fold")
    table.add_row("AI provider", settings.provider)
    table.add_row("Model", settings.model)
    table.add_row("Working folder", _path_link(settings.workspace))
    table.add_row("File changes", _describe_approval(settings.approval))
    table.add_row("Live answers", "On" if settings.stream else "Off")
    table.add_row("Shell commands", "On" if settings.allow_shell else "Off")
    table.add_row("Memory", "On" if settings.memory else "Off")
    table.add_row(
        "API key",
        "Saved" if settings.api_key else "Missing — add one with /key",
    )
    console.print(table)
    console.print(
        Text.assemble(
            ("Change these any time: ", MUTED),
            ("/provider  /model  /approve  /workspace", f"bold {ACCENT}"),
        )
    )
    console.print(
        Text.assemble(
            ("stored in: ", MUTED),
            _path_link(settings_store.user_settings_path()),
            ("  ·  ", MUTED),
            _path_link(settings_store.project_settings_path()),
        )
    )


def _slash_key(argument: str, agent: Agent, settings: Settings) -> SlashResult:
    """``/key [provider] [secret]`` — never sends the key to the model."""
    from jaigent.secrets import set_key

    bits = argument.split(None, 1)
    provider = bits[0].strip().lower() if bits else settings.provider
    secret = (
        _clean_secret(bits[1])
        if len(bits) > 1
        else _read_key(API_KEY_ENV_VARS.get(provider, "JAIGENT_API_KEY"))
    )
    if not secret:
        err_console.print("[red]No key entered. Nothing was sent to the model.[/]")
        return SlashResult()
    try:
        path = set_key(provider, secret)
    except ConfigurationError as exc:
        err_console.print(f"[red]{exc}[/]")
        return SlashResult()
    os.environ[API_KEY_ENV_VARS.get(provider, "JAIGENT_API_KEY")] = secret
    if provider == settings.provider:
        updated = settings.merged_with(api_key=secret)
        agent.settings = updated
        console.print(f"[green]{glyph('check')}[/] stored {provider} key in {path}")
        return SlashResult(settings=updated)
    console.print(f"[green]{glyph('check')}[/] stored {provider} key in {path}")
    return SlashResult()


def _print_status(agent: Agent, settings: Settings, session: sessions.Session) -> None:
    """A compact snapshot of the session, for /status."""
    cost = estimate(settings.model, session.usage)
    store = agent.checkpoints
    rows = [
        ("AI provider", settings.provider),
        ("model", settings.model),
        ("working folder", str(settings.workspace)),
        ("file changes", _describe_approval(settings.approval)),
        ("session", session.id),
        ("messages", str(len(agent.history))),
        ("spend so far", cost.summary()),
        ("undo points", str(len(store.history(limit=1000))) if store else "disabled"),
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
    table = Table(
        title="jAIgent configuration",
        show_header=True,
        header_style=f"bold {ACCENT}",
        box=_table_box(),
        border_style=ACCENT_DIM,
    )
    table.add_column("Setting")
    table.add_column("Value", overflow="fold")
    for key, value in settings.redacted().items():
        table.add_row(key, str(value))
    console.print(table)

    if not settings.api_key:
        console.print(
            "\n[yellow]No API key configured.[/] jAIgent never ships with one — bring your own:"
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
    console.print(Rule(style=ACCENT_DIM))

    examples = (
        ('jaigent "summarise the README in this folder"', "run one task"),
        ("jaigent chat", "interactive session"),
        ("jaigent sessions", "old chats"),
        ("jaigent chat --resume", "pick up where you left off"),
    )
    width = max(len(command) for command, _ in examples)
    # Only pad and annotate when the notes actually fit; otherwise show bare commands.
    roomy = console.width >= width + max(len(note) for _, note in examples) + 6

    for command, note in examples:
        line = Text("  ")
        if roomy:
            line.append(command.ljust(width), style=f"bold {ACCENT}")
            line.append(f"   {note}", style=MUTED)
        else:
            line.append(command, style=f"bold {ACCENT}")
        console.print(line, overflow="ellipsis", no_wrap=True)

    console.print(f"\n[{MUTED}]Bring your own API key:[/] [{ACCENT}]jaigent init[/]")
    console.print(f"[{MUTED}]Full options:[/] [{ACCENT}]jaigent --help[/]\n")


def _print_answer(text: str, *, plain: bool = False) -> None:
    if not text:
        console.print(f"[{MUTED}](the model returned an empty answer)[/]")
        return
    if plain:
        print(text)
    else:
        console.print(_markdown(text))


def _print_tools(registry) -> None:  # noqa: ANN001 - ToolRegistry, avoids an import cycle in typing
    table = Table(show_header=True, header_style=f"bold {ACCENT}", box=_table_box())
    table.add_column("Tool", style=ACCENT, no_wrap=True)
    table.add_column("Description", overflow="fold")
    for tool in registry:
        name = f"{tool.name} [red]{glyph('warn')}[/]" if tool.dangerous else tool.name
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
        cap = float(getattr(settings, "budget", 0) or 0)
        if cap > 0 and result.cost.usd is not None and result.cost.usd >= cap:
            bits.append("spend cap reached")
        else:
            bits.append("step budget exhausted")

    if bits:
        console.print(
            f"[{ACCENT}]{glyph('bullet')}[/] [{MUTED}]{' · '.join(bits)}[/]",
            highlight=False,
        )


# ----------------------------------------------------------------------
def _print_update_notice(args: argparse.Namespace) -> None:
    """Mention a newer release, once, after the command has done its work.

    Only for interactive terminals: piping `jaigent config` into a script must
    not get an extra line of chatter appended to it.
    """
    if args.command in {"update", "serve", "mcp"} or updater.checks_disabled():
        return
    if not sys.stdout.isatty():
        return
    notice = updater.cached_notice()
    if notice:
        console.print(f"\n[{MUTED}]{notice}[/]", highlight=False)


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
        "plugins": cmd_plugins,
        "providers": cmd_providers,
        "schedule": cmd_schedule,
        "commands": cmd_commands,
        "keys": cmd_keys,
        "serve": cmd_serve,
        "route": cmd_route,
        "undo": cmd_undo,
        "checkpoints": cmd_checkpoints,
        "rewind": cmd_rewind,
        "doctor": cmd_doctor,
        "update": cmd_update,
        "mcp": cmd_mcp,
        "auth": cmd_auth,
        "beta": cmd_beta,
        "feedback": cmd_feedback,
    }

    # Refresh the cached release info in the background (at most once a day),
    # and show whatever the *previous* run found. Doing it this way means the
    # notice never costs the current command any time.
    check_thread = None
    # mcp uses stdout as the protocol stream — never start a background
    # network thread that could race with the handshake.
    if args.command not in {"update", "mcp"}:
        check_thread = updater.check_in_background()

    try:
        code = handlers[args.command](args)
    except ConfigurationError as exc:
        # Text, not markup: the message can carry paths with brackets.
        err_console.print(Text.assemble(("configuration error: ", "red"), str(exc)))
        return 78  # EX_CONFIG
    except JaigentError as exc:
        _print_run_error(exc, None)
        return 1
    except KeyboardInterrupt:
        err_console.print("\n[dim]interrupted[/]")
        return 130
    finally:
        # Join on *every* path, not just the happy one. The worker is a daemon
        # thread: returning without joining lets the interpreter tear down
        # while it is mid-TLS-handshake, which segfaults the process.
        updater.finish_check(check_thread)

    _print_update_notice(args)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
