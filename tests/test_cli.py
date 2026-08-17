"""CLI argument parsing and command dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeProvider
from jaigent import cli
from jaigent.llm.base import AssistantMessage, ToolCall


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Replace the provider factory so no network or key is needed."""
    script = [AssistantMessage(content="the answer")]

    def factory(settings):  # noqa: ANN001, ANN202
        return FakeProvider(list(script))

    monkeypatch.setattr("jaigent.agent.get_provider", factory)
    return script


@pytest.mark.usefixtures("clean_env")
class TestConfigCommand:
    def test_reports_missing_key_with_nonzero_exit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code = cli.main(["config"])
        out = capsys.readouterr().out

        assert code == 1
        assert "No API key configured" in out

    def test_shows_masked_key(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-supersecret")
        code = cli.main(["config"])
        out = capsys.readouterr().out

        assert code == 0
        assert "supersecret" not in out


@pytest.mark.usefixtures("clean_env")
class TestToolsCommand:
    def test_lists_default_tools(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["tools"]) == 0
        out = capsys.readouterr().out

        assert "web_search" in out
        assert "read_file" in out
        # The shell tool is not offered; only the hint about enabling it appears.
        assert "run_command is hidden" in out
        assert "Run a shell command" not in out

    def test_shell_tool_shown_when_allowed(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["tools", "--allow-shell"])
        out = capsys.readouterr().out

        assert "run_command" in out
        assert "Run a shell command" in out


@pytest.mark.usefixtures("clean_env", "fake_agent")
class TestRunCommand:
    def test_explicit_run(self, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        code = cli.main(["run", "what", "is", "up", "--workspace", str(tmp_path), "--no-color"])
        assert code == 0
        assert "the answer" in capsys.readouterr().out

    def test_bare_prompt_shorthand(self, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        code = cli.main(["hello there", "--workspace", str(tmp_path), "--no-color"])
        assert code == 0
        assert "the answer" in capsys.readouterr().out

    def test_no_args_prints_splash_with_logo(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main([]) == 0
        out = capsys.readouterr().out

        assert "█" in out  # the wordmark
        assert "searches the web" in out  # the tagline
        assert "jaigent chat" in out  # example commands
        assert "OPENAI_API_KEY" in out  # how to bring a key


@pytest.mark.usefixtures("clean_env")
class TestLogo:
    def test_logo_flag_prints_the_wordmark(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["--logo"]) == 0
        out = capsys.readouterr().out

        assert "█" in out
        assert "0.1.0" in out

    def test_logo_respects_no_color(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["--logo", "--no-color"])
        assert "\x1b[" not in capsys.readouterr().out

    def test_logo_flag_skips_the_agent(self, capsys: pytest.CaptureFixture) -> None:
        # No API key is set, yet --logo must still succeed.
        assert cli.main(["--logo"]) == 0


@pytest.mark.usefixtures("clean_env")
class TestSettingsResolution:
    def test_flags_override_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("JAIGENT_MODEL", "from-env")
        args = cli.build_parser().parse_args(
            ["run", "x", "--model", "from-flag", "--workspace", str(tmp_path), "--max-steps", "3"]
        )
        settings = cli.resolve_settings(args)

        assert settings.model == "from-flag"
        assert settings.max_steps == 3
        assert settings.workspace == tmp_path.resolve()

    def test_environment_used_when_no_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_MODEL", "from-env")
        args = cli.build_parser().parse_args(["run", "x"])
        assert cli.resolve_settings(args).model == "from-env"

    def test_allow_shell_flag(self) -> None:
        args = cli.build_parser().parse_args(["run", "x", "--allow-shell"])
        assert cli.resolve_settings(args).allow_shell is True


@pytest.mark.usefixtures("clean_env")
class TestErrorHandling:
    def test_missing_api_key_exits_78(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code = cli.main(["run", "hello"])
        assert code == 78
        assert "configuration error" in capsys.readouterr().err.lower()

    def test_empty_prompt(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        assert cli.main(["run", "   "]) == 2


def test_version_flag(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert "jaigent" in capsys.readouterr().out


@pytest.mark.usefixtures("clean_env")
def test_verbose_run_traces_tools(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    def factory(settings):  # noqa: ANN001, ANN202
        return FakeProvider(
            [
                AssistantMessage(tool_calls=[ToolCall("c1", "list_files", {})]),
                AssistantMessage(content="listed"),
            ]
        )

    monkeypatch.setattr("jaigent.agent.get_provider", factory)
    cli.main(["run", "list files", "--workspace", str(tmp_path), "--verbose", "--no-color"])
    captured = capsys.readouterr()

    assert "list_files" in captured.err
    assert "listed" in captured.out
