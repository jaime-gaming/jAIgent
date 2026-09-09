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

        assert "#" in out or "jaigent" in out.lower()
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

    def test_skips_dotenv_in_a_protected_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.chdir(tmp_path)
        self._answers(monkeypatch, ["1", "sk-secret", ""])
        monkeypatch.setattr(
            "jaigent.agent.get_provider",
            lambda settings: FakeProvider([AssistantMessage(content="ready")]),
        )
        monkeypatch.setattr("jaigent.paths.can_write_project_dotenv", lambda directory=None: False)

        code = cli.cmd_init(argparse.Namespace(force=True, no_color=True))

        assert code == 0
        assert not (tmp_path / ".env").exists()
        assert "not a place to write" in capsys.readouterr().out.lower()

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

    def test_every_tool_call_leaves_a_quiet_trace(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Without --verbose, tool calls are not invisible: each one leaves a
        single quiet line, so a turn reads as a record of what happened."""
        from jaigent.llm.base import ToolCall

        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path, stream=False)
        agent = Agent(
            settings,
            provider=FakeProvider(
                [
                    AssistantMessage(tool_calls=[ToolCall("c", "read_file", {"path": "notes.md"})]),
                    AssistantMessage(content="done"),
                ]
            ),
        )

        cli.run_turn(agent, settings, "hi", plain=False)

        out = capsys.readouterr().out
        assert "Reading files" in out
        assert "notes.md" in out

    def test_a_failed_tool_call_is_marked_in_the_trace(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent.llm.base import ToolCall

        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path, stream=False)
        agent = Agent(
            settings,
            provider=FakeProvider(
                [
                    AssistantMessage(
                        tool_calls=[ToolCall("c", "read_file", {"path": "missing.md"})]
                    ),
                    AssistantMessage(content="done"),
                ]
            ),
        )

        cli.run_turn(agent, settings, "hi", plain=False)

        out = capsys.readouterr().out
        assert "Reading files" in out
        assert "missing.md" in out


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
        for command in ("/revert", "/checkpoints", "/rewind", "/status", "/approve", "/settings"):
            assert command in out


class TestSlashSafety:
    def test_paths_are_not_slash_commands(self) -> None:
        assert cli.looks_like_slash_command("/tmp/notes.md") is False
        assert cli.looks_like_slash_command("/home/user/file") is False

    def test_real_commands_still_qualify(self) -> None:
        assert cli.looks_like_slash_command("/help") is True
        assert cli.looks_like_slash_command("/model gpt-4o") is True
        assert cli.looks_like_slash_command("exit") is True

    def test_settings_prints_the_live_knobs(
        self, agent: Agent, capsys: pytest.CaptureFixture
    ) -> None:
        slash("/settings", agent)
        out = capsys.readouterr().out
        assert "gpt-4o-mini" in out
        assert "provider" in out.lower()

    def test_key_is_stored_not_sent_to_the_model(
        self, agent: Agent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
        result = slash("/key openai sk-testkey-not-a-prompt", agent)
        assert result.prompt is None
        assert result.settings is not None
        assert result.settings.api_key == "sk-testkey-not-a-prompt"

    def test_markdown_links_are_hyperlinks(self) -> None:
        rendered = cli._markdown("See [docs](https://example.com/a).")
        text = cli.console.render_str if False else None
        del text
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
        ).print(rendered)
        out = buf.getvalue()
        assert "docs" in out
        assert "https://example.com/a" in out or "\x1b]8;;https://example.com/a" in out


def test_revert_twice_steps_back_two_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: /revert restored the same checkpoint every time."""
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
    target = tmp_path / "a.md"
    target.write_text("v1")

    settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
    agent = Agent(
        settings,
        provider=FakeProvider([AssistantMessage(content="ok")]),
        checkpoints=True,
    )

    agent.checkpoints.capture([Path("a.md")], label="write a.md", tool="write_file")
    target.write_text("v2")
    agent.checkpoints.capture([Path("a.md")], label="write a.md", tool="write_file")
    target.write_text("v3")

    slash("/revert", agent)
    assert target.read_text() == "v2"

    slash("/revert", agent)
    assert target.read_text() == "v1"


class TestFriendlyErrors:
    @pytest.mark.parametrize(
        ("raw", "headline", "advice"),
        [
            (
                "Provider request failed: HTTP 401 unauthorized",
                "Your openai key was rejected.",
                "jaigent auth set openai",
            ),
            (
                "HTTP 429 Too Many Requests: rate limit exceeded",
                "The provider is rate-limiting requests.",
                "Wait a minute",
            ),
            (
                "You exceeded your current quota, check billing",
                "Your provider account is out of credit.",
                "Top up",
            ),
            (
                "HTTP 404: model 'gpt-9' not found",
                "The model 'gpt-4o-mini' wasn't found.",
                "jaigent models --only openai",
            ),
            (
                "maximum context length exceeded: too many tokens",
                "The conversation grew too long for the model.",
                "/compact",
            ),
            (
                "could not reach api.openai.com: connection refused",
                "The provider couldn't be reached.",
                "Check your connection",
            ),
            (
                "Every provider failed. Tried: openai.",
                "Every provider failed.",
                "jaigent auth list",
            ),
        ],
    )
    def test_failures_are_translated(
        self, tmp_path: Path, raw: str, headline: str, advice: str
    ) -> None:
        from jaigent.errors import ProviderError

        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)

        got_headline, got_advice = cli.friendly_error(ProviderError(raw), settings)

        assert got_headline == headline
        assert advice in got_advice

    def test_configuration_errors_pass_through(self, tmp_path: Path) -> None:
        from jaigent.errors import ConfigurationError

        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)

        headline, advice = cli.friendly_error(ConfigurationError("Set X first."), settings)

        assert headline == "Set X first."
        assert advice == ""

    def test_unknown_errors_keep_their_text(self, tmp_path: Path) -> None:
        from jaigent.errors import ProviderError

        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)

        headline, advice = cli.friendly_error(ProviderError("mysterious frobnicate"), settings)

        assert headline == "mysterious frobnicate"
        assert advice == ""


class TestRetrySummary:
    @pytest.mark.parametrize(
        ("error", "fragment"),
        [
            ("HTTP 429 rate limit exceeded", "rate limit"),
            ("the request timed out", "timed out"),
            ("connection reset by peer", "couldn't be reached"),
            ("HTTP 503 Service Unavailable", "HTTP 503"),
        ],
    )
    def test_reasons_are_plain_language(self, error: str, fragment: str) -> None:
        assert fragment in cli._retry_summary(error)


class TestFailoverNotices:
    def _failing_agent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str):
        from jaigent.errors import ProviderError
        from jaigent.failover import FailoverProvider

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fallback-key")
        settings = Settings(
            api_key="k",
            model="gpt-4o-mini",
            workspace=tmp_path,
            stream=False,
            retries=1,
        )
        agent = Agent(settings)
        assert isinstance(agent.provider, FailoverProvider)

        def explode(*args, **kwargs):  # noqa: ANN002, ANN003
            raise ProviderError(error)

        monkeypatch.setattr(agent.provider.primary, "complete", explode)
        monkeypatch.setattr(agent.provider, "_sleep", lambda seconds: None)
        monkeypatch.setattr(
            agent.provider, "_build", lambda s: FakeProvider([AssistantMessage(content="ok")])
        )
        return agent, settings

    def test_a_rate_limit_is_announced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        agent, settings = self._failing_agent(
            tmp_path, monkeypatch, "HTTP 429 rate limit exceeded, slow down"
        )

        cli.run_turn(agent, settings, "hi", plain=True)

        out = capsys.readouterr().out
        assert "rate limit" in out
        assert "retrying" in out
        assert "Continuing on anthropic" in out

    def test_a_rejected_key_moves_on_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        agent, settings = self._failing_agent(
            tmp_path, monkeypatch, "HTTP 401 unauthorized: bad key"
        )

        cli.run_turn(agent, settings, "hi", plain=True)

        out = capsys.readouterr().out
        assert "trying the next provider" in out
        assert "Continuing on anthropic" in out


class TestLimitPanels:
    def test_a_spend_cap_explains_itself(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent.agent import AgentResult
        from jaigent.pricing import Cost

        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path, budget=0.5)
        result = AgentResult(output="", stopped_early=True, cost=Cost(usd=0.75))

        cli._print_limit_panel(result, settings)

        out = capsys.readouterr().out
        assert "Spend cap reached" in out
        assert "jaigent settings set budget" in out

    def test_an_exhausted_step_budget_explains_itself(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent.agent import AgentResult
        from jaigent.pricing import Cost

        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path, max_steps=3)
        result = AgentResult(output="", stopped_early=True, cost=Cost(usd=0.0))

        cli._print_limit_panel(result, settings)

        out = capsys.readouterr().out
        assert "Out of steps" in out
        assert "/compact" in out

    def test_a_finished_turn_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent.agent import AgentResult

        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)

        cli._print_limit_panel(AgentResult(output="done"), settings)

        assert capsys.readouterr().out == ""


class TestCloseHandlers:
    def test_closing_the_terminal_saves_the_chat(self, agent: Agent) -> None:
        import signal

        agent.history = [{"role": "user", "content": "remember this"}]
        session = Session.new()
        restore = cli._install_close_handlers(session, agent, True)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            with pytest.raises(SystemExit):
                handler(signal.SIGTERM, None)
            assert session.path.is_file()
            assert "remember this" in session.path.read_text(encoding="utf-8")
        finally:
            restore()

    def test_handlers_are_restored(self, agent: Agent) -> None:
        import signal

        before = signal.getsignal(signal.SIGTERM)
        restore = cli._install_close_handlers(Session.new(), agent, True)
        try:
            assert signal.getsignal(signal.SIGTERM) is not before
        finally:
            restore()
        assert signal.getsignal(signal.SIGTERM) is before

    def test_no_save_means_no_handler(self, agent: Agent) -> None:
        import signal

        before = signal.getsignal(signal.SIGTERM)
        restore = cli._install_close_handlers(Session.new(), agent, False)
        try:
            assert signal.getsignal(signal.SIGTERM) is before
        finally:
            restore()

    def test_an_empty_chat_writes_nothing(self, agent: Agent) -> None:
        import signal

        agent.history = []
        session = Session.new()
        restore = cli._install_close_handlers(session, agent, True)
        try:
            with pytest.raises(SystemExit):
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            assert not session.path.is_file()
        finally:
            restore()


class TestSessionOverrides:
    """`chat --resume`: explicit flags beat the session, backends travel whole."""

    def _args(self, **overrides):
        import argparse

        args = argparse.Namespace(
            resume="last", model=None, provider=None, workspace=None, base_url=None
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _settings(self, tmp_path: Path):
        return Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)

    def test_an_explicit_model_wins(self, tmp_path: Path) -> None:
        session = Session.new(provider="openai", model="gpt-4o", workspace=str(tmp_path))

        updates = cli._session_overrides(
            session, self._args(model="gpt-4o-mini"), self._settings(tmp_path)
        )

        assert "model" not in updates

    def test_an_explicit_provider_wins_and_grounds_the_model(self, tmp_path: Path) -> None:
        session = Session.new(provider="anthropic", model="claude-x", workspace=str(tmp_path))

        updates = cli._session_overrides(
            session, self._args(provider="openai"), self._settings(tmp_path)
        )

        assert "provider" not in updates
        # The stored claude id belongs to another backend; adopting it onto
        # openai would 404 on the first turn.
        assert "model" not in updates

    def test_an_explicit_workspace_wins(self, tmp_path: Path) -> None:
        other = tmp_path / "elsewhere"
        other.mkdir()
        session = Session.new(provider="openai", model="m", workspace=str(tmp_path))

        updates = cli._session_overrides(
            session, self._args(workspace=str(other)), self._settings(tmp_path)
        )

        assert "workspace" not in updates

    def test_a_custom_base_url_survives_resuming(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JAIGENT_BASE_URL", "https://proxy.local/v1")
        settings = Settings.from_env()
        object.__setattr__(settings, "workspace", tmp_path)
        session = Session.new(provider="openai", model="gpt-4o", workspace=str(tmp_path))

        updates = cli._session_overrides(session, self._args(), settings)

        assert "base_url" not in updates

    def test_switching_backend_takes_its_key_and_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jaigent.config import DEFAULT_BASE_URLS

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        session = Session.new(provider="anthropic", model="claude-x", workspace=str(tmp_path))

        updates = cli._session_overrides(session, self._args(), self._settings(tmp_path))

        assert updates["provider"] == "anthropic"
        assert updates["model"] == "claude-x"
        assert updates["api_key"] == "sk-ant-test"
        assert updates["base_url"] == DEFAULT_BASE_URLS["anthropic"]

    def test_switching_to_a_keyless_backend_explains_itself(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        from jaigent.errors import ConfigurationError

        session = Session.new(provider="anthropic", model="claude-x", workspace=str(tmp_path))

        with pytest.raises(ConfigurationError, match="--provider openai") as exc:
            cli._session_overrides(session, self._args(), self._settings(tmp_path))

        assert "anthropic" in str(exc.value)
        assert "jaigent auth set anthropic" in str(exc.value)

    def test_a_vanished_workspace_is_skipped(self, tmp_path: Path) -> None:
        session = Session.new(provider="openai", model="m", workspace=str(tmp_path / "gone"))

        updates = cli._session_overrides(session, self._args(), self._settings(tmp_path))

        assert "workspace" not in updates


class TestFinishChat:
    def _agent(self, tmp_path: Path) -> Agent:
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
        agent = Agent(settings, provider=FakeProvider([AssistantMessage(content="ok")]))
        agent.history = [{"role": "user", "content": "hi"}]
        return agent

    def test_yes_saves_the_conversation(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path)
        session = Session.new()
        cli.console.input = lambda prompt="": "y"  # type: ignore[method-assign]
        try:
            cli._finish_chat(session, agent, True, dirty=True)
        finally:
            del cli.console.input  # restore the bound method

        assert session.path.is_file()

    def test_no_discards_it(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path)
        session = Session.new()
        cli.console.input = lambda prompt="": "n"  # type: ignore[method-assign]
        try:
            cli._finish_chat(session, agent, True, dirty=True)
        finally:
            del cli.console.input

        assert not session.path.is_file()

    def test_a_closed_prompt_discards_it(self, tmp_path: Path) -> None:
        def closed(prompt: str = "") -> str:
            raise EOFError

        agent = self._agent(tmp_path)
        session = Session.new()
        cli.console.input = closed  # type: ignore[method-assign]
        try:
            cli._finish_chat(session, agent, True, dirty=True)
        finally:
            del cli.console.input

        assert not session.path.is_file()

    def test_no_save_never_asks(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        def explode(prompt: str = "") -> str:
            pytest.fail("must not prompt with --no-save")

        agent = self._agent(tmp_path)
        cli.console.input = explode  # type: ignore[method-assign]
        try:
            cli._finish_chat(Session.new(), agent, False, dirty=True)
        finally:
            del cli.console.input

        assert "bye" in capsys.readouterr().out

    def test_a_clean_exit_never_asks(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        def explode(prompt: str = "") -> str:
            pytest.fail("must not prompt when nothing changed")

        agent = self._agent(tmp_path)
        cli.console.input = explode  # type: ignore[method-assign]
        try:
            cli._finish_chat(Session.new(), agent, True, dirty=False)
        finally:
            del cli.console.input

        assert "bye" in capsys.readouterr().out


class TestChatInterrupts:
    def test_ctrl_c_during_a_custom_command_stays_in_chat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Regression: the interrupt escaped to main() and quit without saving."""
        monkeypatch.setenv("JAIGENT_API_KEY", "test-key")
        answers = iter(["/review", "/exit"])

        def scripted(prompt: str = "") -> str:
            return next(answers)

        monkeypatch.setattr(cli.console, "input", scripted)

        def fake_slash(prompt: str, *args):  # noqa: ANN002
            if prompt == "/exit":
                return cli.SlashResult(quit=True)
            return cli.SlashResult(prompt="review the diff")

        monkeypatch.setattr(cli, "_handle_slash", fake_slash)

        def always_interrupted(*args, **kwargs):  # noqa: ANN002, ANN003
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_turn", always_interrupted)
        args = cli.build_parser().parse_args(["chat", "--workspace", str(tmp_path), "--no-save"])

        assert cli.cmd_chat(args) == 0
        assert "interrupted" in capsys.readouterr().out

    def test_ctrl_c_during_a_turn_stays_in_chat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("JAIGENT_API_KEY", "test-key")
        answers = iter(["do the thing", "/exit", "n"])

        def scripted(prompt: str = "") -> str:
            return next(answers)

        monkeypatch.setattr(cli.console, "input", scripted)
        real_slash = cli._handle_slash

        def quit_on_exit(prompt: str, *args):  # noqa: ANN002
            if prompt == "/exit":
                return cli.SlashResult(quit=True)
            return real_slash(prompt, *args)

        monkeypatch.setattr(cli, "_handle_slash", quit_on_exit)
        monkeypatch.setattr(
            cli, "run_turn", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        args = cli.build_parser().parse_args(["chat", "--workspace", str(tmp_path)])

        assert cli.cmd_chat(args) == 0
        assert "interrupted" in capsys.readouterr().out


class TestResumeRebuildsTheBackend:
    """Regression: /resume renamed the settings but the old provider answered."""

    def _saved(
        self, tmp_path: Path, provider: str, model: str, workspace: Path | None = None
    ) -> Session:
        session = Session.new(provider=provider, model=model, workspace=str(workspace or tmp_path))
        session.messages = [{"role": "user", "content": "hello"}]
        session.save()
        return session

    def test_provider_switch_rebuilds_an_owned_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jaigent.failover import FailoverProvider

        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
        agent = Agent(settings)  # owns its provider, like production chat
        assert isinstance(agent.provider, FailoverProvider)
        saved = self._saved(tmp_path, "anthropic", "claude-x")

        result = slash(f"/resume {saved.id}", agent)

        assert result.settings is not None
        assert result.settings.provider == "anthropic"
        assert result.settings.model == "claude-x"
        assert agent.provider.primary.name == "anthropic"  # type: ignore[attr-defined]
        assert agent.history == [{"role": "user", "content": "hello"}]

    def test_a_keyless_backend_keeps_the_current_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
        agent = Agent(settings)
        saved = self._saved(tmp_path, "anthropic", "claude-x")

        result = slash(f"/resume {saved.id}", agent)

        assert result.settings is not None
        assert result.settings.provider == "openai"
        assert result.settings.model == "gpt-4o-mini"
        assert "staying on" in capsys.readouterr().out
        # The conversation still resumes; only the backend is kept.
        assert agent.history == [{"role": "user", "content": "hello"}]

    def test_an_unknown_backend_keeps_the_current_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
        agent = Agent(settings)
        saved = self._saved(tmp_path, "hal9000", "hal-1")

        result = slash(f"/resume {saved.id}", agent)

        assert result.settings is not None
        assert result.settings.provider == "openai"
        assert "unknown provider" in capsys.readouterr().out

    def test_same_backend_only_moves_the_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
        agent = Agent(settings)
        provider_before = agent.provider
        saved = self._saved(tmp_path, "openai", "gpt-4o")

        result = slash(f"/resume {saved.id}", agent)

        assert result.settings is not None
        assert result.settings.model == "gpt-4o"
        assert agent.provider is not provider_before
        assert agent.provider.primary.model == "gpt-4o"  # type: ignore[attr-defined]

    def test_resuming_saves_the_conversation_being_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        settings = Settings(api_key="k", model="gpt-4o-mini", workspace=tmp_path)
        agent = Agent(settings)
        agent.history = [{"role": "user", "content": "unsaved work"}]
        current = Session.new()
        saved = self._saved(tmp_path, "openai", "gpt-4o-mini")

        slash(f"/resume {saved.id}", agent, current)

        assert current.path.is_file()
