"""The commands, keys, serve and route subcommands, plus animated output."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeProvider
from jaigent import cli, gateway
from jaigent.llm.base import AssistantMessage, ToolCall


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("JAIGENT_HOME", str(home))
    monkeypatch.setenv("JAIGENT_KEYS_FILE", str(home / "keys.json"))
    monkeypatch.chdir(project)
    return project


class TestRouteCommand:
    def test_simple_prompt(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["route", "hi", "there"]) == 0
        out = capsys.readouterr().out

        assert "simple" in out
        assert "gpt-4.1-nano" in out

    def test_complex_prompt(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["route", "refactor the package and write all the tests"])
        out = capsys.readouterr().out

        assert "complex" in out
        assert "refactoring" in out

    def test_respects_the_provider(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["route", "hello", "--provider", "anthropic"])
        assert "claude" in capsys.readouterr().out


class TestCommandsCommand:
    def test_empty_list(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["commands", "list"]) == 0
        assert "No custom commands" in capsys.readouterr().out

    def test_new_then_list(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["commands", "new", "review", "-d", "Review the diff"]) == 0
        capsys.readouterr()

        cli.main(["commands", "list"])
        out = capsys.readouterr().out
        assert "/review" in out
        assert "Review the diff" in out

    def test_new_writes_to_the_project(self, isolated_home: Path) -> None:
        cli.main(["commands", "new", "demo", "-d", "d", "--template", "body"])
        assert (isolated_home / ".jaigent" / "commands" / "demo.md").is_file()

    def test_user_scope(self, tmp_path: Path) -> None:
        cli.main(["commands", "new", "mine", "-d", "d", "--user"])
        assert (tmp_path / "home" / "commands" / "mine.md").is_file()

    def test_show(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["commands", "new", "demo", "-d", "d", "--template", "THE TEMPLATE"])
        capsys.readouterr()

        assert cli.main(["commands", "show", "demo"]) == 0
        assert "THE TEMPLATE" in capsys.readouterr().out

    def test_show_missing(self) -> None:
        assert cli.main(["commands", "show", "ghost"]) == 1

    def test_remove(self, isolated_home: Path) -> None:
        cli.main(["commands", "new", "demo", "-d", "d", "--template", "b"])
        assert cli.main(["commands", "remove", "demo"]) == 0
        assert not (isolated_home / ".jaigent" / "commands" / "demo.md").exists()

    def test_reserved_name_is_refused(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["commands", "new", "help", "-d", "d"]) == 1
        assert "built-in" in capsys.readouterr().err


class TestCustomCommandDispatch:
    def test_slash_command_expands_and_runs(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        captured: dict[str, str] = {}

        class Recorder(FakeProvider):
            def complete(self, messages, tools=None, **kwargs):  # noqa: ANN001, ANN003
                user = [m for m in messages if m.get("role") == "user"]
                captured["prompt"] = user[-1]["content"] if user else ""
                return AssistantMessage(content="reviewed")

        monkeypatch.setattr("jaigent.agent.get_provider", lambda s: Recorder([]))
        cli.main(["commands", "new", "review", "-d", "d", "--template", "Review $ARGUMENTS now."])
        capsys.readouterr()

        assert cli.main(["/review", "the auth module", "--no-color"]) == 0
        assert captured["prompt"] == "Review the auth module now."

    def test_unknown_slash_command_is_reported(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["/nope", "--no-color"]) == 1
        assert "Unknown command" in capsys.readouterr().err

    def test_expand_command_passes_plain_text_through(self) -> None:
        from jaigent.config import Settings

        settings = Settings(api_key="k", workspace=".")
        assert cli.expand_command("just a prompt", settings) == "just a prompt"


class TestKeysCommand:
    def test_empty_list(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["keys", "list"]) == 0
        assert "No API keys yet" in capsys.readouterr().out

    def test_new_prints_the_secret_once(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["keys", "new", "my-app"]) == 0
        out = capsys.readouterr().out

        assert "jgt-" in out
        assert "copy it now" in out

    def test_list_hides_the_secret(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["keys", "new", "my-app"])
        secret = next(
            word
            for word in capsys.readouterr().out.split()
            if word.startswith("jgt-") and len(word) > 20
        )

        cli.main(["keys", "list"])
        listed = capsys.readouterr().out
        assert "my-app" in listed
        assert secret not in listed

    def test_revoke(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["keys", "new", "doomed"])
        capsys.readouterr()

        assert cli.main(["keys", "revoke", "doomed"]) == 0
        assert gateway.load_keys()[0].revoked is True

    def test_revoke_missing(self) -> None:
        assert cli.main(["keys", "revoke", "ghost"]) == 1


class TestServeCommand:
    def test_refuses_to_start_without_keys(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["serve", "--port", "0"]) == 78
        assert "No API keys" in capsys.readouterr().err

    def test_reports_a_port_clash(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        gateway.create_key("k")

        def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise OSError("address already in use")

        monkeypatch.setattr("jaigent.gateway.build_server", boom)
        assert cli.main(["serve"]) == 1
        assert "Could not bind" in capsys.readouterr().err


class TestAnimatedOutput:
    def test_verbose_prints_tool_lines(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "jaigent.agent.get_provider",
            lambda s: FakeProvider(
                [
                    AssistantMessage(tool_calls=[ToolCall("c", "list_files", {})]),
                    AssistantMessage(content="done"),
                ]
            ),
        )
        cli.main(["run", "look", "-w", str(tmp_path), "--verbose", "--no-color"])
        out = capsys.readouterr().out

        assert "list_files" in out
        assert "done" in out

    def test_no_spinner_escape_codes_when_piped(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "jaigent.agent.get_provider", lambda s: FakeProvider([AssistantMessage(content="hi")])
        )
        cli.main(["run", "x", "-w", str(tmp_path), "--no-color"])

        assert "\x1b[" not in capsys.readouterr().out

    def test_preview_args_is_compact(self) -> None:
        preview = cli._preview_args({"path": "a" * 100, "count": 2})
        assert len(preview) <= 75
        assert "count=2" in preview or "…" in preview


def test_new_commands_are_registered() -> None:
    for command in ("commands", "keys", "serve", "route"):
        assert command in cli.COMMANDS
