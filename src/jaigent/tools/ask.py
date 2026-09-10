"""Let the model ask the user a question mid-run, with a dedicated prompt UI.

File approvals already interrupt the run with a diff; this is the equivalent
for *choices* — a missing preference, an ambiguous target, a fork with real
consequences.

With options and a terminal, the question renders as an arrow-key picker:
``↑``/``↓`` move, Enter confirms, digits jump-pick, and Esc switches to typing
a free-form answer. Once answered, the panel collapses into a single summary
line, so the transcript stays compact. Without a terminal — or with an open
question — it falls back to a numbered panel and a typed prompt.

Where nobody can answer (``serve``, schedules, pipes, MCP), the tool says so
and the model proceeds with its best judgment instead of blocking forever.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from jaigent.branding import ACCENT, ACCENT_DIM, INK, MUTED
from jaigent.errors import ToolError
from jaigent.picker import CANCELLED, CUSTOM, EOF, pick_option, picker_available
from jaigent.tools.base import Tool
from jaigent.ui import glyph

#: Cap on suggested answers; more would scroll the question off screen.
MAX_OPTIONS = 6

#: Longest question echoed on the one-line summary after answering.
SUMMARY_QUESTION_LIMIT = 64

#: What the model reads when the user did not pick anything specific.
SKIPPED = "The user skipped the question. Proceed with your best judgment."
CLOSED = "The user closed the prompt without answering. Proceed with your best judgment."


def build_ask_tools(
    *,
    console: Console | None = None,
    input_fn: Callable[[str], str] | None = None,
    interactive: bool | None = None,
    keys: Iterable[str] | None = None,
) -> list[Tool]:
    """Expose ``ask_user``.

    Args:
        console: Where the question renders. The CLI passes its own console so
            the question and any live status line coordinate instead of
            fighting over the screen.
        input_fn: Reads typed answers; injected in tests.
        interactive: Force interactive mode on or off. Defaults to probing
            whether stdin is a terminal — ``serve`` and schedules pass False
            explicitly, because their stdin may be a tty with nobody watching.
        keys: Scripted picker keys; injected in tests.
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
        if isinstance(options, str):
            # A bare string iterates as characters; refuse it so the model
            # retries with a list instead of getting a letter-per-option picker.
            raise ToolError("options must be a list of strings, not a single string")
        choices = [str(item).strip() for item in (options or []) if str(item).strip()]
        choices = choices[:MAX_OPTIONS]
        if not is_interactive():
            return (
                "The user cannot be asked right now (non-interactive session). "
                "Proceed with your best judgment and note the assumption."
            )

        if choices and picker_available(target, keys=keys) and (keys is not None or not input_fn):
            return _ask_with_picker(target, clean, choices, input_fn, keys)
        return _ask_typed(target, clean, choices, input_fn)

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
                            "Up to 6 short suggested answers, shown as a list the "
                            "user can pick from directly. The user may also type "
                            "their own answer. Omit for an open question."
                        ),
                    },
                },
                "required": ["question"],
            },
            func=ask_user,
        )
    ]


# ---------------------------------------------------------------------------
# The two prompt styles
# ---------------------------------------------------------------------------
def _ask_with_picker(
    target: Console,
    question: str,
    choices: list[str],
    input_fn: Callable[[str], str] | None,
    keys: Iterable[str] | None,
) -> str:
    """The arrow-key picker; the panel vanishes once answered."""
    result = pick_option(target, question, choices, keys=keys)
    # KeyboardInterrupt propagates: cancelling the question cancels the
    # run, exactly like quitting an approval prompt.
    if result.kind == CANCELLED:
        raise KeyboardInterrupt
    if result.kind == EOF:
        return CLOSED
    if result.kind == CUSTOM:
        answer = _read_answer(target, question, input_fn)
        if answer is None:
            return CLOSED
        if not answer:
            return SKIPPED
        _print_summary(target, question, answer)
        return f"The user answered: {answer}"
    _print_summary(target, question, result.value)
    return f"The user answered: {result.value}"


def _ask_typed(
    target: Console,
    question: str,
    choices: list[str],
    input_fn: Callable[[str], str] | None,
) -> str:
    """The fallback: a numbered panel and a typed answer."""
    rows: list[Any] = [Text(question, style=f"bold {INK}")]
    if choices:
        rows.append(Text(""))
        for index, choice in enumerate(choices, start=1):
            number = Text(f"  {index}. ", style=f"bold {ACCENT}")
            number.append(choice, style=INK)
            rows.append(number)
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

    answer = _read_answer(target, question, input_fn)
    if answer is None:
        return CLOSED
    if not answer:
        return SKIPPED
    if choices:
        try:
            picked = int(answer)
        except ValueError:
            pass
        else:
            if 1 <= picked <= len(choices):
                _print_summary(target, question, choices[picked - 1])
                return f"The user answered: {choices[picked - 1]}"
    return f"The user answered: {answer}"


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------
def _read_answer(
    target: Console, question: str, input_fn: Callable[[str], str] | None
) -> str | None:
    """Read a typed answer, echoing the question so it is not lost.

    ``None`` means the prompt closed (EOF); ``""`` means the user skipped.
    """
    read = input_fn or (lambda prompt: target.input(Text(prompt, style=ACCENT)))
    try:
        # The picker's panel is gone by now; keep the question on screen.
        short = _short_question(question)
        return read(f"{short} — your answer: " if short else "Your answer: ").strip()
    except EOFError:
        return None


def _short_question(question: str) -> str:
    """The question as one line, for the summary and the typed echo."""
    line = question.splitlines()[0].strip() if question.strip() else ""
    if len(line) > SUMMARY_QUESTION_LIMIT:
        line = line[: SUMMARY_QUESTION_LIMIT - 1] + "…"
    return line


def _print_summary(target: Console, question: str, answer: str) -> None:
    """One line that outlives the picker: what was asked, what was chosen."""
    line = Text()
    line.append(f"{glyph('check')} ", style="green")
    line.append(_short_question(question), style=MUTED)
    line.append(f" {glyph('arrow')} ", style=MUTED)
    line.append(answer, style=f"bold {INK}")
    target.print(line)
