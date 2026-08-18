"""The interactive surfaces: chat slash commands, init, and the splash."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import FakeProvider
from jaigent import cli
from jaigent.agent import Agent
from jaigent.config import Settings
from jaigent.llm.base import AssistantMessage
from jaigent.session import Session


@pytest.fixture(autouse=True)
def isolated_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "sessions"
    monkeypatch.setenv("JAIGENT_SESSION_DIR", str(directory))
    return directory


@pytest.fixture
def agent(tmp_path: Path) -> Agent:
    settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
    return Agent(settings, provider=FakeProvider([AssistantMessage(content="ok")]))


def slash(command: str, agent: Agent, session: Session | None = None):  # noqa: ANN201
    return cli._handle_slash(command, agent, agent.settings, session or Session.new())


class TestSlashCommands:
    def test_exit_quits(self, agent: Agent) -> None:
        assert slash("/exit", agent).quit is True

    @pytest.mark.parametrize("word", ["/quit", "exit", "quit"])
    def test_exit_aliases(self, agent: Agent, word: str) -> None:
        assert slash(word, agent).quit is True

    def test_help_lists_commands(self, agent: Agent, capsys: pytest.CaptureFixture) -> None:
        slash("/help", agent)
        out = capsys.readouterr().out

        assert "/reset" in out
        assert "/undo" in out
        assert "/workspace" in out

    def test_reset_clears_history(self, agent: Agent) -> None:
        agent.history = [{"role": "user", "content": "x"}]
        session = Session.new()
        session.messages = [{"role": "user", "content": "x"}]

        slash("/reset", agent, session)

        assert agent.history == []
        assert session.messages == []

    def test_tools_lists_tools(self, agent: Agent, capsys: pytest.CaptureFixture) -> None:
        slash("/tools", agent)
        assert "web_search" in capsys.readouterr().out

    def test_cost_reports_session_usage(self, agent: Agent, capsys: pytest.CaptureFixture) -> None:
        session = Session.new(model="gpt-4o-mini")
        session.usage = {"prompt_tokens": 1000, "completion_tokens": 500}

        slash("/cost", agent, session)
        out = capsys.readouterr().out

        assert "1,500 tokens" in out

    def test_save_writes_the_file(self, agent: Agent, capsys: pytest.CaptureFixture) -> None:
        session = Session.new()
        slash("/save", agent, session)

        assert session.path.is_file()
        assert "saved to" in capsys.readouterr().out

    def test_model_switches(self, agent: Agent) -> None:
        session = Session.new()
        result = slash("/model gpt-4o", agent, session)

        assert result.settings is not None
        assert result.settings.model == "gpt-4o"
        assert session.model == "gpt-4o"
        assert agent.settings.model == "gpt-4o"

    def test_model_without_argument_reports(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        result = slash("/model", agent)

        assert result.settings is None
        assert "gpt-4o-mini" in capsys.readouterr().out

    def test_workspace_switches(self, agent: Agent, tmp_path: Path) -> None:
        target = tmp_path / "other"
        target.mkdir()

        result = slash(f"/workspace {target}", agent)

        assert result.settings is not None
        assert result.settings.workspace == target.resolve()
        assert agent.approver.workspace == target.resolve()

    def test_workspace_rejects_a_missing_directory(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        result = slash("/workspace /definitely/not/here", agent)

        assert result.settings is None
        assert "not a directory" in capsys.readouterr().err

    def test_undo_drops_the_last_exchange(self, agent: Agent) -> None:
        agent.history = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "b"},
        ]
        slash("/undo", agent)

        assert [m["content"] for m in agent.history] == ["one", "a"]

    def test_undo_on_empty_history(self, agent: Agent, capsys: pytest.CaptureFixture) -> None:
        slash("/undo", agent)
        assert "nothing to undo" in capsys.readouterr().out

    def test_unknown_command(self, agent: Agent, capsys: pytest.CaptureFixture) -> None:
        slash("/teleport", agent)
        assert "unknown command" in capsys.readouterr().out


class TestSplash:
    def test_shows_logo_and_examples(self, capsys: pytest.CaptureFixture) -> None:
        cli.print_splash(cli.build_parser())
        out = capsys.readouterr().out

        assert "█" in out
        assert "jaigent chat" in out

    def test_narrow_terminal_drops_the_notes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.console, "width", 46)
        with cli.console.capture() as capture:
            cli.print_splash(cli.build_parser())
        out = capture.get()

        assert "jaigent chat" in out
        assert "interactive session" not in out


class TestInit:
    def _answers(self, monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
        queue = list(answers)
        monkeypatch.setattr(cli.console, "input", lambda *a, **k: queue.pop(0) if queue else "")

    def test_writes_env_and_tests_the_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.chdir(tmp_path)
        self._answers(monkeypatch, ["1", "sk-secret", ""])
        monkeypatch.setattr(
            "jaigent.agent.get_provider",
            lambda settings: FakeProvider([AssistantMessage(content="ready")]),
        )

        code = cli.cmd_init(argparse.Namespace(force=True, no_color=True))
        env = (tmp_path / ".env").read_text(encoding="utf-8")

        assert code == 0
        assert "OPENAI_API_KEY=sk-secret" in env
        assert "JAIGENT_PROVIDER=openai" in env
        assert "responded" in capsys.readouterr().out

    def test_aborts_without_a_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.chdir(tmp_path)
        self._answers(monkeypatch, ["1", "  ", ""])

        assert cli.cmd_init(argparse.Namespace(force=True, no_color=True)) == 1
        assert not (tmp_path / ".env").exists()

    def test_declining_overwrite_leaves_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("KEEP=me\n", encoding="utf-8")
        self._answers(monkeypatch, ["n"])

        code = cli.cmd_init(argparse.Namespace(force=False, no_color=True))

        assert code == 0
        assert (tmp_path / ".env").read_text(encoding="utf-8") == "KEEP=me\n"

    def test_reports_a_failing_test_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent.errors import ProviderError

        monkeypatch.chdir(tmp_path)
        self._answers(monkeypatch, ["1", "sk-bad", ""])

        def explode(settings):  # noqa: ANN001, ANN202
            raise ProviderError("401 unauthorized")

        monkeypatch.setattr("jaigent.agent.get_provider", explode)

        code = cli.cmd_init(argparse.Namespace(force=True, no_color=True))

        assert code == 1
        # The key is still saved so the user does not have to paste it again.
        assert "sk-bad" in (tmp_path / ".env").read_text(encoding="utf-8")
        assert "401" in capsys.readouterr().err


class TestRunTurn:
    def test_streaming_writes_chunks(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path, stream=True)
        agent = Agent(settings, provider=FakeProvider([AssistantMessage(content="streamed")]))

        cli.run_turn(agent, settings, "hi", plain=False)

        assert "streamed" in capsys.readouterr().out

    def test_non_streaming_renders_markdown(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path, stream=False)
        agent = Agent(settings, provider=FakeProvider([AssistantMessage(content="# Title")]))

        cli.run_turn(agent, settings, "hi", plain=False)

        assert "Title" in capsys.readouterr().out

    def test_footer_reports_tool_calls(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from jaigent.llm.base import ToolCall

        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path, stream=False)
        agent = Agent(
            settings,
            provider=FakeProvider(
                [
                    AssistantMessage(tool_calls=[ToolCall("c", "list_files", {})]),
                    AssistantMessage(content="done", usage={"total_tokens": 10}),
                ]
            ),
        )

        cli.run_turn(agent, settings, "hi", plain=False)

        assert "1 tool call" in capsys.readouterr().out


class TestCheckpointSlashCommands:
    """The /revert, /checkpoints, /rewind and /diff family."""

    @pytest.fixture
    def agent_with_change(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Agent:
        """An agent whose store holds one checkpoint for a modified file."""
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
        (tmp_path / "notes.md").write_text("original")
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
        agent = Agent(
            settings,
            provider=FakeProvider([AssistantMessage(content="ok")]),
            checkpoints=True,
        )
        agent.checkpoints.capture(
            [Path("notes.md")], label="write_file notes.md", tool="write_file"
        )
        (tmp_path / "notes.md").write_text("clobbered")
        return agent

    def test_revert_restores_the_file(self, agent_with_change: Agent) -> None:
        slash("/revert", agent_with_change)

        assert (agent_with_change.settings.workspace / "notes.md").read_text() == "original"

    def test_revert_reports_when_there_is_nothing_to_do(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/revert", agent)

        assert "nothing to revert" in capsys.readouterr().out.lower()

    def test_checkpoints_lists_the_history(
        self, agent_with_change: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/checkpoints", agent_with_change)

        assert "notes.md" in capsys.readouterr().out

    def test_checkpoints_reports_an_empty_history(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/checkpoints", agent)

        assert "no checkpoints" in capsys.readouterr().out.lower()

    def test_diff_shows_the_pending_revert(
        self, agent_with_change: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/diff", agent_with_change)

        out = capsys.readouterr().out
        assert "revert" in out
        assert "notes.md" in out

    def test_diff_is_quiet_when_nothing_changed(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/diff", agent)

        assert "nothing to compare" in capsys.readouterr().out.lower()

    def test_rewind_restores_a_checkpoint_by_id(self, agent_with_change: Agent) -> None:
        checkpoint = agent_with_change.checkpoints.latest()

        slash(f"/rewind {checkpoint.id}", agent_with_change)

        assert (agent_with_change.settings.workspace / "notes.md").read_text() == "original"

    def test_rewind_accepts_an_id_prefix(self, agent_with_change: Agent) -> None:
        checkpoint = agent_with_change.checkpoints.latest()

        slash(f"/rewind {checkpoint.id[:4]}", agent_with_change)

        assert (agent_with_change.settings.workspace / "notes.md").read_text() == "original"

    def test_rewind_without_an_id_explains_itself(
        self, agent_with_change: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/rewind", agent_with_change)

        assert "usage" in capsys.readouterr().out.lower()

    def test_rewind_reports_an_unknown_id(
        self, agent_with_change: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/rewind zzzzzz", agent_with_change)

        captured = capsys.readouterr()
        assert "no checkpoint" in (captured.out + captured.err).lower()

    def test_they_degrade_gracefully_when_checkpoints_are_off(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
        off = Agent(
            settings,
            provider=FakeProvider([AssistantMessage(content="ok")]),
            checkpoints=False,
        )

        slash("/revert", off)

        assert "disabled" in capsys.readouterr().out.lower()


class TestSessionSlashCommands:
    """/status, /approve and /commands."""

    def test_status_reports_the_essentials(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/status", agent)

        out = capsys.readouterr().out
        assert "gpt-4o-mini" in out
        assert "openai" in out

    def test_approve_changes_the_mode(self, agent: Agent) -> None:
        result = slash("/approve ask", agent)

        assert result.settings is not None
        assert result.settings.approval == "ask"

    def test_approve_rejects_an_unknown_mode(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        result = slash("/approve banana", agent)

        assert result.settings is None
        assert "auto" in capsys.readouterr().out

    def test_approve_without_an_argument_shows_the_current_mode(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/approve", agent)

        assert "approval is" in capsys.readouterr().out.lower()

    def test_commands_reports_when_there_are_none(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/commands", agent)

        assert "no custom commands" in capsys.readouterr().out.lower()

    def test_help_mentions_the_new_commands(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/help", agent)

        out = capsys.readouterr().out
        for command in ("/revert", "/checkpoints", "/rewind", "/status", "/approve"):
            assert command in out
