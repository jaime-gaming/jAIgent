"""The chat input lock: silenced while a turn runs, restored before the prompt.

The unit tests drive the lock against a stub line discipline, so they hold on
any platform and never touch the terminal running the suite. The last test uses
a real pty, because "does not echo and does not survive to the next prompt" is
exactly the kind of claim a stub can only make about itself.
"""

from __future__ import annotations

import contextlib
import os
import sys
import termios
import time
from pathlib import Path
from typing import Any

import pytest

from jaigent.input_lock import InputLock


class FakeTermios:
    """A stand-in for the ``termios`` module that records what it was asked."""

    ECHO = 0o10
    TCSANOW = 0
    TCIFLUSH = 0
    error = termios.error

    def __init__(self, lflag: int = 0o10 | 0o2) -> None:
        self.saved = [0, 0, 0, lflag, 0, 0, []]
        self.set: list[list[Any]] = []
        self.flushed: list[int] = []
        self.fails = False

    def tcgetattr(self, fd: int) -> list[Any]:
        if self.fails:
            raise termios.error("not a terminal")
        return list(self.saved)

    def tcsetattr(self, fd: int, when: int, attrs: list[Any]) -> None:
        if self.fails:
            raise termios.error("not a terminal")
        self.set.append(list(attrs))

    def tcflush(self, fd: int, queue: int) -> None:
        self.flushed.append(queue)


class FakeStream:
    """A stream that claims to be (or not be) a terminal."""

    def __init__(self, *, tty: bool, fd: int = 0) -> None:
        self._tty = tty
        self._fd = fd

    def isatty(self) -> bool:
        return self._tty

    def fileno(self) -> int:
        return self._fd


@pytest.fixture
def fake_tty(monkeypatch: pytest.MonkeyPatch) -> FakeTermios:
    """Pretend stdin is a terminal with a stub line discipline.

    Patched inside a helper rather than from the fixture body: pytest swaps
    ``sys.stdout`` back to its capture stream between phases, so a patch made
    during setup is gone by the time the test runs. ``sys.stdin`` survives.
    """
    fake = FakeTermios()
    monkeypatch.setattr("jaigent.input_lock.termios", fake)
    monkeypatch.setattr(sys, "stdin", FakeStream(tty=True))
    return fake


@pytest.fixture
def piped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", FakeStream(tty=False))


class TestEngaging:
    def test_echo_is_cleared_while_locked(self, fake_tty: FakeTermios) -> None:
        lock = InputLock()

        assert lock.acquire() is True
        assert lock.locked is True
        assert fake_tty.set, "the line discipline was never touched"
        assert not fake_tty.set[0][3] & FakeTermios.ECHO

    def test_release_puts_back_exactly_what_it_found(self, fake_tty: FakeTermios) -> None:
        fake_tty.saved[3] = 0o10 | 0o2 | 0o1  # ECHO, ICANON and ISIG all on
        lock = InputLock()

        lock.acquire()
        lock.release()

        assert lock.locked is False
        assert fake_tty.set[-1][3] == 0o10 | 0o2 | 0o1

    def test_acquiring_twice_touches_the_terminal_once(self, fake_tty: FakeTermios) -> None:
        lock = InputLock()

        lock.acquire()
        lock.acquire()

        assert len(fake_tty.set) == 1

    def test_releasing_without_locking_does_nothing(self, fake_tty: FakeTermios) -> None:
        lock = InputLock()

        lock.release()

        assert fake_tty.set == []
        assert fake_tty.flushed == []

    def test_a_piped_stdout_does_not_stop_the_lock(
        self, monkeypatch: pytest.MonkeyPatch, fake_tty: FakeTermios
    ) -> None:
        """Output going to a file is no reason to leave the keyboard echoing."""
        monkeypatch.setattr(sys, "stdout", FakeStream(tty=False, fd=1))
        lock = InputLock()

        assert lock.acquire() is True


class TestTypeahead:
    def test_pending_keystrokes_are_dropped_before_echo_returns(
        self, fake_tty: FakeTermios
    ) -> None:
        lock = InputLock()
        lock.acquire()

        # Recorded from here on, so the ordering covers the release only.
        order: list[str] = []
        real_setattr = FakeTermios.tcsetattr

        def flush(fd: int, queue: int) -> None:
            order.append(f"flush:{queue}")
            fake_tty.flushed.append(queue)

        def restore(fd: int, when: int, attrs: list[Any]) -> None:
            order.append("restore")
            real_setattr(fake_tty, fd, when, attrs)

        fake_tty.tcflush = flush  # type: ignore[method-assign]
        fake_tty.tcsetattr = restore  # type: ignore[method-assign]

        lock.release()

        assert order == [f"flush:{FakeTermios.TCIFLUSH}", "restore"]


class TestDegradingGracefully:
    def test_a_pipe_is_left_alone(self, piped: None) -> None:
        lock = InputLock()

        assert lock.supported is False
        assert lock.acquire() is False
        assert lock.locked is False
        lock.release()  # and does not raise

    def test_a_terminal_that_refuses_the_change_is_not_locked(self, fake_tty: FakeTermios) -> None:
        fake_tty.fails = True
        lock = InputLock()

        assert lock.acquire() is False
        assert lock.locked is False

    def test_a_stream_without_fileno_is_not_locked(
        self, monkeypatch: pytest.MonkeyPatch, fake_tty: FakeTermios
    ) -> None:
        class NoFd:
            def isatty(self) -> bool:
                return True

        monkeypatch.setattr(sys, "stdin", NoFd())
        lock = InputLock()

        assert lock.acquire() is False

    def test_the_context_manager_always_releases(self, fake_tty: FakeTermios) -> None:
        lock = InputLock()

        with pytest.raises(RuntimeError), lock:
            raise RuntimeError("boom")

        assert lock.locked is False
        assert fake_tty.set[-1][3] & FakeTermios.ECHO


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")
class TestAgainstARealTerminal:
    """The same claims, made to a kernel line discipline instead of a stub."""

    CHILD = """
import sys, time
sys.path.insert(0, {source!r})
from jaigent.input_lock import InputLock

lock = InputLock()
sys.stdout.write("LOCKED=%s\\n" % lock.acquire())
sys.stdout.flush()
time.sleep(2.0)
lock.release()
sys.stdout.write("RELEASED=%s\\n" % lock.locked)
sys.stdout.flush()
sys.stdout.write("NEXT=%r\\n" % sys.stdin.readline())
sys.stdout.flush()
"""

    def test_typing_during_a_turn_is_silent_and_forgotten(self) -> None:
        import pty
        import select

        source = str(Path(__file__).resolve().parent.parent / "src")
        master, slave = pty.openpty()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the forked child
            os.close(master)
            os.setsid()
            for fd in (0, 1, 2):
                os.dup2(slave, fd)
            os.close(slave)
            os.execv(sys.executable, [sys.executable, "-c", self.CHILD.format(source=source)])
        os.close(slave)

        chunks: list[bytes] = []

        def drain(marker: str, timeout: float) -> str:
            """Read until ``marker`` shows up, or give up after ``timeout``."""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                text = b"".join(chunks).decode(errors="replace")
                if marker in text:
                    return text
                readable, _, _ = select.select([master], [], [], 0.05)
                if not readable:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break  # the child closed its end
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode(errors="replace")

        def echo() -> bool:
            """The slave's line discipline is visible from the master side."""
            return bool(termios.tcgetattr(master)[3] & termios.ECHO)

        try:
            started = drain("LOCKED=True", 5.0)
            assert "LOCKED=True" in started, started
            assert echo() is False, "keystrokes would be echoed while the turn runs"

            # Typed while the agent works: invisible now, gone afterwards.
            os.write(master, b"typed while locked\n")
            during = drain("typed while locked", 0.6)
            assert "typed while locked" not in during, during

            # Wait for the release before typing again, or this line is
            # typeahead too and the flush under test would eat it.
            released = drain("RELEASED=False", 5.0)
            assert "RELEASED=False" in released, released
            assert echo() is True, "the terminal was left silent after the turn"

            os.write(master, b"the real answer\n")
            finished = drain("NEXT=", 5.0)
            assert "NEXT='the real answer\\n'" in finished, finished
        finally:
            with contextlib.suppress(OSError):
                os.close(master)
            os.waitpid(pid, 0)
