"""Let the model ask the user a question mid-run, with a dedicated prompt UI.

File approvals already interrupt the run with a diff; this is the equivalent
for *choices* — a missing preference, an ambiguous target, a fork with real
consequences. The question renders as its own panel with numbered options, so
it never looks like more streamed text to skim past.

Where nobody can answer (``serve``, schedules, pipes, MCP), the tool says so
and the model proceeds with its best judgment instead of blocking forever.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from jaigent.branding import ACCENT, ACCENT_DIM, MUTED
from jaigent.errors import ToolError
from jaigent.tools.base import Tool

#: Cap on suggested answers; more would scroll the question off screen.
MAX_OPTIONS = 6


def build_ask_tools(
    *,
    console: Console | None = None,
    input_fn: Callable[[str], str] | None = None,
    interactive: bool | None = None,
) -> list[Tool]:
    """Expose ``ask_user``.

    Args:
        console: Where the question panel renders.
        input_fn: Reads the answer; injected in tests.
        interactive: Force interactive mode on or off. Defaults to probing
            whether stdin is a terminal — ``serve`` and schedules pass False
            explicitly, because their stdin may be a tty with nobody watching.
    """
    target = console or Console()

    def is_interactive() -> bool:
        if interactive is not None:
            return interactive
        try:
            return bool(sys.stdin.isatty())
        except Exception:  # noqa: BLE001 - a tty probe must never raise
            return False

    def ask_user(question: str, options: list[Any] | None = None) -> str:
        clean = (question or "").strip()
        if not clean:
            raise ToolError("ask_user needs a question: pass the exact text to show the user.")
        choices = [str(item).strip() for item in (options or []) if str(item).strip()]
        choices = choices[:MAX_OPTIONS]
        if not is_interactive():
            return (
                "The user cannot be asked right now (non-interactive session). "
                "Proceed with your best judgment and note the assumption."
            )

        rows: list[Any] = [Text(clean)]
        if choices:
            rows.append(Text(""))
            for index, choice in enumerate(choices, start=1):
                rows.append(Text(f"  {index}. {choice}", style=f"bold {ACCENT}"))
            rows.append(Text(""))
            rows.append(Text("Reply with a number, or type your own answer.", style=MUTED))
        else:
            rows.append(Text(""))
            rows.append(Text("Type your answer.", style=MUTED))

        target.print()
        target.print(
            Panel(
                Group(*rows),
                title=f"[bold {ACCENT}]jAI has a question[/]",
                border_style=ACCENT_DIM,
                padding=(0, 1),
            )
        )

        read = input_fn or (lambda prompt: target.input(Text(prompt, style=ACCENT)))
        try:
            answer = read("Your answer: ").strip()
        except EOFError:
            return "The user closed the prompt without answering. Proceed with your best judgment."
        # KeyboardInterrupt propagates: cancelling the question cancels the
        # run, exactly like quitting an approval prompt.
        if not answer:
            return "The user skipped the question. Proceed with your best judgment."
        if choices:
            try:
                picked = int(answer)
            except ValueError:
                pass
            else:
                if 1 <= picked <= len(choices):
                    return f"The user answered: {choices[picked - 1]}"
        return f"The user answered: {answer}"

    return [
        Tool(
            name="ask_user",
            description=(
                "Ask the user a clarifying question and wait for their answer. "
                "Use this when you genuinely cannot proceed without input — a "
                "missing preference, an ambiguous target, a choice with real "
                "consequences. Do not use it to narrate progress or to confirm "
                "routine steps; just do the work. At most one question per call."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The exact question to show the user. One thing at a time.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Up to 6 short suggested answers, shown as a numbered list. "
                            "The user may also type their own answer. Omit for an open question."
                        ),
                    },
                },
                "required": ["question"],
            },
            func=ask_user,
        )
    ]
