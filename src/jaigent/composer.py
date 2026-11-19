"""The chat box: a framed prompt you type *into*, and the queue behind it.

A terminal has no input widget, so the box is drawn by hand: a frame around a
line edited with the keys you already expect — arrows, Home/End, word jumps,
word deletes, the history of what you sent. It sits under the transcript, is
redrawn in place, and does not scroll away while the agent works.

While a turn runs the box stays usable and Enter **queues** the line; the
queue drains the moment the turn ends, one turn each. That is the whole point:
the old prompt silenced the keyboard during a turn and then flushed whatever
had been typed into it, so a message written while waiting was thrown away.
Ctrl-C with text in the box interrupts the running turn and sends that text at
once.

Why drawn here rather than with a library. Input widgets are usually readline
(no frame, no multi-line) or prompt_toolkit, which would be a new runtime
dependency for a prompt — against this project's rules.

Everything degrades. With no terminal there is no box and the caller keeps a
plain line; every terminal-mode call is wrapped, so an odd console produces a
plainer prompt instead of a crash mid-session.

The interesting half is pure: :class:`Composer` plus :func:`apply_key` is the
state machine tests drive with scripted keys, and :func:`render_composer`
returns rich renderables for a given width. Only :class:`LiveComposer` touches
termios, and only when asked to.
"""

from __future__ import annotations

import contextlib
import os
import select
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from jaigent.branding import ACCENT, ACCENT_DIM, INK, MUTED
from jaigent.ui import prompt_mark, supports_unicode

if sys.platform != "win32":
    import termios
    import tty

# ---------------------------------------------------------------------------
# What one key press means
# ---------------------------------------------------------------------------
STAY = "stay"
"""Nothing to do; redraw if the line changed."""

SUBMIT = "submit"
"""Enter on a non-empty line: the caller takes the text."""

CLEARED = "cleared"
"""Ctrl-L: wipe the screen and draw the box again."""

INTERRUPT = "interrupt"
"""Ctrl-C on an empty box."""

INTERRUPT_AND_SEND = "send"
"""Ctrl-C with text in the box: stop the turn and send this line."""

EOF_KEY = "eof"
"""Ctrl-D on an empty box: close the session."""

#: How many sent lines the history keeps.
HISTORY_LIMIT = 200

#: Shown, muted, when the box is empty and waiting.
IDLE_PLACEHOLDER = "Ask anything, or type /help"

#: Shown when a turn is running, so nobody has to guess whether a message
#: typed now is lost or kept.
QUEUE_PLACEHOLDER = "Type ahead — Enter queues it for the next turn"

#: What the footer says the keyboard does. English matches the rest of the
#: interface (``Enter: send`` rather than a translated label per console).
SEND_LABEL = "Enter: send"
QUEUE_LABEL = "Enter: queue"

# ---------------------------------------------------------------------------
# Terminal mode
# ---------------------------------------------------------------------------
if sys.platform == "win32":  # pragma: no cover - Windows only
    _MODE_ERRORS: tuple[type[BaseException], ...] = ()
else:
    _MODE_ERRORS = (OSError, ValueError, termios.error)

#: Escape sequences, longest first so the matcher is deterministic:
#: ``\x1b[1;5C`` (Ctrl-Right) must not be read as ``\x1b[1~``.
_SEQUENCES: tuple[tuple[str, str], ...] = (
    ("\x1b[13;2u", "enter"),  # kitty keyboard protocol: shift-enter
    ("\x1b[27;5;13~", "enter"),
    ("\x1b[1;5C", "word-right"),
    ("\x1b[1;5D", "word-left"),
    ("\x1b[1;3C", "word-right"),
    ("\x1b[1;3D", "word-left"),
    ("\x1b[3~", "delete"),
    ("\x1b[1~", "home"),
    ("\x1b[4~", "end"),
    ("\x1b[7~", "home"),
    ("\x1b[8~", "end"),
    ("\x1b[H", "home"),
    ("\x1b[F", "end"),
    ("\x1bOH", "home"),
    ("\x1bOF", "end"),
    ("\x1b[A", "up"),
    ("\x1b[B", "down"),
    ("\x1b[C", "right"),
    ("\x1b[D", "left"),
    ("\x1bOA", "up"),
    ("\x1bOB", "down"),
    ("\x1bOC", "right"),
    ("\x1bOD", "left"),
    ("\x1b[2~", "insert"),
    ("\x1bb", "word-left"),
    ("\x1bf", "word-right"),
)

#: Single characters and control codes, normalised to key names.
_CHARS: dict[str, str] = {
    "\r": "enter",
    "\n": "enter",
    "\t": "tab",
    "\x7f": "backspace",
    "\x08": "backspace",
    "\x01": "home",  # Ctrl-A
    "\x05": "end",  # Ctrl-E
    "\x03": "ctrl-c",
    "\x04": "ctrl-d",
    "\x0c": "ctrl-l",
    "\x15": "ctrl-u",
    "\x0b": "ctrl-k",
    "\x17": "ctrl-w",
    "\x10": "up",  # Ctrl-P, as in every readline
    "\x0e": "down",  # Ctrl-N
}

#: Windows scan codes behind a ``\x00``/``\xe0`` prefix.
_WINDOWS_SCAN: dict[str, str] = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
    "G": "home",
    "O": "end",
    "S": "delete",
    "s": "word-left",  # Ctrl-Left
    "t": "word-right",  # Ctrl-Right
}

#: How long to wait for the rest of a multi-byte character or an escape
#: sequence before deciding it was a lone key.
_SEQUENCE_GRACE = 0.02


def key_name(raw: str) -> str:
    """Normalise a single character to a key name.

    Printable characters pass through unchanged: ``key_name("q") == "q"``.
    Escape sequences are handled by :class:`KeyReader`, which has to know how
    long they are before it can name them.
    """
    return _CHARS.get(raw, raw)


def _decode_sequence(data: str) -> str | None:
    """Name a whole escape sequence, or ``None`` if it is not one we use.

    An unrecognised sequence is still *consumed*: the alternative — treating
    its tail as typed characters — inserts ``[1;2C`` into the line whenever
    someone presses a key this build does not map.
    """
    for sequence, name in _SEQUENCES:
        if data == sequence:
            return name
    return STAY


class KeyReader:
    """Turns a byte stream into one key name at a time.

    Reads are buffered on purpose. Terminals deliver whatever has been typed
    into one ``read``, so a read of a single byte loses the rest — which is
    how ``left left`` once became ``left``. Here a key is decoded from the
    buffer, and anything after it stays queued for the next call.

    Three kinds of input need care:

    * **An escape sequence** (arrows, Home, Delete, Ctrl-arrows) is ``ESC``
      plus one or more bytes. A bare Esc is the same prefix with nothing
      behind it, so the reader waits a few milliseconds before deciding.
    * **A multi-byte character** (``é``, ``漢``) arrives as 2–4 bytes; half of
      one is not a keystroke. The lead byte says how many to collect.
    * **Ctrl-C** is not delivered at all in cbreak mode — the terminal raises
      SIGINT instead. It is turned back into a key by
      :class:`~jaigent.composer.SigintQueue`, not by this class.

    ``read_fn`` is the seam the tests use: give it a script and the reader is
    exercised without a terminal.
    """

    def __init__(
        self,
        read_fn: Callable[[], bytes],
        *,
        more: Callable[[], bool] | None = None,
        grace: float = _SEQUENCE_GRACE,
    ) -> None:
        self._read_fn = read_fn
        #: Whether the stream has more waiting right now. ``None`` means "trust
        #: each chunk to be complete", which is what the tests rely on.
        self._more = more
        self._grace = grace
        #: Bytes, not text: a character split across two reads can only be
        #: rejoined while it is still bytes. Decoding each chunk as it arrived
        #: turned "漢" into two replacement characters.
        self._buffer = b""

    @property
    def buffered(self) -> bytes:
        return self._buffer

    def _more_now(self) -> bool:
        if self._more is None:
            return False
        try:
            return bool(self._more())
        except (OSError, ValueError):  # pragma: no cover - broken pty
            return False

    def _fill(self) -> bool:
        """Pull one more chunk from the terminal. ``False`` means end of input."""
        try:
            chunk = self._read_fn()
        except OSError:  # pragma: no cover - the pty went away
            return False
        if not chunk:
            return False
        self._buffer += chunk
        return True

    def read(self) -> str:
        """The next key's name, or ``"eof"`` when the terminal closes."""
        while not self._buffer and not self._fill():
            return "eof"
        first = self._buffer[0]
        if first == 0x1B:
            return self._take_escape()
        if first < 0x80:
            # Control characters and ASCII printables are one keystroke each.
            self._buffer = self._buffer[1:]
            return key_name(chr(first))
        return self._take_character(first)

    def _take_character(self, lead: int) -> str:
        """Decode one multi-byte character, waiting for the rest of its bytes."""
        wanted = 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
        while len(self._buffer) < wanted:
            if self._more is not None and not self._more_now():
                break
            if not self._fill():
                break
        chunk, self._buffer = self._buffer[:wanted], self._buffer[wanted:]
        return chunk.decode("utf-8", "replace")

    def _take_escape(self) -> str:
        # (a lone ESC is decided below, after waiting for a possible tail)
        """Decode one escape sequence, consuming exactly what belongs to it.

        Matched longest-first against :data:`_SEQUENCES`. While the buffer is a
        proper prefix of a known sequence the rest may still be in flight, so
        the reader gives the terminal a few milliseconds before deciding: that
        is what tells a bare Esc from the start of an arrow key.
        """
        while True:
            for sequence, name in _SEQUENCES:
                if self._buffer.startswith(sequence.encode("ascii")):
                    self._buffer = self._buffer[len(sequence) :]
                    return name
            if self._incomplete() and self._wait_for_more():
                continue
            break
        # Not a sequence this build knows. Consume it whole anyway: leaving the
        # tail behind would type "[1;2C" into the line.
        length = 1 if len(self._buffer) == 1 else self._escape_length()
        self._buffer = self._buffer[length:]
        return "esc" if length == 1 else STAY

    def _incomplete(self) -> bool:
        """Whether the buffer is a prefix of a sequence we know."""
        return any(sequence.encode("ascii").startswith(self._buffer) for sequence, _ in _SEQUENCES)

    def _wait_for_more(self) -> bool:
        """Block briefly for the rest of a sequence. ``False`` means it never came."""
        if self._more is not None:
            if not self._more_now():
                return False
            return self._fill()
        # No stream to ask: accept a chunk that is exactly one sequence.
        return False

    def _escape_length(self) -> int:
        """How many characters of an unrecognised escape sequence to drop."""
        if len(self._buffer) < 2:
            return 1
        if self._buffer[1] == ord("O"):
            return 3  # SS3: ESC O <letter>
        if self._buffer[1] != ord("["):
            return 2  # Alt-<key>
        index = 2
        while index < len(self._buffer):
            if 0x40 <= self._buffer[index] <= 0x7E:
                return index + 1
            index += 1
        return len(self._buffer)


def _read_key_windows() -> str:  # pragma: no cover - Windows only
    """Read one key through ``msvcrt``."""
    import msvcrt  # noqa: PLC0415 - Windows only

    # ``msvcrt`` is a Windows-only module; the stub on other platforms does
    # not describe it, and this file is not run there.
    getwch = msvcrt.getwch  # type: ignore[attr-defined]

    char = getwch()
    if char in ("\x00", "\xe0"):
        return _WINDOWS_SCAN.get(getwch(), STAY)
    return key_name(char)


class SigintQueue:
    """The terminal's SIGINT, turned back into a keystroke.

    In cbreak mode the line discipline still handles ``ISIG``: Ctrl-C sends a
    signal rather than a byte, so a reader waiting in ``read(2)`` either gets
    interrupted or — worse, since Python retries — never sees it at all. The
    handler here only records the event, so it is safe to run at any point,
    and the reader drains it between keys.

    Install it with :meth:`install` around the chat; it always puts the
    previous handler back. Outside a SIGINT-aware context it does nothing,
    and on a platform without signals it is inert.
    """

    def __init__(self) -> None:
        self.count = 0
        self._previous: Any = None
        self._installed = False

    def install(self) -> bool:
        """Record Ctrl-C instead of letting it interrupt whatever runs.

        ``interrupts=False`` is for the case where Ctrl-C arrives as a byte
        anyway (a pty with no controlling terminal): leaving the default
        handler in place then costs nothing.
        """
        if self._installed:
            return True
        try:
            import signal

            self._previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._on_sigint)
        except (ImportError, OSError, ValueError):  # pragma: no cover - odd host
            return False
        self._installed = True
        return True

    def make_a_key(self) -> None:
        """Turn Ctrl-C into a keystroke: recorded, never raised.

        With cbreak mode the terminal keeps ``ISIG``, so Ctrl-C raises SIGINT
        rather than sending ``\x03``. Handling it means the box can decide
        what it means — stop this turn, keep what I typed — instead of the
        process unwinding through whatever happened to be running.
        """
        self.install()

    def restore(self) -> None:
        if not self._installed:
            return
        with contextlib.suppress(OSError, ValueError, ImportError):
            import signal

            if self._previous is not None:
                signal.signal(signal.SIGINT, self._previous)
        self._installed = False

    def _on_sigint(self, signum: int, frame: object) -> None:  # pragma: no cover - signal
        self.count += 1

    def take(self) -> bool:
        """Consume one recorded Ctrl-C, if there is one."""
        if self.count:
            self.count -= 1
            return True
        return False


@dataclass(frozen=True)
class Keys:
    """A keyboard: how to read one key, and how to ask if one is waiting.

    ``ready`` is what makes Ctrl-C work. The terminal raises SIGINT rather than
    sending a byte, so a thread parked in ``read(2)`` never learns about it —
    Python retries the read after running the handler. Waiting for "something
    is readable" with a short timeout instead lets the reader look at the
    signal queue between keys, which is the only way a keystroke that arrives
    as a signal can be handled like any other keystroke.
    """

    read: Callable[[], str]
    #: Whether a key can be read without blocking: true if the terminal has
    #: bytes waiting *or* the reader is holding some of an earlier chunk.
    #: Asking only the terminal loses text: one read takes the whole chunk,
    #: so everything after the first character sits in the reader's buffer
    #: while the terminal itself looks idle.
    ready: Callable[[float], bool] | None = None


@contextlib.contextmanager
def raw_keys() -> Iterator[Keys]:
    """Yield a key reader with the terminal in cbreak mode.

    Cbreak rather than raw: Ctrl-C is a signal either way, and flushing still
    happens line by line as it always did. The saved mode is always restored,
    including when the body raises — on a pty that is closing, the usual case.

    The reader is built around the file descriptor, not around ``sys.stdin``:
    a read through ``sys.stdin`` would buffer bytes Python never gives back,
    which is exactly how a keystroke gets lost between two calls.
    """
    if sys.platform == "win32":  # pragma: no cover - Windows only
        yield Keys(_read_key_windows, None)
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        reader = KeyReader(lambda: os.read(fd, 1024), more=lambda: _pending(fd))

        def ready(timeout: float) -> bool:
            return bool(reader.buffered) or _pending(fd, timeout)

        yield Keys(reader.read, ready)
    finally:
        with contextlib.suppress(*_MODE_ERRORS):
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _pending(fd: int, grace: float = _SEQUENCE_GRACE) -> bool:
    """Whether more input is already on its way from the terminal."""
    try:
        readable, _, _ = select.select([fd], [], [], grace)
    except (OSError, ValueError):  # pragma: no cover - broken pty
        return False
    return bool(readable)


# ---------------------------------------------------------------------------
# The editable line
# ---------------------------------------------------------------------------
@dataclass
class Composer:
    """The text in the box, the cursor in it, and the history behind it.

    Pure state: no terminal, no rich, no I/O — every editing behaviour the box
    has is a method here, so the interesting part is testable without a
    terminal. ``cursor`` is an index into ``text`` (0 is before the first
    character).
    """

    text: str = ""
    cursor: int = 0
    #: Lines already sent, oldest first. Shared by reference with the caller so
    #: history survives one box giving way to the next.
    history: list[str] = field(default_factory=list)
    #: Where in the history browsing has got to; ``None`` means the live line.
    _browse: int | None = None
    _draft: str = ""

    @property
    def value(self) -> str:
        """What would be sent: the line without surrounding whitespace."""
        return self.text.strip()

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    # -- editing -----------------------------------------------------------
    def insert(self, chunk: str) -> None:
        if not chunk:
            return
        self.text = self.text[: self.cursor] + chunk + self.text[self.cursor :]
        self.cursor += len(chunk)
        self._browse = None

    def backspace(self) -> None:
        if self.cursor > 0:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1
            self._browse = None

    def delete(self) -> None:
        if self.cursor < len(self.text):
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
            self._browse = None

    def move(self, delta: int) -> None:
        """Move the cursor, stopping at both ends."""
        self.cursor = max(0, min(len(self.text), self.cursor + delta))

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.text)

    def word_left(self) -> None:
        """Jump to the start of the previous word (Ctrl-Left, Alt-B)."""
        index = self.cursor
        while index > 0 and not self.text[index - 1].isalnum():
            index -= 1
        while index > 0 and self.text[index - 1].isalnum():
            index -= 1
        self.cursor = index

    def word_right(self) -> None:
        """Jump past the end of the next word (Ctrl-Right, Alt-F)."""
        index = self.cursor
        while index < len(self.text) and not self.text[index].isalnum():
            index += 1
        while index < len(self.text) and self.text[index].isalnum():
            index += 1
        self.cursor = index

    def kill_word(self) -> None:
        """Delete the word before the cursor (Ctrl-W)."""
        index = self.cursor
        while index > 0 and not self.text[index - 1].isalnum():
            index -= 1
        while index > 0 and self.text[index - 1].isalnum():
            index -= 1
        self.text = self.text[:index] + self.text[self.cursor :]
        self.cursor = index
        self._browse = None

    def kill_to_start(self) -> None:
        """Delete everything before the cursor (Ctrl-U)."""
        self.text = self.text[self.cursor :]
        self.cursor = 0
        self._browse = None

    def kill_to_end(self) -> None:
        """Delete everything after the cursor (Ctrl-K)."""
        self.text = self.text[: self.cursor]
        self._browse = None

    def clear(self) -> None:
        self.text = ""
        self.cursor = 0
        self._browse = None

    # -- history -----------------------------------------------------------
    def remember(self, line: str) -> None:
        """Keep a sent line, ignoring blanks and immediate repeats."""
        line = line.strip()
        if not line or (self.history and self.history[-1] == line):
            return
        self.history.append(line)
        if len(self.history) > HISTORY_LIMIT:
            del self.history[: len(self.history) - HISTORY_LIMIT]

    def history_older(self) -> bool:
        """Step back through what was sent; ``False`` at the oldest entry.

        The half-written line is kept as a draft and restored on the way back
        past the newest entry: losing a prompt because you glanced at the
        history is the kind of thing that makes a terminal feel hostile.
        """
        if not self.history or self._browse == 0:
            return False
        if self._browse is None:
            self._browse = len(self.history)
            self._draft = self.text
        self._browse -= 1
        self.text = self.history[self._browse]
        self.cursor = len(self.text)
        return True

    def history_newer(self) -> bool:
        """Step forward through the history, back to the draft at the end."""
        if self._browse is None:
            return False
        if self._browse < len(self.history) - 1:
            self._browse += 1
            self.text = self.history[self._browse]
        else:
            self._browse = None
            self.text = self._draft
        self.cursor = len(self.text)
        return True


# ---------------------------------------------------------------------------
# The key table
# ---------------------------------------------------------------------------
def apply_key(state: Composer, key: str) -> str:
    """Apply one key to ``state`` and say what the caller should do.

    Returns one of :data:`STAY`, :data:`SUBMIT`, :data:`CLEARED`,
    :data:`INTERRUPT`, :data:`INTERRUPT_AND_SEND` or :data:`EOF_KEY`. The state
    machine never runs a command and never clears the line on submit: reading
    and sending stay separate, so the caller decides what a line means.
    """
    if key == "enter":
        # Empty Enter is free: holding it down while thinking must not fill
        # the queue with blanks.
        return SUBMIT if not state.empty else STAY
    if key == "left":
        state.move(-1)
    elif key == "right":
        state.move(1)
    elif key == "word-left":
        state.word_left()
    elif key == "word-right":
        state.word_right()
    elif key == "home":
        state.home()
    elif key == "end":
        state.end()
    elif key == "up":
        state.history_older()
    elif key == "down":
        state.history_newer()
    elif key == "backspace":
        state.backspace()
    elif key == "delete":
        state.delete()
    elif key == "ctrl-u":
        state.kill_to_start()
    elif key == "ctrl-k":
        state.kill_to_end()
    elif key == "ctrl-w":
        state.kill_word()
    elif key == "ctrl-l":
        return CLEARED
    elif key == "ctrl-c":
        # With text the message is too good to throw away: the caller stops
        # the turn and sends it. Empty, there is nothing to interrupt.
        return INTERRUPT_AND_SEND if state.value else INTERRUPT
    elif key == "ctrl-d":
        if state.empty:
            return EOF_KEY
        state.delete()  # readline's behaviour: delete, do not quit
    elif key in {"esc", "tab", "insert", STAY}:
        pass  # reserved: completion, multi-line, paste — none yet
    elif key and key.isprintable():
        state.insert(key)
    return STAY


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _frame(unicode_ok: bool) -> dict[str, str]:
    """The box's pieces: rounded where the console can draw them."""
    if unicode_ok:
        return {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
    return {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}


def queue_summary(queued: Sequence[str]) -> str:
    """``2 queued``, or empty when nothing is waiting.

    One is spelled out rather than counted: ``1 queued`` reads like a glitch.
    """
    if not queued:
        return ""
    return "1 queued" if len(queued) == 1 else f"{len(queued)} queued"


def _window(text: str, cursor: int, room: int) -> tuple[str, int]:
    """The visible slice of ``text`` and where the cursor sits inside it.

    Scrolling by characters, not cells: a wide CJK line is therefore slightly
    off rather than measured wrongly, which is the cheaper of the two.
    """
    if len(text) <= room:
        return text, cursor
    start = max(0, cursor - room + 1)
    start = min(start, max(0, len(text) - room))
    return text[start : start + room], cursor - start


def render_composer(
    state: Composer,
    *,
    width: int,
    meta: str = "",
    hint: str = "",
    placeholder: str = IDLE_PLACEHOLDER,
    queued: Sequence[str] = (),
    unicode_ok: bool = True,
    label: str = SEND_LABEL,
    busy: bool = False,
) -> Text:
    """Draw the box: a framed line, its footer, and an optional status line.

    ``meta`` is the session's context on the left of the footer (model,
    approval mode); ``label`` is what Enter does right now. The frame is
    accent-coloured while the box has the keyboard, muted while a turn owns the
    screen — the same signal without a word of explanation.

    ``width`` is a hard limit. The line scrolls under the cursor rather than
    wrapping: a box that grows a row per keystroke walks the transcript off the
    top of the screen.
    """
    width = max(16, width)
    frame = _frame(unicode_ok)
    mark = prompt_mark(unicode_ok=unicode_ok)
    border = ACCENT_DIM if busy else ACCENT

    inner = width - 2  # the two frame characters
    prefix = f" {mark} "
    room = max(1, inner - len(prefix) - 1)
    text, _cursor = _window(state.text, state.cursor, room)

    line = Text()
    line.append(frame["v"], style=border)
    line.append(prefix, style=f"bold {ACCENT}")
    if state.empty:
        line.append(placeholder[:room], style=MUTED)
    else:
        line.append(text, style=INK)
    padding = inner - len(prefix) - len(placeholder[:room] if state.empty else text)
    line.append(" " * max(0, padding))
    line.append(frame["v"], style=border)

    top = Text(frame["tl"] + frame["h"] * inner + frame["tr"], style=border)

    # The footer rides on the bottom border, the way a status bar does: the
    # corners stay put, the rule stretches between the two ends of text.
    waiting = queue_summary(queued)
    right = f" {waiting} · {label} " if waiting else f" {label} "
    left = f" {meta} " if meta else ""
    footer = Text()
    footer.append(frame["bl"], style=border)
    fill = inner - len(left) - len(right)
    if fill >= 0:
        footer.append(left, style=f"bold {ACCENT}")
        footer.append(frame["h"] * fill, style=border)
        footer.append(right, style=border if busy else ACCENT)
    else:
        # No room for both ends: the keyboard hint is the part that teaches.
        shortened = right.strip()[: max(0, inner - 2)]
        footer.append(frame["h"] * (inner - 1 - len(shortened)), style=border)
        footer.append(shortened + " ", style=border if busy else ACCENT)
    footer.append(frame["br"], style=border)

    out = Text()
    out.append_text(top)
    out.append("\n")
    out.append_text(line)
    out.append("\n")
    out.append_text(_fit(footer, width))
    if hint:
        out.append("\n")
        out.append_text(_fit(Text(hint, style=MUTED), width))
    return out


def _fit(text: Text, width: int) -> Text:
    """``text`` cut to ``width`` cells, keeping each character's style."""
    if text.cell_len <= width:
        return text
    fitted = text.copy()
    fitted.truncate(width, overflow="crop")
    return fitted


# ---------------------------------------------------------------------------
# The one region that owns the bottom of the screen
# ---------------------------------------------------------------------------
class ChatScreen:
    """The single live region a chat session keeps at the bottom.

    Everything that has to stay put while the session runs lives here, stacked
    in order: the thinking line, the answer as it streams, and the box. One
    ``rich`` Live owns all three, refreshed on a timer, so the pieces never
    fight over the screen — printing anything else (a tool trace, a notice, the
    footer) lands *above* the region and the box stays exactly where it was.

    This is what "the chat does not move" means in a terminal: the region is
    redrawn in place, and only the transcript above it scrolls.

    Layers are callables or ready renderables; a ``None`` layer is simply not
    drawn. The Live is transient: stopping it erases the region, and whatever
    should stay in the transcript is printed by the caller first.
    """

    def __init__(self, console: Console, *, refresh_per_second: float = 12.0) -> None:
        self.console = console
        self.refresh_per_second = refresh_per_second
        #: The thinking line, as a callable, while a turn is running.
        self.status: Callable[[], Any] | None = None
        #: The answer being streamed, already rendered.
        self.answer: Any = None
        #: The box, as a callable.
        self.box: Callable[[], Any] | None = None
        self._live: Live | None = None

    @property
    def running(self) -> bool:
        return self._live is not None

    def start(self) -> ChatScreen:
        if self._live is None:
            self._live = Live(
                self.compose(),
                console=self.console,
                transient=True,
                refresh_per_second=self.refresh_per_second,
                vertical_overflow="visible",
            )
            self._live.start()
        return self

    def stop(self) -> None:
        """Erase the region. Layers are kept, so :meth:`start` restores them."""
        if self._live is not None:
            self._live.stop()
            self._live = None

    def set_status(self, render: Callable[[], Any] | None) -> None:
        self.status = render

    def set_answer(self, renderable: Any) -> None:
        self.answer = renderable

    def set_box(self, render: Callable[[], Any] | None) -> None:
        self.box = render

    def compose(self) -> Any:  # noqa: ANN401 - a rich renderable
        """The current region: status, then the answer, then the box."""
        parts: list[Any] = []
        if self.status is not None:
            text = self.status()
            if text is not None and str(getattr(text, "plain", "x")):
                parts.append(text)
        if self.answer is not None:
            parts.append(self.answer)
        if self.box is not None:
            parts.append(self.box())
        if not parts:
            return Text("")
        if len(parts) == 1:
            return parts[0]
        return Group(*parts)

    def refresh(self) -> None:
        if self._live is not None:
            self._live.update(self.compose(), refresh=True)

    def print(self, message: Any) -> None:  # noqa: ANN401 - anything rich renders
        """Print above the region, keeping the region exactly where it was.

        The Live is transient, so stopping it erases it; the message lands at
        the bottom of the transcript and the region is drawn again underneath.
        Printing straight through would leave the region's rows behind it and
        tear the screen, which is why this exists at all.
        """
        was_running = self._live is not None
        if was_running:
            self.stop()
        self.console.print(message, highlight=False)
        if was_running:
            self.start()


# ---------------------------------------------------------------------------
# Reading a line
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Line:
    """What :meth:`LiveComposer.read` got, and what the caller should do with it.

    ``cancel`` is set for Ctrl-C: the line is still the text, so the caller can
    interrupt a turn *and* send it — which is what makes a long turn stoppable
    without losing the message written during it.
    """

    text: str
    cancel: bool = False


class ComposerLine(EOFError):
    """Ctrl-D on an empty box: the caller closes the session."""


class Unavailable(RuntimeError):
    """The box cannot run here; the caller should fall back to a plain line."""


# ---------------------------------------------------------------------------
# The live box
# ---------------------------------------------------------------------------
class LiveComposer:
    """Draws the box, reads keys, and hands back whole lines.

    One instance lives for a whole chat session and moves through four states,
    each with one owner of the keyboard:

    * **stopped** — no raw mode, nothing on screen.
    * **idle** (:meth:`start`) — the box is drawn and :meth:`read` waits for a
      line. The caller's main loop sits here between turns.
    * **listening** (:meth:`listen`) — the box stays drawn and a background
      thread keeps accepting text, *queueing* each line. This is the state a
      turn runs in, which is why a message typed while the agent works is kept
      and sent next rather than thrown away.
    * **paused** (:meth:`pause`) — the box is off the screen and the terminal
      is back in cooked mode, because something else needs a real prompt: an
      approval diff, ``ask_user``, the picker.

    Everything is idempotent, so callers never have to check first; every
    terminal-mode change is undone on :meth:`close`, including on an exception.
    """

    def __init__(
        self,
        console: Console,
        *,
        meta: str = "",
        label: str = SEND_LABEL,
        hint: str = "",
        read_key: Callable[[], str] | None = None,
        unicode_ok: bool | None = None,
        refresh_per_second: float = 12.0,
        screen: ChatScreen | None = None,
    ) -> None:
        self.console = console
        #: The shared region the box draws into, when there is one. Two
        #: ``rich`` Lives on one console fight over the same rows — the second
        #: is nested and never renders — so the box joins the region instead of
        #: opening a live of its own.
        self.screen = screen
        self._showing = False
        self.meta = meta
        self.hint = hint
        self.label = label
        self.placeholder = IDLE_PLACEHOLDER
        self.state = Composer()
        #: Lines queued while a turn runs, in order.
        self.pending: list[str] = []
        #: Set by Ctrl-C with text in the box, cleared once the turn obeys it.
        self.interrupted = False
        #: Set by Ctrl-D while a turn runs: leave once it is over.
        self.eof_requested = False
        #: The SIGINT queue, when the caller installed one.
        self.sigint: SigintQueue | None = None
        #: A scripted reader (tests, and any caller that brings its own).
        self._external = read_key
        self._read_key = read_key
        #: Set when a real terminal was opened; ``None`` for a scripted reader.
        self._keys_obj: Keys | None = None
        self._unicode = supports_unicode(console.file) if unicode_ok is None else unicode_ok
        self._live: Live | None = None
        self._keys: contextlib.AbstractContextManager[Keys] | None = None
        self._pump: threading.Thread | None = None
        self._stop = threading.Event()
        self._busy = False
        self.refresh_per_second = refresh_per_second

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> LiveComposer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def started(self) -> bool:
        """Whether a keyboard is ours: real cbreak mode, or a scripted reader."""
        return self._keys is not None or self._read_key is not None

    @property
    def terminal(self) -> bool:
        """Whether this box owns a real terminal (and therefore its signals)."""
        return self._keys is not None

    @property
    def showing(self) -> bool:
        """Whether the box is currently on screen."""
        return self._live is not None or self._showing

    @property
    def listening(self) -> bool:
        return self._pump is not None and self._pump.is_alive()

    def start(self) -> LiveComposer:
        """Take the keyboard and draw the box. Raises :class:`Unavailable`."""
        self._enter_raw()
        self._draw()
        return self

    def _enter_raw(self) -> None:
        if self._read_key is not None or self._keys is not None:
            return
        try:
            context = raw_keys()
            keys = context.__enter__()
        except (*_MODE_ERRORS, AttributeError, ValueError) as exc:
            raise Unavailable(str(exc)) from exc
        self._keys = context
        self._keys_obj = keys
        self._read_key = keys.read

    def _leave_raw(self) -> None:
        """Drop cbreak mode, keeping a scripted reader if one was supplied."""
        if self._keys is not None:
            with contextlib.suppress(Exception):
                self._keys.__exit__(None, None, None)
            self._keys = None
        self._keys_obj = None
        self._read_key = self._external

    def close(self) -> None:
        """Erase the box and give the keyboard back. Always safe to call."""
        self.stop_listening()
        self._erase()
        self._showing = False
        self._leave_raw()
        self._read_key = None

    def _draw(self) -> None:
        if self.screen is not None:
            self.screen.set_box(self.render)
            self.screen.refresh()
            self._showing = True
            return
        if self._live is not None:
            self.refresh()
            return
        self._live = Live(
            self.render(),
            console=self.console,
            transient=True,
            refresh_per_second=self.refresh_per_second,
            vertical_overflow="visible",
        )
        self._live.start()

    def _erase(self) -> None:
        if self.screen is not None:
            if self._showing:
                self.screen.set_box(None)
                self.screen.refresh()
            self._showing = False
            return
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    # -- presenting --------------------------------------------------------
    def set_mode(
        self,
        *,
        meta: str | None = None,
        hint: str | None = None,
        placeholder: str | None = None,
        label: str | None = None,
        busy: bool | None = None,
    ) -> None:
        """Change what the box says and, if it is showing, redraw it."""
        if meta is not None:
            self.meta = meta
        if hint is not None:
            self.hint = hint
        if placeholder is not None:
            self.placeholder = placeholder
        if label is not None:
            self.label = label
        if busy is not None:
            self._busy = busy
        self.refresh()

    def notify(self, message: str) -> None:
        """Print a line *above* the box and put the box back underneath.

        The live region is stopped first, which erases it, and started again
        afterwards: rich's handling of a print *during* a live region depends
        on how the console was built, and a notice landing inside the box — or
        erasing it — is exactly the kind of glitch this rewrite exists to
        remove. The layers are kept, so restarting restores the status line and
        the answer being streamed exactly as they were.
        """
        if self.screen is not None:
            self.screen.stop()
            self.console.print(message, highlight=False)
            if self.started:
                self.screen.start()
            return
        self._erase()
        self.console.print(message, highlight=False)
        if self.started:
            self._draw()

    def refresh(self) -> None:
        if self.screen is not None:
            if self._showing:
                self.screen.set_box(self.render)
                self.screen.refresh()
            return
        if self._live is not None:
            self._live.update(self.render(), refresh=True)

    def render(self) -> Text:
        return render_composer(
            self.state,
            width=self.console.width,
            meta=self.meta,
            hint=self.hint,
            placeholder=self.placeholder,
            queued=self.pending,
            unicode_ok=self._unicode,
            label=self.label,
            busy=self._busy,
        )

    # -- states ------------------------------------------------------------
    def pause(self) -> None:
        """Step aside for a real prompt: no box, cooked terminal.

        An approval diff or a picker writes its own prompt and expects the
        line discipline to echo and edit; leaving cbreak on would swallow it.
        """
        self.stop_listening()
        self._erase()
        self._leave_raw()

    def unpause(self) -> None:
        """Come back after :meth:`pause`."""
        self._enter_raw()
        self._draw()

    def stop_listening(self) -> None:
        """Stop the background reader, keeping whatever was half-typed."""
        stop, self._stop = self._stop, threading.Event()
        stop.set()
        pump, self._pump = self._pump, None
        if pump is not None and pump.is_alive():
            pump.join(timeout=1.0)

    def listen(self) -> None:
        """Accept text in the background, queueing each line as it is sent.

        The box stays on screen and says so: while a turn runs, Enter queues
        the line instead of sending it, and Ctrl-C stops the turn and sends
        what has been typed so far.
        """
        if self.listening or self._read_key is None:
            return
        self.set_mode(
            busy=True,
            placeholder=QUEUE_PLACEHOLDER,
            label=QUEUE_LABEL,
            hint="Enter queues \u00b7 Ctrl-C interrupts and sends \u00b7 Esc retracts",
        )
        stop = threading.Event()
        self._stop = stop
        self._pump = threading.Thread(target=self._pump_keys, args=(stop,), daemon=True)
        self._pump.start()

    def _pump_keys(self, stop: threading.Event) -> None:
        """Read keys until stopped, queueing lines. Runs on its own thread.

        The wait is on "is a key ready", not on ``read`` itself: a Ctrl-C
        arrives as a signal, and a thread parked inside ``read(2)`` would never
        come back to look at the signal queue. Every few milliseconds the loop
        wakes, takes any pending signal, and only then reads a key it knows is
        there.
        """
        while not stop.is_set():
            reader = self._read_key
            if reader is None:  # pragma: no cover - defensive
                return
            if self.sigint is not None and self.sigint.take():
                key = "ctrl-c"
            elif self._keys_obj is not None and self._keys_obj.ready is not None:
                if not self._keys_obj.ready(0.05):
                    continue
                try:
                    key = reader()
                except KeyboardInterrupt:
                    key = "ctrl-c"
                except Exception:  # pragma: no cover - terminal went away
                    return
            else:
                try:
                    key = reader()
                except KeyboardInterrupt:
                    key = "ctrl-c"
                except Exception:  # pragma: no cover - terminal went away
                    return
            if stop.is_set():
                return
            if key == "eof":
                self.eof_requested = True
                stop.set()
                return
            outcome = apply_key(self.state, key)
            if outcome in (SUBMIT, INTERRUPT_AND_SEND):
                self._queue_current(interrupt=outcome == INTERRUPT_AND_SEND)
            elif outcome == INTERRUPT:
                self.interrupted = True
            elif outcome == EOF_KEY:
                self.eof_requested = True
                stop.set()
                return
            elif outcome == CLEARED:
                self.console.clear()
            elif key == "esc" and self.state.empty and self.pending:
                self.retract()
            self.refresh()

    def _queue_current(self, *, interrupt: bool = False) -> None:
        """Move what is in the box into the queue, and say so above the box."""
        line = self.state.value
        if interrupt:
            # Ctrl-C means "stop what you are doing": with text, that text is
            # the next message, so it is sent rather than dropped.
            self.interrupted = True
        if not line:
            self.refresh()
            return
        self.state.remember(line)
        self.state.clear()
        self.pending.append(line)
        self.refresh()
        # The transcript record of what will be sent, marked as waiting. This
        # is the only place a queued message is echoed: it goes out later, and
        # printing it twice would make the transcript lie about the order.
        self.notify(f"[bold {ACCENT}]you[/] {_one_line(line)} [{MUTED}](queued, sends next)[/]")

    def take_next(self) -> str | None:
        """The next queued line, removed from the queue."""
        return self.pending.pop(0) if self.pending else None

    def drain_signals(self) -> None:
        """Forget a Ctrl-C that arrived while nothing was reading keys."""
        if self.sigint is not None:
            self.sigint.count = 0

    def retract(self) -> str | None:
        """Take the newest queued message back into the box (Esc).

        Typing ahead is easy to overdo, and a queue nobody can correct is a
        queue people stop trusting. The line comes back editable, exactly as
        it was written, and leaves the queue.
        """
        if not self.pending:
            return None
        line = self.pending.pop()
        self.state.text = line
        self.state.end()
        self.refresh()
        self.notify(f"[{MUTED}]retracted[/] [bold]{_one_line(line)}[/]")
        return line

    # -- reading -----------------------------------------------------------
    def read(self) -> Line:
        """Wait for one line. Raises :class:`ComposerLine` when the box closes.

        The caller treats an empty line as "nothing to send" — an empty Enter,
        or a Ctrl-C on an empty box, which stops whatever is running.
        """
        if self._read_key is None:
            raise Unavailable("no key reader")
        while True:
            key = self._next_key()
            if key == "eof" or (key == "ctrl-d" and self.state.empty):
                self.state.clear()
                self.refresh()
                raise ComposerLine
            outcome = apply_key(self.state, key)
            if outcome in (SUBMIT, INTERRUPT_AND_SEND):
                line = self.state.value
                self.state.remember(line)
                self.state.clear()
                self.refresh()
                return Line(line, cancel=outcome == INTERRUPT_AND_SEND)
            if outcome == INTERRUPT:
                self.refresh()
                return Line("", cancel=True)
            if outcome == CLEARED:
                self.console.clear()
                self.refresh()
                continue
            if outcome == EOF_KEY:  # pragma: no cover - handled above
                raise ComposerLine
            self.refresh()

    def _next_key(self) -> str:
        """The next key, with Esc and Ctrl-C handled where they mean something.

        Esc retracts the newest queued message when there is one and the box is
        empty. Ctrl-C arrives one of two ways depending on the terminal: as
        SIGINT (cbreak still has ``ISIG``) or, on a pty with no controlling
        terminal, as a literal ``\\x03``. Both end up as the key name here,
        because a message written during a turn should not be lost to a signal
        either.
        """
        if self.sigint is not None and self.sigint.take():
            return "ctrl-c"
        try:
            key = self._read_key() if self._read_key is not None else "eof"
        except KeyboardInterrupt:
            return "ctrl-c"
        if key == "esc" and self.pending and self.state.empty:
            self.retract()
            return STAY
        return key


def _one_line(text: str, limit: int = 60) -> str:
    """A queued line as it appears in a notice: one line, bounded."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
