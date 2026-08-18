"""Human-in-the-loop approval for destructive tool calls.

The agent can write and delete files. In ``ask`` mode — the default for
interactive use — jaigent shows a unified diff of what is about to change and
waits for a yes or no. ``--yes`` skips the prompt for automation, ``--dry-run``
refuses every mutation while still letting the agent read and search.

The policy is enforced in one place, :class:`Approver`, which the agent consults
before running any tool marked ``dangerous`` or listed in :data:`MUTATING_TOOLS`.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from jaigent.branding import ACCENT, ACCENT_DIM, MUTED

#: Tools that change the filesystem or the machine, and therefore need approval.
MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete_file", "run_command"})

#: Cap on diff lines shown before truncation.
MAX_DIFF_LINES = 60


class Mode(str, Enum):
    """How much the agent may do without asking."""

    #: Prompt before every mutating tool call.
    ASK = "ask"
    #: Never prompt. Used by ``--yes`` and by non-interactive runs.
    AUTO = "auto"
    #: Refuse every mutating tool call, reporting the refusal to the model.
    DRY_RUN = "dry-run"


@dataclass(slots=True, frozen=True)
class Decision:
    """The outcome of an approval check."""

    allowed: bool
    reason: str = ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def build_diff(before: str, after: str, filename: str, *, limit: int = MAX_DIFF_LINES) -> Text:
    """Render a coloured unified diff between two strings."""
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
            n=2,
        )
    )

    text = Text()
    if not lines:
        text.append("(no textual change)", style=MUTED)
        return text

    shown = lines[:limit]
    for line in shown:
        if line.startswith("+++") or line.startswith("---"):
            text.append(line + "\n", style=MUTED)
        elif line.startswith("@@"):
            text.append(line + "\n", style=ACCENT_DIM)
        elif line.startswith("+"):
            text.append(line + "\n", style="green")
        elif line.startswith("-"):
            text.append(line + "\n", style="red")
        else:
            text.append(line + "\n", style=MUTED)

    if len(lines) > limit:
        text.append(f"… {len(lines) - limit} more diff lines\n", style=MUTED)

    text.rstrip()
    return text


def preview(tool: str, arguments: dict[str, Any], workspace: Path) -> RenderableType:
    """Build a human-readable preview of what ``tool`` is about to do."""
    title = Text()
    title.append(tool, style=f"bold {ACCENT}")

    if tool == "run_command":
        command = str(arguments.get("command", ""))
        body: RenderableType = Text(f"$ {command}", style="bold")
        return Panel(body, title=title, border_style=ACCENT_DIM, padding=(0, 1))

    raw_path = str(arguments.get("path", ""))
    target = (workspace / raw_path).resolve() if raw_path else workspace
    exists = target.is_file()

    if tool == "delete_file":
        body = Text(f"delete {raw_path}", style="red")
        if exists:
            size = target.stat().st_size
            body.append(f"  ({size:,} bytes)", style=MUTED)
        return Panel(body, title=title, border_style="red", padding=(0, 1))

    before = _read(target) if exists else ""

    if tool == "write_file":
        after = str(arguments.get("content", ""))
        if arguments.get("append"):
            after = before + after
    elif tool == "edit_file":
        old = str(arguments.get("old_text", ""))
        new = str(arguments.get("new_text", ""))
        count = int(arguments.get("count", 1) or 1)
        after = before.replace(old, new) if count == -1 else before.replace(old, new, count)
    else:  # pragma: no cover - defensive
        after = before

    header = Text()
    header.append(raw_path, style="bold")
    header.append("  new file" if not exists else "", style=MUTED)

    return Panel(
        Group(header, build_diff(before, after, raw_path or "file")),
        title=title,
        border_style=ACCENT_DIM,
        padding=(0, 1),
    )


class Approver:
    """Decides whether a tool call may proceed, prompting the user if needed.

    Args:
        mode: The approval policy.
        console: Where previews and prompts are rendered.
        prompt: Injection point for the y/n question; defaults to a rich prompt.
            Tests pass a stub, and non-interactive runs never reach it.
        workspace: Used to resolve relative paths when building previews.
    """

    def __init__(
        self,
        mode: Mode = Mode.AUTO,
        *,
        console: Console | None = None,
        prompt: Callable[[str], str] | None = None,
        workspace: Path | None = None,
    ) -> None:
        self.mode = Mode(mode)
        self.console = console or Console()
        self._prompt = prompt
        self.workspace = Path(workspace or Path.cwd())
        #: Tools the user chose to always allow for the rest of the run.
        self.always: set[str] = set()

    # ------------------------------------------------------------------
    def needs_approval(self, tool: str) -> bool:
        return tool in MUTATING_TOOLS

    def check(self, tool: str, arguments: dict[str, Any]) -> Decision:
        """Decide whether ``tool`` may run with ``arguments``."""
        if not self.needs_approval(tool):
            return Decision(True)

        if self.mode is Mode.DRY_RUN:
            return Decision(
                False,
                f"Refused: jaigent is in dry-run mode, so {tool} did not run and nothing "
                "was changed. Describe what you would have done instead.",
            )

        if self.mode is Mode.AUTO or tool in self.always:
            return Decision(True)

        return self._ask(tool, arguments)

    # ------------------------------------------------------------------
    def _ask(self, tool: str, arguments: dict[str, Any]) -> Decision:
        self.console.print()
        self.console.print(preview(tool, arguments, self.workspace))

        answer = self._read_answer("Apply this change? [y]es / [n]o / [a]lways / [q]uit: ")
        choice = (answer or "").strip().lower()

        if choice in {"a", "always"}:
            self.always.add(tool)
            return Decision(True)
        if choice in {"", "y", "yes"}:
            return Decision(True)
        if choice in {"q", "quit"}:
            raise KeyboardInterrupt
        return Decision(
            False,
            f"The user declined the {tool} call. Do not retry it; ask what they would "
            "prefer, or continue with the rest of the task.",
        )

    def _read_answer(self, question: str) -> str:
        if self._prompt is not None:
            return self._prompt(question)
        try:
            # Rendered as Text, not markup: the question contains [y]/[n]/[a]/[q],
            # which rich would otherwise swallow as style tags.
            return self.console.input(Text(question, style=ACCENT))
        except (EOFError, KeyboardInterrupt):
            return "n"
