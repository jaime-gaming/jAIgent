"""Locking the chat input while the agent works.

A terminal has no disabled control, so the lock is applied to the line
discipline instead: for the duration of a turn the terminal stops echoing, and
the pending input queue is flushed before the prompt comes back. Keystrokes
typed while the agent works are therefore neither printed into the transcript
nor left waiting to answer the next question.

``ISIG`` is left alone, so Ctrl-C still interrupts. Anything that asks the user
something — an approval diff, ``ask_user`` — releases the lock for the length
of that prompt and takes it again afterwards.

Every way this can fail to apply (a pipe instead of a terminal, a console that
refuses the mode change) degrades to doing nothing rather than raising in the
middle of a turn.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
from types import TracebackType
from typing import Any

if sys.platform == "win32":  # pragma: no cover - not exercised on Linux CI
    _IS_WINDOWS = True
else:
    import termios

    _IS_WINDOWS = False

#: What touching the line discipline can raise on POSIX.
_TTY_ERRORS: tuple[type[BaseException], ...] = (
    () if _IS_WINDOWS else (OSError, ValueError, termios.error)
)

#: ``GetStdHandle(STD_INPUT_HANDLE)``; only meaningful on a Windows console.
_STD_INPUT_HANDLE = -10
#: Typing is shown by the console itself; clearing this flag hides it.
_ENABLE_ECHO_INPUT = 0x0004

#: Bound under the platform check: ``ctypes.windll`` exists only on Windows.
_KERNEL32: Any
if sys.platform == "win32":  # pragma: no cover - Windows console only
    _KERNEL32 = ctypes.windll.kernel32
else:
    _KERNEL32 = None


class InputLock:
    """Silence and discard chat keystrokes until released.

    Usage::

        lock = InputLock()
        lock.acquire()          # the turn starts
        ...
        lock.release()          # the prompt comes back, empty

    Engages only when stdin is a terminal; a piped or scheduled run has no
    keyboard to lock, and changing the mode of a pipe is an error.
    """

    def __init__(self) -> None:
        #: The termios attributes to put back, once the lock is released.
        self._saved_attrs: list[Any] | None = None
        #: The same thing on a Windows console: the saved input mode.
        self._saved_mode: int | None = None
        self._engaged = False

    # ------------------------------------------------------------------
    @property
    def supported(self) -> bool:
        """Whether locking can engage here at all.

        Only stdin matters: that is the side being locked, so a run whose
        output is piped still has typeahead worth discarding.
        """
        return _is_tty(sys.stdin)

    @property
    def locked(self) -> bool:
        """Whether the keyboard is currently silenced."""
        return self._engaged

    # ------------------------------------------------------------------
    def acquire(self) -> bool:
        """Silence the keyboard. Returns whether it actually engaged.

        Acquiring twice is a no-op, so a tool that pauses and resumes the lock
        cannot leave the terminal in a half-changed state.
        """
        if self._engaged:
            return True
        if not self.supported:
            return False
        if _IS_WINDOWS:  # pragma: no cover - Windows console only
            self._engaged = self._acquire_windows()
        else:
            self._engaged = self._acquire_posix()
        return self._engaged

    def release(self) -> None:
        """Discard anything typed while locked and give the keyboard back.

        The flush matters as much as the mode change: without it, a line typed
        during the turn would still be in the queue, waiting to be read as the
        answer to a prompt the user has not seen.
        """
        if not self._engaged:
            return
        self._engaged = False
        if _IS_WINDOWS:  # pragma: no cover - Windows console only
            self._release_windows()
        else:
            self._release_posix()

    def __enter__(self) -> InputLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # ------------------------------------------------------------------
    # POSIX
    # ------------------------------------------------------------------
    def _acquire_posix(self) -> bool:
        """Clear ``ECHO`` on stdin, keeping canonical mode and signals."""
        fd = _fd(sys.stdin)
        if fd is None:
            return False
        try:
            saved = termios.tcgetattr(fd)
            # lflag is the fourth element. Only the echo bit changes, so line
            # editing and Ctrl-C behave as before.
            attrs = list(saved)
            attrs[3] = attrs[3] & ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except _TTY_ERRORS:
            return False
        self._saved_attrs = list(saved)
        return True

    def _release_posix(self) -> None:
        fd = _fd(sys.stdin)
        if fd is None:
            return
        with contextlib.suppress(*_TTY_ERRORS):
            termios.tcflush(fd, termios.TCIFLUSH)
        saved, self._saved_attrs = self._saved_attrs, None
        if saved is not None:
            with contextlib.suppress(*_TTY_ERRORS):
                termios.tcsetattr(fd, termios.TCSANOW, saved)

    # ------------------------------------------------------------------
    # Windows
    # ------------------------------------------------------------------
    def _acquire_windows(self) -> bool:  # pragma: no cover - Windows console only
        kernel32 = _KERNEL32
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # redirected input: nothing to lock
        if not kernel32.SetConsoleMode(handle, mode.value & ~_ENABLE_ECHO_INPUT):
            return False
        self._saved_mode = int(mode.value)
        return True

    def _release_windows(self) -> None:  # pragma: no cover - Windows console only
        kernel32 = _KERNEL32
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        kernel32.FlushConsoleInputBuffer(handle)
        saved, self._saved_mode = self._saved_mode, None
        if saved is not None:
            kernel32.SetConsoleMode(handle, saved)


def _is_tty(stream: object) -> bool:
    """``stream.isatty()`` without trusting the object to have it."""
    check = getattr(stream, "isatty", None)
    if check is None:
        return False
    try:
        return bool(check())
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False


def _fd(stream: object) -> int | None:
    """A usable file descriptor for ``stream``, or ``None``."""
    fileno = getattr(stream, "fileno", None)
    if fileno is None:
        return None
    try:
        return int(fileno())
    except (OSError, ValueError, TypeError):
        return None
