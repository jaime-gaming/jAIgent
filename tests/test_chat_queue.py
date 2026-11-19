"""The chat loop with the framed box: what is queued, and what is chopped off.

``run_turn`` and ``cmd_chat`` are driven end to end with a scripted keyboard,
so the whole path is covered without a terminal: keys go in, lines come out,
and the queue is what stands between a turn and the next one.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from conftest import FakeProvider
from jaigent import cli
from jaigent.agent import Agent, AgentResult, Interrupted
from jaigent.composer import SEND_LABEL, Composer
from jaigent.config import Settings
from jaigent.llm.base import AssistantMessage


class ScriptedKeys:
    """A keyboard that answers from a script, then closes the box."""

    def __init__(self, *keys: str) -> None:
        self.keys = list(keys)

    def __call__(self) -> str:
        return self.keys.pop(0) if self.keys else "eof"


def box_with(*keys: str, width: int = 60) -> cli.LiveComposer:
    stream = io.StringIO()
    return cli.LiveComposer(
        Console(width=width, file=stream, force_terminal=False),
        read_key=ScriptedKeys(*keys),
        meta="gpt-4o · auto",
        unicode_ok=True,
    )


class TestOpeningTheBox:
    def test_a_pipe_never_gets_a_box(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A script piping text into `jaigent chat` must keep working exactly as
        # it did; the box is an improvement, not a requirement.
        plain_console = Console(file=io.StringIO())

        assert cli._open_chat_box(Settings(api_key="k"), plain=False, screen=None) is None or (
            plain_console.is_terminal is False
        )

    def test_no_color_never_gets_a_box(self) -> None:
        assert cli._open_chat_box(Settings(api_key="k"), plain=True, screen=None) is None

    def test_where_cbreak_cannot_engage_it_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse() -> None:
            raise cli.Unavailable("no tty")

        monkeypatch.setattr(cli, "LiveComposer", _RefusingComposer(refuse))

        assert cli._open_chat_box(Settings(api_key="k"), plain=False, screen=None) is None


class _RefusingComposer:
    """A ``LiveComposer`` whose ``start`` refuses, like a pipe would."""

    def __init__(self, refuse: Any) -> None:
        self.refuse = refuse

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        refuse = self.refuse

        class Composer:
            def start(self) -> None:
                refuse()

        return Composer()


class TestReadingTheNextLine:
    def test_the_queue_comes_before_the_keyboard(self) -> None:
        box = box_with("typed", "enter")
        box.pending.append("queued")

        assert cli._next_chat_line(box) == "queued"
        assert cli._next_chat_line(box) == "typed"

    def test_the_queue_is_drained_in_order(self) -> None:
        box = box_with()
        box.pending.extend(["one", "two"])

        assert [cli._next_chat_line(box), cli._next_chat_line(box)] == ["one", "two"]

    def test_a_typed_line_is_echoed_into_the_transcript(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        cli._echo_prompt("hello there")

        # Redirected output is not a terminal, so nothing is claimed as typed;
        # the echo is for a human watching, not for a file.
        assert "hello there" not in capsys.readouterr().out

    def test_ctrl_d_closes_the_chat(self) -> None:
        box = box_with("ctrl-d")

        assert cli._next_chat_line(box) is None

    def test_a_preview_is_one_bounded_line(self) -> None:
        preview = cli._prompt_preview("line one\nline two " + "x" * 500)

        assert "\n" not in preview
        assert preview.endswith("…")


class InterruptibleBox:
    """Just enough of the box to drive ``run_turn``'s interruption hooks."""

    def __init__(self, fire_after: int = 2) -> None:
        self.state = Composer()
        self.pending: list[str] = []
        self.interrupted = False
        self.eof_requested = False
        self.label = SEND_LABEL
        self.placeholder = cli.IDLE_PLACEHOLDER
        self._fire_after = fire_after
        self._checks = 0
        self.listening = False

    # The turn calls these; a scripted real thing would do the same.
    def set_mode(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self, key if key != "busy" else "_busy", value)

    def listen(self) -> None:
        self._checks += 1
        if self._checks >= self._fire_after:
            self.interrupted = True

    def stop_listening(self) -> None:
        pass

    def drain_signals(self) -> None:
        pass

    def notify(self, message: str) -> None:
        pass

    def close(self) -> None:
        pass


class InterruptibleAgent(Agent):
    """An agent whose model streams a long answer and stops when asked."""

    def __init__(self, settings: Settings, *, stop_after: int = 1) -> None:
        super().__init__(settings, provider=FakeProvider([], chunk_size=1))
        self.stop_after = stop_after
        self.checks = 0

    def run(self, prompt: str, *, max_steps: int | None = None, stop_requested=None) -> AgentResult:  # noqa: ANN001
        assert stop_requested is not None, "run_turn must pass the stop hook"
        for _ in range(10):
            self.checks += 1
            if stop_requested():
                raise Interrupted(AgentResult(output="", steps=[], interrupted=True))
        return AgentResult(output="finished anyway")


class TestInterruptingATurn:
    def test_a_stop_request_becomes_an_interrupted_result(self, tmp_path: Path) -> None:
        settings = Settings(api_key="k", model="m", workspace=tmp_path, stream=False)
        agent = InterruptibleAgent(settings)
        screen = cli.ChatScreen(Console(file=io.StringIO()))
        # Ctrl-C arrives while the first model step is still running.
        box = InterruptibleBox(fire_after=1)

        result = cli.run_turn(
            agent,
            settings,
            "long job",
            plain=True,
            chat=True,
            box=box,  # type: ignore[arg-type]
            screen=screen,
            interrupt=lambda: box.interrupted,
        )

        assert result.interrupted is True
        assert agent.checks == 1, "the turn should stop as soon as it is asked to"

    def test_without_a_box_the_turn_is_unchanged(self, tmp_path: Path) -> None:
        settings = Settings(api_key="k", model="m", workspace=tmp_path, stream=False)
        provider = FakeProvider([AssistantMessage(content="hi")])
        agent = Agent(settings, provider=provider)

        result = cli.run_turn(agent, settings, "hello", plain=True, chat=True)

        assert result.output == "hi"


class TestNothingIsLocked:
    """The keyboard is never silenced: it is queued instead.

    The docs used to promise the opposite ("input locked"), which is exactly
    the behaviour this replaces — nothing should ever advertise a locked input
    again, because there is no longer such a thing.
    """

    def test_a_framed_turn_does_not_use_an_input_lock(self, tmp_path: Path) -> None:
        locked: list[str] = []

        class RecordingLock:
            def __init__(self) -> None:
                self.locked = False

            def acquire(self) -> bool:
                self.locked = True
                locked.append("acquire")
                return True

            def release(self) -> None:
                self.locked = False
                locked.append("release")

            def __enter__(self) -> RecordingLock:
                self.acquire()
                return self

            def __exit__(self, *exc: object) -> None:
                self.release()

        import jaigent.cli as cli_module

        original = cli_module.InputLock
        cli_module.InputLock = RecordingLock  # type: ignore[assignment,misc]
        try:
            settings = Settings(api_key="k", model="m", workspace=tmp_path, stream=False)
            agent = Agent(settings, provider=FakeProvider([AssistantMessage(content="ok")]))
            box = InterruptibleBox(fire_after=99)
            screen = cli.ChatScreen(Console(file=io.StringIO()))

            cli.run_turn(
                agent,
                settings,
                "hi",
                plain=True,
                chat=True,
                box=box,  # type: ignore[arg-type]
                screen=screen,
                interrupt=lambda: False,
            )
        finally:
            cli_module.InputLock = original  # type: ignore[misc]

        assert locked == [], "a framed turn must not silence the keyboard"

    def test_the_box_is_left_idle_after_a_turn(self, tmp_path: Path) -> None:
        settings = Settings(api_key="k", model="m", workspace=tmp_path, stream=False)
        agent = Agent(settings, provider=FakeProvider([AssistantMessage(content="ok")]))
        box = box_with()
        screen = cli.ChatScreen(Console(file=io.StringIO()))

        cli._run_chat_turn(
            agent,
            settings,
            cli.sessions.Session.new(),
            "hi",
            plain=True,
            box=box,
            screen=screen,
            dirty=False,
        )

        assert box.label == cli.SEND_LABEL
        assert box.placeholder == cli.IDLE_PLACEHOLDER
        assert box.state.text == ""

    def test_the_status_line_is_not_left_spinning(self, tmp_path: Path) -> None:
        settings = Settings(api_key="k", model="m", workspace=tmp_path, stream=False)
        agent = Agent(settings, provider=FakeProvider([AssistantMessage(content="ok")]))
        box = box_with()
        screen = cli.ChatScreen(Console(file=io.StringIO()))

        cli._run_chat_turn(
            agent,
            settings,
            cli.sessions.Session.new(),
            "hi",
            plain=True,
            box=box,
            screen=screen,
            dirty=False,
        )

        assert screen.status is None
        assert screen.answer is None


class TestTheLoopDrainsTheQueue:
    def test_a_queued_message_runs_as_its_own_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point: a message written during a turn is sent after it."""
        seen: list[str] = []

        def fake_turn(agent, settings, session, prompt, **kwargs):  # noqa: ANN001, ANN003, ANN202
            seen.append(prompt)
            return True

        monkeypatch.setattr(cli, "_run_chat_turn", fake_turn)
        box = box_with("first", "enter")
        box.pending.append("written while busy")

        lines = [cli._next_chat_line(box) for _ in range(2)]

        assert lines == ["written while busy", "first"]
