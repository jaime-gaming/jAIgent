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
import time
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from jaigent.branding import ACCENT, ACCENT_DIM, FLASH, INK, MUTED
from jaigent.safe_text import one_line
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

#: What a terminal wraps a paste in, when it has been asked to mark them.
_BRACKET_START = b"\x1b[200~"
_BRACKET_END = b"\x1b[201~"

#: How many extra reads to spend on a paste whose closing marker has not
#: arrived yet. A real paste lands in a couple of writes.
_PASTE_WAIT_TRIES = 20

#: The shortest run of *text* that counts as a paste when it holds no newline:
#: a copied URL, a sentence dropped in from a browser. Shorter bursts are
#: typing, and a typist must still see their own characters arrive one by one.
_PASTE_MIN = 64


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
        """The next key's name, or ``"eof"`` when the terminal closes.

        A pasted block comes back as one name, ``paste:<text>``, because the
        caller has to insert it in one go: a character at a time would redraw
        the box per character, and every newline in the block would read as
        Enter and send a fragment of it.
        """
        while not self._buffer and not self._fill():
            return "eof"
        if self._buffer.startswith(_BRACKET_START):
            return self._take_bracketed()
        if not self._more_now() and self._looks_pasted():
            text = self._buffer.decode("utf-8", "replace")
            self._buffer = b""
            return PASTE + text
        first = self._buffer[0]
        if first == 0x1B:
            return self._take_escape()
        if first < 0x80:
            # Control characters and ASCII printables are one keystroke each.
            self._buffer = self._buffer[1:]
            return key_name(chr(first))
        return self._take_character(first)

    def _looks_pasted(self) -> bool:
        """Whether the buffered bytes are a pasted block rather than keys.

        The test has to be strict: two arrow keys arrive in one read and must
        stay two keys, and so must a burst of ordinary typing. A block
        qualifies when it holds a newline (nobody presses Enter and another
        key in the same instant) or when it is far too long for a keyboard to
        have produced it in one read.
        """
        if len(self._buffer) == 1:
            # A single byte is a single key, Enter included.
            return False
        if b"\n" in self._buffer or b"\r" in self._buffer:
            return True
        return len(self._buffer) >= _PASTE_MIN

    def _take_bracketed(self) -> str:
        """Read a bracketed paste, waiting for a closing marker that may lag."""
        tries = 0
        while True:
            text = self._buffer.decode("utf-8", "replace")
            end = text.find(_BRACKET_END.decode())
            if end != -1:
                self._buffer = text[end + len(_BRACKET_END) :].encode()
                return PASTE + text[len(_BRACKET_START) : end]
            tries += 1
            if tries > _PASTE_WAIT_TRIES:
                break
            if self._more is not None and not self._more_now():
                break
            if not self._fill():
                break
        # Never closed: keep what arrived instead of dropping the text.
        text = self._buffer[len(_BRACKET_START) :].decode("utf-8", "replace")
        self._buffer = b""
        return PASTE + text

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
    elif key.startswith(PASTE):
        # The block goes in whole. Newlines are flattened: the box holds one
        # line, and a paste must not press Enter on the user's behalf.
        pasted = " ".join(key[len(PASTE) :].split())
        if pasted:
            state.insert(pasted)
    elif key in {"esc", "tab", "insert", STAY, "eof"}:
        pass  # reserved, or not text: completion and multi-line editing
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


#: A whole pasted block, carried as one key: ``paste:<text>``.
PASTE = "paste:"

#: A motion frame: the lighter glyph travelling along the top border.
MOTION_FRAMES: tuple[str, ...] = ("⠂", "⠒", "⠲", "⠴", "⠦", "⠖", "⠒", "⠐")

#: Seconds between frames of the travelling highlight. Slow enough to read as
#: a glow, fast enough to look alive.
MOTION_INTERVAL = 0.09

#: How often an idle box looks around: mostly to notice that the window was
#: resized, which no keystroke announces. Four times a second is nothing.
IDLE_INTERVAL = 0.25

#: How long an acknowledgement — "queued", "retracted" — stays inside the box.
#: Long enough to read twice, short enough to be gone before the reply starts.
NOTICE_SECONDS = 2.5

#: How long the frame stays lit after Enter. Short: it is a confirmation, not
#: a state, and the eye should catch it without waiting for it.
FLASH_SECONDS = 0.18
ASCII_MOTION_FRAMES: tuple[str, ...] = (".", ":", "*", "o", "*", ":", ".", " ")


def _cells(text: str) -> int:
    """Display width of ``text``: East Asian characters take two cells."""
    return sum(_char_cells(char) for char in text)


def _char_cells(char: str) -> int:
    """Display width of one character: 0 for a combining mark, 2 if wide."""
    if not char:
        return 0
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _clip(text: str, room: int) -> str:
    """``text`` cut to ``room`` cells, never splitting a wide character."""
    return _take(text, room)


def _window(text: str, cursor: int, room: int) -> tuple[str, int]:
    """The visible slice of ``text`` and the cursor's column inside it.

    Room is in cells, not characters, and a wide character is two of them: a
    line of CJK or emoji must not push the frame out of shape. The rule is the
    one every text field uses — show as much as fits, ending where the cursor
    is when the cursor is past the right edge, and starting at the cursor when
    it is past the left one.
    """
    if _cells(text) <= room:
        return text, _cells(text[:cursor])

    # The slice's start: far enough back that the cursor fits at the end.
    start = 0 if cursor <= room else cursor - room
    start = _advance_to_fit(text, start, room)
    shown = _take(text[start:], room)
    if cursor < start:  # pragma: no cover - start is never past the cursor
        start = cursor
        shown = _take(text[start:], room)
    # Clamp: the cursor may sit inside a wide character, and a half-glyph is
    # not something a terminal can draw.
    return shown, min(_cells(text[start:cursor]), _cells(shown))


def _window_at(text: str, cursor: int, room: int) -> tuple[str, int]:
    """Like :func:`_window`, but also return where the slice began."""
    if _cells(text) <= room:
        return text, 0
    start = 0 if cursor <= room else cursor - room
    if cursor < start:  # pragma: no cover - defensive
        start = cursor
    return _take(text[start:], room), start


def _take(text: str, room: int) -> str:
    """The longest prefix of ``text`` that fits in ``room`` cells."""
    out: list[str] = []
    used = 0
    for char in text:
        width = _char_cells(char)
        if used + width > room:
            break
        out.append(char)
        used += width
    return "".join(out)


def _advance_to_fit(text: str, start: int, room: int) -> int:
    """Nudge ``start`` so the tail it leaves fits ``room`` cells cleanly.

    Without this, starting in the middle of a wide character would leave half
    of it on screen; the slice simply starts one cell later instead.
    """
    while start < len(text) and _cells(_take(text[start:], room + 1)) < room + 1:
        break
    return start


def _window_positions(text: str, cursor: int, room: int) -> tuple[str, int]:
    """The visible slice and the cursor's column in cells, for drawing.

    The column is measured from the slice's own start, and a fractional column
    (the cursor inside a wide character) is clamped back to its left edge: an
    inverted half-glyph is not something a terminal can draw.
    """
    shown, start = _window_at(text, cursor, room)
    column = _cells(text[start:cursor])
    return shown, min(column, _cells(shown))


def render_composer(
    state: Composer,
    *,
    width: int,
    meta: str = "",
    placeholder: str = IDLE_PLACEHOLDER,
    queued: Sequence[str] = (),
    unicode_ok: bool = True,
    label: str = SEND_LABEL,
    busy: bool = False,
    phase: int = 0,
    caret: bool = False,
    animate: bool = True,
    notice: str = "",
    flash: bool = False,
) -> Text:
    """Draw the box: an optional hint line, the framed line, and the footer.

    ``notice`` is a short, transient message drawn *inside* the frame, above
    the line and right-aligned — empty when there is nothing to say. It is how
    "queued" and "retracted" are acknowledged: a message that appears and goes
    away on its own, taking no row from the transcript and never becoming part
    of it.

    ``meta`` is the session's context on the left of the footer when idle;
    ``label`` is what Enter does right now. Both are dropped whole on a
    terminal too narrow for them — a cut-off "Enter: se" reads as a bug.

    ``busy`` turns the frame dim and slides a travelling highlight along the
    top border (``phase`` advances it); ``animate=False`` keeps it still, for
    ``--no-animation``, ``CI`` and dumb terminals.

    ``width`` is a hard limit, measured in cells: the line scrolls under the
    cursor rather than wrapping, because a box that grows a row per keystroke
    walks the transcript off the top of the screen.
    """
    width = max(12, width)
    frame = _frame(unicode_ok)
    mark = prompt_mark(unicode_ok=unicode_ok)
    border = ACCENT_DIM if busy else ACCENT
    if flash:
        # The brief light after Enter. It outranks the busy dim, because it
        # is news and the dim is just the resting state.
        border = FLASH

    inner = width - 2  # the two frame characters
    prefix = f" {mark} "
    room = max(1, inner - _cells(prefix) - 1)

    body = Text()
    if state.empty:
        body.append(_hint(placeholder, room), style=MUTED)
    else:
        shown, cursor_col = _window_positions(state.text, state.cursor, room)
        at_end = cursor_col >= _cells(shown)
        if caret and at_end and _cells(shown) >= room:
            # The caret takes a cell of its own when it sits past the last
            # character. Give it one by scrolling the text a cell further:
            # drawing it without the room is how the row grew a cell past
            # the frame at narrow widths.
            shown, cursor_col = _window_positions(state.text, state.cursor, max(1, room - 1))
            at_end = True
        # The caret's column is counted in cells, the characters are not:
        # comparing the two directly put the caret on the wrong glyph in a
        # line of CJK, where one character is two cells wide.
        column = 0
        for char in shown:
            width_here = _char_cells(char)
            if caret and column <= cursor_col < column + width_here:
                body.append(char if char.strip() else " ", style=_CURSOR_STYLE)
            else:
                body.append(char, style=INK)
            column += width_here
        if caret and at_end:
            body.append(" ", style=_CURSOR_STYLE)

    padding = room - body.cell_len
    line = Text()
    line.append(frame["v"], style=border)
    line.append(prefix, style=f"bold {ACCENT}")
    line.append_text(body)
    if padding > 0:
        line.append(" " * padding)
    line.append(" ", style=border)
    line.append(frame["v"], style=border)

    out = Text()
    if notice:
        out.append_text(_notice_row(frame, inner, border, notice))
        out.append("\n")
    top = _top_border(
        frame, inner, border, busy=busy, phase=phase, animate=animate, unicode_ok=unicode_ok
    )
    out.append_text(top)
    out.append("\n")
    out.append_text(line)
    out.append("\n")
    out.append_text(_footer(frame, inner, border, meta=meta, label=label, queued=queued, busy=busy))
    return out


def _hint(text: str, room: int) -> str:
    """The placeholder, cut at a word when it does not fit whole.

    ``Ask anything, or ta`` reads like a bug; ``Ask anything,`` reads like a
    hint that was shortened on purpose. When even the first word does not fit,
    the hard cut is the honest answer.
    """
    if _cells(text) <= room:
        return text
    kept = ""
    for word in text.split(" "):
        candidate = f"{kept} {word}".strip()
        if _cells(candidate) > room - 1:
            break
        kept = candidate
    return f"{kept}…" if kept else _clip(text, room)


def _notice_row(frame: dict[str, str], inner: int, border: str, notice: str) -> Text:
    """A hint drawn inside the frame: blank cells, the note on the right.

    It sits above the top border rather than under the bottom one so the
    footer — which carries permanent information — never moves, and so the
    note is the closest thing on screen to what it is about. Its left edge is
    the frame's, not the screen's: a line that starts at column zero is not
    "inside" anything.
    """
    text = _clip(notice, max(0, inner - 4))
    pad = max(0, inner - 1 - _cells(text))
    row = Text()
    row.append(" " * pad, style=border)
    row.append(f" {text} ", style=MUTED)
    row.append(frame["v"], style=border)
    return row


#: The character under the cursor is drawn inverted: near-white on accent.
_CURSOR_STYLE = f"reverse bold {ACCENT}"

#: The bright core of the travelling highlight. A style of its own rather than
#: ``ACCENT``, because ``ACCENT`` is also the border colour of an idle box.
FIRE = f"bold {FLASH}"


def _invert(char: str) -> str:
    """A space stays a visible block; anything else is drawn as itself."""
    return char if char.strip() else " "


def _top_border(
    frame: dict[str, str],
    inner: int,
    border: str,
    *,
    busy: bool,
    phase: int,
    animate: bool,
    unicode_ok: bool,
) -> Text:
    """The top rule, with a travelling highlight while a turn runs.

    Three steps of brightness, not a gradient: a terminal has no alpha, so the
    glow is drawn as a bright core with a dimmer shoulder either side, and it
    is the *widths* that make it read as light rather than as a marker. The
    glow is deliberately narrow — a third of the border lit up looks like a
    progress bar, not like something moving.
    """
    if not busy or not animate or inner < 12:
        return Text(frame["tl"] + frame["h"] * inner + frame["tr"], style=border)

    glow = max(4, min(10, inner // 6))
    core = max(2, glow // 2)
    # One cell per frame, travelling out past the right corner and back in
    # from the left: it slides off the end and returns, rather than hopping
    # from one side to the other.
    start = phase % (inner + glow)
    text = Text()
    text.append(frame["tl"], style=border)
    for offset in range(inner):
        distance = abs(offset - start)
        if distance < core:
            text.append(frame["h"], style=FIRE)
        elif distance < glow:
            # The shoulder. ACCENT, not the border colour: with the busy
            # border already dimmed, a shoulder in the same colour is
            # invisible and the glow looks like a hard-edged block.
            text.append(frame["h"], style=ACCENT)
        else:
            text.append(frame["h"], style=border)
    text.append(frame["tr"], style=border)
    return text


def _footer(
    frame: dict[str, str],
    inner: int,
    border: str,
    *,
    meta: str,
    label: str,
    queued: Sequence[str],
    busy: bool,
) -> Text:
    """The bottom rule, doubling as the box's status bar.

    Idle, the left end says what is answering. Busy, that is already on the
    status line above, so the same cell is spent on the queue count and on
    what Enter does — the two answers a person typing ahead actually needs.
    Barely fits? Whole pieces are dropped, in order of least importance, and
    the corners never move, so the box keeps its shape at any width.
    """
    waiting = queue_summary(queued)
    shown_meta = f" {meta} " if meta else ""
    shown_queue = f" {waiting} " if waiting else ""
    shown_label = f" {label} " if label else ""
    # (left, middle, right) for each attempt, most complete first.
    plans: list[tuple[str, str, str]] = []
    if busy:
        plans.append(("", shown_queue, shown_label))
        plans.append(("", shown_queue, ""))
        plans.append(("", "", ""))
        if meta:
            plans.append((shown_meta, "", ""))
    else:
        plans.append((shown_meta, shown_queue, shown_label))
        plans.append((shown_meta, "", shown_label))
        plans.append((shown_meta, "", ""))
        plans.append(("", "", ""))

    left = middle = right = ""
    for candidate_left, candidate_middle, candidate_right in plans:
        if _cells(candidate_left) + _cells(candidate_middle) + _cells(candidate_right) <= inner - 1:
            left, middle, right = candidate_left, candidate_middle, candidate_right
            break

    footer = Text()
    footer.append(frame["bl"], style=border)
    fill = inner - _cells(left) - _cells(middle) - _cells(right)
    footer.append(left, style=f"bold {ACCENT}")
    if middle:
        # What is waiting is news, not chrome.
        footer.append(middle, style=MUTED)
    footer.append(frame["h"] * max(0, fill), style=border)
    footer.append(right, style=border if busy else ACCENT)
    footer.append(frame["br"], style=border)
    return footer


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
        #: A short message shown inside the frame, and when it expires. A
        #: notice is an acknowledgement, not a record: it appears above the
        #: line for a few seconds and goes, so the transcript stays about the
        #: conversation.
        self._notice = ""
        self._notice_until = 0.0
        #: The border style a just-sent line leaves behind for a moment.
        self._flash: str | None = None
        self._flash_until = 0.0
        #: Motion: the frame index, and the ticker that advances it.
        self._phase = 0
        self._motion: threading.Thread | None = None
        self._motion_stop: threading.Event | None = None
        #: The width the box was last drawn for, to notice a resize.
        self._width = console.width
        self._animate = True
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
        self._stop_motion()
        self._erase()
        self._showing = False
        self._leave_raw()
        self._read_key = None

    def _draw(self) -> None:
        # The ticker keeps the box honest about the window's size even while
        # nothing is happening; started here so every draw path has it.
        self._start_motion()
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
        placeholder: str | None = None,
        label: str | None = None,
        busy: bool | None = None,
        animate: bool | None = None,
    ) -> None:
        """Change what the box says and, if it is showing, redraw it."""
        if meta is not None:
            self.meta = meta
        if animate is not None:
            self._animate = animate
            if animate:
                self._start_motion()
            else:
                self._stop_motion()
        if placeholder is not None:
            self.placeholder = placeholder
        if label is not None:
            self.label = label
        if busy is not None:
            self._busy = busy
        self.refresh()

    def notify(self, message: str, *, markup: bool = False) -> None:
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
            self.console.print(message, highlight=False, markup=markup)
            if self.started:
                self.screen.start()
            return
        self._erase()
        self.console.print(message, highlight=False, markup=markup)
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
            placeholder=self.placeholder,
            queued=self.pending,
            unicode_ok=self._unicode,
            label=self.label,
            busy=self._busy,
            phase=self._phase,
            # Always, including while a turn runs: a box that keeps taking
            # text but hides the caret makes that text feel unowned.
            caret=True,
            animate=self._animate,
            notice=self.notice,
            flash=self.flashing,
        )

    @property
    def notice(self) -> str:
        """The acknowledgement currently showing, if it has not expired."""
        return self._notice if time.monotonic() < self._notice_until else ""

    def flash(self, seconds: float = FLASH_SECONDS) -> None:
        """Close the border in a bright colour for a moment, then let it go.

        The box's answer to Enter: a line that vanishes without a trace reads
        like a dropped keystroke, and a printed confirmation would be one more
        row of chrome. The whole frame lights up instead, for a fifth of a
        second, and the motion ticker puts it back.
        """
        self._flash = FLASH
        self._flash_until = time.monotonic() + seconds
        self.refresh()
        self._start_motion()

    @property
    def flashing(self) -> bool:
        """Whether the frame is still lit from the last send."""
        if self._flash is None:
            return False
        if time.monotonic() >= self._flash_until:
            self._flash = None
            return False
        return True

    def say(self, message: str, *, seconds: float = NOTICE_SECONDS) -> None:
        """Acknowledge an action inside the box, for a moment.

        Used for "queued" and "retracted": the feedback has to be immediate
        and close to the thing it is about, and it must not survive as a line
        in the conversation the user later scrolls back through.
        """
        self._notice = message
        self._notice_until = time.monotonic() + seconds
        self.refresh()
        self._start_motion()

    # -- motion ------------------------------------------------------------
    def _start_motion(self) -> None:
        """Advance the travelling highlight a few times a second.

        Only while there is something to say with it: a timer spinning for an
        idle prompt burns a wake-up per frame for a box that is not moving.
        """
        if self._motion is not None:
            return
        stop = threading.Event()
        self._motion_stop = stop
        self._motion = threading.Thread(target=self._tick, args=(stop,), daemon=True)
        self._motion.start()

    def _stop_motion(self) -> None:
        stop, self._motion_stop = self._motion_stop, None
        thread, self._motion = self._motion, None
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)

    def _tick(self, stop: threading.Event) -> None:
        """Keep the box current: motion, notices, and the window's size.

        One timer for all three, because they are the same kind of thing —
        something that changes without a key being pressed. While the turn
        runs it ticks at the frame rate; idle it slows to a glance, which is
        enough to notice a resize and cheap enough to leave running for a whole
        session.
        """
        while not stop.wait(MOTION_INTERVAL if self._busy else IDLE_INTERVAL):
            if not self.showing:
                continue
            redraw = False
            if self._busy and self._animate:
                self._phase += 1
                redraw = True
            if self._notice and time.monotonic() >= self._notice_until:
                self._notice = ""
                redraw = True
            if self._flash is not None and not self.flashing:
                redraw = True
            if self.console.width != self._width:
                # The terminal was resized: nothing tells us but the width
                # itself, and a box drawn for the old size is a broken box.
                self._width = self.console.width
                self._redraw_after_resize()
                continue
            if redraw:
                self.refresh()

    def _redraw_after_resize(self) -> None:
        """Redraw at the new size.

        The frame from the old width is erased as far as it can be — a
        terminal that reflowed it into more rows leaves a remnant in the
        scrollback, which is what any resized transcript looks like — and the
        new frame is drawn for the width that is actually there.
        """
        self._erase()
        self._draw()

    def _motion_for(self, busy: bool) -> None:
        """Point the ticker at the turn's state.

        The ticker runs whenever the box is on screen — idle it only watches
        the window's size — and moves the frame only while ``busy``. Without
        animation the ticker still runs, because a resize is not decoration.
        """
        if busy:
            self._start_motion()
        else:
            self._phase = 0

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
        """Stop the background reader and the animation, keeping the text."""
        stop, self._stop = self._stop, threading.Event()
        stop.set()
        pump, self._pump = self._pump, None
        if pump is not None and pump.is_alive():
            pump.join(timeout=1.0)
        self._motion_for(False)

    def listen(self) -> None:
        """Accept text in the background, queueing each line as it is sent.

        The box stays on screen and says so: while a turn runs, Enter queues
        the line instead of sending it, and Ctrl-C stops the turn and sends
        what has been typed so far.
        """
        if self.listening or self._read_key is None:
            return
        self.set_mode(busy=True, placeholder=QUEUE_PLACEHOLDER, label=QUEUE_LABEL)
        self._motion_for(True)
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
        self.flash()
        # The transcript record of what will be sent, marked as waiting: this
        # is the only echo, because printing it twice would make the transcript
        # lie about the order. It goes above the box like the rest of the
        # conversation, so the reply that follows reads as an answer to it.
        preview = one_line(line, limit=60)
        self.notify(
            f"[bold {ACCENT}]you[/] {preview} [{MUTED}](queued, sends next)[/]", markup=True
        )
        self.say("queued")

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
        self.say("sent back to the box")
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
                # Layered, in the order they happen: the flash lights the
                # frame, then the box settles back to its idle shape.
                self.flash()
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
