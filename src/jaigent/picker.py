"""The arrow-key option picker behind ``ask_user``'s interactive mode.

Typing a number under a wall of text is a questionnaire; choosing with the
arrow keys is a conversation. This module renders the question as a panel of
radio options, moves a pointer with ``↑``/``↓`` (or ``j``/``k``), and confirms
with Enter — digits still jump-pick, and Esc switches to typing a free-form
answer.

It is deliberately dependency-free: raw keys come from ``termios`` on POSIX
and ``msvcrt`` on Windows, wrapped so that any surprise — a pipe instead of a
terminal, a console without Unicode, a missing module — degrades to the typed
prompt instead of raising. The panel redraws in place with a gently pulsing
marker, and is transient: once answered, the caller replaces it with a single
summary line so the transcript stays compact.

Tests never touch a real terminal: pass ``keys`` and the picker runs the same
state machine against a scripted key sequence.
"""

from __future__ import annotations

import contextlib
import os
import select
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from jaigent.branding import ACCENT, ACCENT_DIM, INK, MUTED
from jaigent.ui import glyph, supports_unicode

if sys.platform == "win32":  # pragma: no cover - not exercised on Linux CI
    import msvcrt
else:
    import termios
    import tty

# Outcomes of a key press / of the whole pick.
STAY = "stay"
PICKED = "picked"
CUSTOM = "custom"
CANCELLED = "cancelled"
EOF = "eof"
#: The picker cannot run here (piped, no raw mode); the caller falls back.
UNAVAILABLE = "unavailable"

#: Seconds between pulses of the selected marker.
PULSE_INTERVAL = 0.45

#: Control characters and escape sequences, normalised to key names.
_KEY_NAMES = {
    "\r": "enter",
    "\n": "enter",
    "\t": "tab",
    "\x1b": "esc",
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1b[C": "right",
    "\x1b[D": "left",
    "\x7f": "backspace",
    "\x08": "backspace",
    "\x03": "ctrl-c",
    "\x04": "ctrl-d",
}

#: The Windows scan-code suffixes for the arrow keys.
_WINDOWS_ARROWS = {"H": "up", "P": "down", "K": "left", "M": "right"}

#: What entering raw mode can raise on this platform.
_ENTER_ERRORS: tuple[type[BaseException], ...] = (
    () if sys.platform == "win32" else (OSError, ValueError, termios.error)
)


@dataclass(frozen=True)
class PickResult:
    """How a pick ended.

    ``kind`` is one of ``PICKED`` (``value`` holds the choice), ``CUSTOM``
    (the user wants to type their own answer), ``CANCELLED`` (Ctrl-C),
    ``EOF`` (the prompt closed) or ``UNAVAILABLE`` (no interactive picker
    here; the caller should fall back to a typed prompt).
    """

    kind: str
    value: str = ""

    @property
    def picked(self) -> bool:
        return self.kind == PICKED


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
def key_name(raw: str) -> str:
    """Normalise a raw character or escape sequence to a key name.

    Printable characters pass through unchanged, so ``key_name("q") == "q"``.
    """
    return _KEY_NAMES.get(raw, raw)


def _read_key_posix(fd: int) -> str:
    """Read one key from ``fd`` in cbreak mode."""
    chunk = os.read(fd, 1)
    if not chunk:
        return "eof"
    if chunk == b"\x1b":
        # Arrow keys arrive as ESC [ x; a lone ESC is the Esc key. Give the
        # rest of the sequence a moment, then treat whatever came as one key.
        readable, _, _ = select.select([fd], [], [], 0.05)
        if not readable:
            return "esc"
        sequence = chunk + os.read(fd, 5)
        return key_name(sequence.decode("utf-8", "replace")[:3])
    return key_name(chunk.decode("utf-8", "replace"))


def _read_key_windows() -> str:
    """Read one key through ``msvcrt``."""
    if sys.platform == "win32":  # pragma: no cover - not exercised on Linux CI
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):  # an arrow or function key follows
            return _WINDOWS_ARROWS.get(msvcrt.getwch(), STAY)
        return key_name(char)
    return STAY  # pragma: no cover - Windows only


@contextlib.contextmanager
def _cbreak_keys() -> Iterator[Callable[[], str]]:
    """Yield a key reader with the terminal in cbreak mode (POSIX).

    Echo is off, so the pointer animation is the only thing that moves while
    the user chooses. The previous terminal settings are always restored.
    """
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        yield lambda: _read_key_posix(fd)
    finally:
        # Never crash over a restore; the terminal is going away regardless.
        with contextlib.suppress(OSError):
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def raw_keys_available() -> bool:
    """Whether raw key reading is possible on this platform and stdin."""
    if sys.platform == "win32":  # pragma: no cover - not exercised on Linux CI
        return True
    try:
        sys.stdin.fileno()
    except (OSError, ValueError, AttributeError):
        return False
    return True


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------
@dataclass
class PickerState:
    """Which option the cursor is on. Pure state: no terminal, no rendering."""

    options: list[str]
    index: int = 0

    def move(self, delta: int) -> None:
        """Step the cursor, wrapping around the ends."""
        if self.options:
            self.index = (self.index + delta) % len(self.options)


def apply_key(state: PickerState, key: str) -> str:
    """Act on one key.

    Returns ``STAY``, ``PICKED``, ``CUSTOM``, ``CANCELLED`` or ``EOF``;
    ``STAY`` and ``PICKED`` may have moved ``state.index``.
    """
    if key in {"up", "k"}:
        state.move(-1)
        return STAY
    if key in {"down", "j"}:
        state.move(1)
        return STAY
    if key == "enter":
        return PICKED
    if key in {"esc", "e", "tab"}:
        return CUSTOM
    if key == "ctrl-c":
        return CANCELLED
    if key == "ctrl-d":
        return EOF
    if key.isdigit() and key != "0":
        # Digits jump straight to the numbered option and pick it.
        position = int(key) - 1
        if position < len(state.options):
            state.index = position
            return PICKED
    return STAY


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_picker(
    question: str,
    options: list[str],
    index: int,
    *,
    unicode_ok: bool = True,
    pulse: bool = False,
) -> Panel:
    """The question panel: radio options, a pointer on the current row, hints.

    ``pulse`` alternates the selected marker so the panel reads as waiting
    rather than frozen; callers toggle it on a timer.
    """
    rows: list[Text] = [Text(question, style=f"bold {INK}"), Text("")]
    for position, option in enumerate(options):
        selected = position == index
        if selected:
            marker = glyph("radio_pulse" if pulse else "radio_on", unicode_ok=unicode_ok)
        else:
            marker = glyph("radio_off", unicode_ok=unicode_ok)
        pointer = glyph("pointer", unicode_ok=unicode_ok) if selected else " "
        line = Text()
        line.append(f"  {pointer} ", style=ACCENT if selected else MUTED)
        line.append(f"{marker} ", style=ACCENT if selected else MUTED)
        # Long options would force the panel to overflow narrow terminals and
        # wrap mid-word between pulse frames.
        display = option if len(option) <= 60 else option[:57].rstrip() + "…"
        line.append(display, style=f"bold {INK}" if selected else MUTED)
        rows.append(line)

    rows.append(Text(""))
    up = glyph("arrow_up", unicode_ok=unicode_ok)
    down = glyph("arrow_down", unicode_ok=unicode_ok)
    bullet = glyph("bullet", unicode_ok=unicode_ok)
    hint = Text(style=MUTED)
    # Keep hint readable on narrow terminals (e.g. 40 cols); longer hints
    # would wrap and make the panel jump between pulse frames.
    if len(options) > 6:
        # Should not happen (MAX_OPTIONS=6), but guard anyway.
        hint.append(f"{up}{down} move {bullet} enter confirm")
    else:
        hint.append(f"{up}{down} move")
        if len(options) > 1:
            hint.append(f" {bullet} 1-{len(options)} pick")
        hint.append(f" {bullet} enter confirm {bullet} esc own answer")
    rows.append(hint)

    return Panel(
        Group(*rows),
        title=f"[bold {ACCENT}]jAI has a question[/]",
        border_style=ACCENT_DIM,
        padding=(0, 1),
    )


# ---------------------------------------------------------------------------
# Driving the picker
# ---------------------------------------------------------------------------
def picker_available(console: Console, *, keys: Iterable[str] | None = None) -> bool:
    """Whether :func:`pick_option` can run here.

    Scripted ``keys`` always qualify — that is the test path, and it must not
    touch a real terminal. Otherwise the console must be a terminal, stdin
    must be a tty somebody is typing at, and the platform must offer raw keys.
    """
    if keys is not None:
        return True
    if not console.is_terminal:
        return False
    try:
        if not sys.stdin.isatty():
            return False
    except Exception:  # noqa: BLE001 - a tty probe must never raise
        return False
    return raw_keys_available()


def pick_option(
    console: Console,
    question: str,
    options: list[str],
    *,
    keys: Iterable[str] | None = None,
) -> PickResult:
    """Ask the user to choose one of ``options``.

    Args:
        console: Where the panel renders. Must be the same console any live
            status line uses, so the two never fight over the screen.
        question: Shown at the top of the panel.
        options: The choices; the caller has already capped their number.
        keys: Scripted key names for tests. When given, no terminal is
            touched and nothing is rendered.

    Returns:
        A :class:`PickResult`; ``UNAVAILABLE`` when there is nothing to pick
        from. ``KeyboardInterrupt`` propagates if the terminal raises it.
    """
    if not options:
        return PickResult(UNAVAILABLE)

    state = PickerState(list(options))

    if keys is not None:
        return _pick_scripted(state, keys)

    reader_cm: AbstractContextManager[Callable[[], str]]
    if sys.platform == "win32":  # pragma: no cover - not exercised on Linux CI
        reader_cm = contextlib.nullcontext(_read_key_windows)
    else:
        reader_cm = _cbreak_keys()

    try:
        with reader_cm as reader:
            return _pick_live(console, question, state, reader)
    except _ENTER_ERRORS:
        # The terminal refused raw mode (a pipe, a lost tty, an odd console).
        return PickResult(UNAVAILABLE)


def _pick_scripted(state: PickerState, keys: Iterable[str]) -> PickResult:
    """Run the key loop against a scripted sequence, touching no terminal."""
    for key in keys:
        outcome = apply_key(state, key)
        if outcome == PICKED:
            return PickResult(PICKED, state.options[state.index])
        if outcome != STAY:
            return PickResult(outcome)
    return PickResult(EOF)


def _pick_live(
    console: Console, question: str, state: PickerState, reader: Callable[[], str]
) -> PickResult:
    """Redraw the panel in place while the user chooses."""
    unicode_ok = supports_unicode(getattr(console, "file", None))
    live = Live(
        render_picker(question, state.options, state.index, unicode_ok=unicode_ok),
        console=console,
        transient=True,
        refresh_per_second=8,
    )
    pulsing = threading.Event()

    def pulse() -> None:
        # A slow blink on the selected marker: proof the prompt is listening.
        on = False
        while not pulsing.wait(PULSE_INTERVAL):
            live.update(
                render_picker(question, state.options, state.index, unicode_ok=unicode_ok, pulse=on)
            )
            on = not on

    live.start()
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        while True:
            try:
                key = reader()
            except (OSError, ValueError):
                return PickResult(EOF)
            if key == "eof":
                return PickResult(EOF)
            outcome = apply_key(state, key)
            if outcome == PICKED:
                return PickResult(PICKED, state.options[state.index])
            if outcome != STAY:
                return PickResult(outcome)
            live.update(render_picker(question, state.options, state.index, unicode_ok=unicode_ok))
    finally:
        pulsing.set()
        thread.join(timeout=1.0)
        live.stop()
