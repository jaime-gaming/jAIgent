"""What the agent is, where it is running, and what it can actually do.

A model answers better when it knows its own situation: that it is jAIgent,
which surface it is running on, which provider is behind it, whether a human
will approve a mutation or nobody is watching at all, and where it stops. None
of that is in the tool schemas, and guessing it is how an agent promises a file
it cannot write or a question nobody will answer.

So this module reads the *real* configuration and the *real* tool registry and
turns them into a short, bounded paragraph for the system prompt. Every claim
is derived, never asserted: a capability appears only when the tool that backs
it is actually registered, and a limit only when the setting that imposes it is
actually on. An agent built read-only is told it cannot change files; an agent
with no shell is told it has no shell; an agent under a spend cap is told the
number.

It deliberately does not repeat what the prompt already says (the workspace
path, the tool names, "explore before you edit") or what the provider already
sends (each tool's own description and schema). It adds the part that is true
of *this run* rather than of any run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jaigent import __version__

if TYPE_CHECKING:  # pragma: no cover
    from jaigent.config import Settings
    from jaigent.tools.base import ToolRegistry

#: Tool names that back each capability, so the claim follows the registry
#: rather than a hardcoded list that could drift from what is actually offered.
_WEB = frozenset({"web_search", "fetch_page"})
_READ = frozenset({"list_files", "read_file", "search_files"})
_WRITE = frozenset({"write_file", "edit_file", "delete_file"})
_MEMORY = frozenset({"remember", "recall"})


def describe(
    settings: Settings,
    tools: ToolRegistry,
    *,
    surface: str = "",
    interactive: bool | None = None,
    version: str | None = None,
) -> str:
    """A short paragraph telling the agent its context and capabilities.

    Args:
        settings: The resolved configuration for this run.
        tools: The registry the model will actually be offered.
        surface: Where this run happens, as a short phrase such as
            ``"in the terminal"`` or ``"as a scheduled task"``. Empty omits the
            clause rather than guessing one.
        interactive: Whether a human can answer ``ask_user`` this run. ``False``
            is what the web panel, the gateway and a schedule pass, and it both
            drops the "you can ask" claim and says plainly that nobody will
            answer, so the agent uses its own judgement instead of blocking.
            ``None`` leaves the claim in, for a terminal where someone usually
            is.
        version: Override the reported version, mostly for tests.

    Returns:
        Plain text, ready to drop into the system prompt. It is bounded: a
        handful of sentences that cost little and rule out a class of confident
        mistakes.
    """
    names = set(tools.names())
    version = version or __version__

    identity = f"You are jAIgent {version}"
    if surface:
        identity += f", running {surface}"
    model = settings.model or "the provider's default model"
    lines = [f"{identity}. This run uses {settings.provider} ({model})."]

    can = _capabilities(names, settings, interactive)
    if can:
        lines.append("You can: " + "; ".join(can) + ".")

    rules = _rules(names, settings, interactive)
    if rules:
        lines.append("Rules of this run: " + "; ".join(rules) + ".")

    return "\n".join(lines)


def _capabilities(names: set[str], settings: Settings, interactive: bool | None) -> list[str]:
    """What the agent can do, one clause per group of tools actually present."""
    can: list[str] = []
    if names & _WEB:
        can.append(f"search the live web and read pages (backend: {settings.search_backend})")
    if names & _READ:
        can.append("list, read and search files in the workspace")
    if names & _WRITE:
        can.append("create, edit and delete files in the workspace")
    if "run_command" in names:
        can.append("run shell commands inside the workspace")
    if "write_todos" in names:
        can.append("keep a multi-step plan as a checklist the user watches")
    # Asking is only a real capability when someone can answer. The tool is
    # registered either way, but advertising it on an unattended run is how an
    # agent blocks on a question nobody will ever see.
    if "ask_user" in names and interactive is not False:
        can.append("ask the user one clarifying question when genuinely blocked")
    if "load_skill" in names:
        can.append("load a stored procedure on demand")
    if names & _MEMORY:
        can.append("save and recall standing notes that persist between runs")
    return can


def _rules(names: set[str], settings: Settings, interactive: bool | None) -> list[str]:
    """The limits and permissions that are true of this run, derived not assumed."""
    rules: list[str] = []

    approval = str(settings.approval)
    if approval == "ask":
        rules.append(
            "a human approves each mutating call before it runs, and you cannot approve it yourself"
        )
    elif approval == "dry-run":
        rules.append("mutations are simulated, so nothing is written to disk this run")
    elif approval == "auto":
        rules.append("mutations apply without asking")

    if interactive is False:
        rules.append("nobody can answer a question this run, so use your own judgement")
    if not (names & _WRITE):
        rules.append("this is a read-only run, so you cannot change files")
    if "run_command" not in names:
        rules.append("you have no shell access")

    rules.append(f"you stop after {settings.max_steps} tool-calling steps")
    budget = float(getattr(settings, "budget", 0) or 0)
    if budget > 0:
        rules.append(f"you stop before this run costs ${budget:.2f}")

    if getattr(settings, "checkpoints", False) and (names & _WRITE):
        rules.append("file changes are checkpointed so they can be undone")
    if getattr(settings, "memory", False):
        rules.append("standing notes persist between runs")

    return rules
