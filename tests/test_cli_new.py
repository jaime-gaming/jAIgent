"""The commands, keys, serve and route subcommands, plus animated output."""

from __future__ import annotations

import json
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

    @pytest.mark.parametrize("flag", ["--body", "-b"])
    def test_the_template_also_answers_to_body(
        self, capsys: pytest.CaptureFixture, flag: str
    ) -> None:
        """`skills new` calls the same thing --body; that word works here too."""
        cli.main(["commands", "new", "demo", "-d", "d", flag, "THE TEMPLATE"])
        capsys.readouterr()

        assert cli.main(["commands", "show", "demo"]) == 0
        assert "THE TEMPLATE" in capsys.readouterr().out

    def test_its_help_lists_its_own_flags_not_every_flag(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """A command file has no use for a provider, a model or a temperature.

        The shared flags already live on `jaigent commands` itself; repeating
        them on every action buried the four that matter under twenty that do
        nothing here, which is what `skills` has always avoided.
        """
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["commands", "new", "--help"])

        text = capsys.readouterr().out
        assert "--template" in text
        assert "--user" in text
        assert "--provider" not in text
        assert "--temperature" not in text

    def test_show_missing(self) -> None:
        assert cli.main(["commands", "show", "ghost"]) == 1

    def test_a_new_command_says_what_it_stops_from_loading(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """As with skills: the command that just stopped loading gets named."""
        cli.main(["commands", "new", "review", "-d", "yours", "--template", "t", "--user"])
        capsys.readouterr()

        assert (
            cli.main(["commands", "new", "review", "-d", "the project's", "--template", "t"]) == 0
        )
        assert "shadows the user command" in capsys.readouterr().out

    def test_a_new_command_lands_where_the_agent_works(self, tmp_path: Path) -> None:
        """`--workspace` moves the whole command, not just the file tools."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        assert (
            cli.main(
                [
                    "commands",
                    "--workspace",
                    str(elsewhere),
                    "new",
                    "review",
                    "-d",
                    "Review it",
                    "--template",
                    "Look at {workspace}.",
                ]
            )
            == 0
        )
        assert (elsewhere / ".jaigent" / "commands" / "review.md").is_file()
        assert not (tmp_path / "project" / ".jaigent" / "commands" / "review.md").exists()

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


class TestARunThatAnswersWithNothing:
    """A turn that comes back empty must not look like a turn that worked.

    It used to print nothing at all and exit 0, which on a terminal reads as a
    program that hung and then gave up, and in a script reads as success with an
    empty answer. The commonest real cause is worth naming in the message: an
    endpoint that speaks another provider's dialect hands back a body with no
    text in it, which is what `--base-url` pointing at the wrong API looks like.
    """

    def _empty(self, monkeypatch: pytest.MonkeyPatch, *, tool_calls: bool = False) -> None:
        script: list[AssistantMessage] = []
        if tool_calls:
            script.append(AssistantMessage(tool_calls=[ToolCall("c", "list_files", {})]))
        script.append(AssistantMessage(content=""))
        monkeypatch.setattr("jaigent.agent.get_provider", lambda s: FakeProvider(script))

    def test_it_says_so_and_fails(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        self._empty(monkeypatch)

        assert cli.main(["run", "x", "-w", str(tmp_path), "--no-color"]) == 1

        err = capsys.readouterr().err
        assert "answered with nothing to print" in err
        assert "--base-url" in err

    def test_it_counts_the_work_it_did_manage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        """Tool calls that ran are real, and the notice should not hide them."""
        self._empty(monkeypatch, tool_calls=True)

        assert cli.main(["run", "x", "-w", str(tmp_path), "--no-color"]) == 1

        # Flattened: a console wraps where it likes, including mid-sentence.
        flat = " ".join(capsys.readouterr().err.split())
        assert "It made 1 tool call and then returned no text" in flat

    def test_an_answer_that_is_there_still_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "jaigent.agent.get_provider", lambda s: FakeProvider([AssistantMessage(content="hi")])
        )

        assert cli.main(["run", "x", "-w", str(tmp_path), "--no-color"]) == 0
        assert "hi" in capsys.readouterr().out

    def test_the_object_reports_it_as_the_failure_it_is(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        """The same contract as a turn that raised, with what it cost kept."""
        self._empty(monkeypatch)

        assert cli.main(["run", "x", "-w", str(tmp_path), "--json", "--no-color"]) == 1

        body = json.loads(capsys.readouterr().out)
        assert body["ok"] is False
        assert body["kind"] == "EmptyAnswer"
        assert body["answer"] == ""

    def test_no_empty_file_is_written(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        """`-o` replaces what was there, so an empty answer must not reach it."""
        self._empty(monkeypatch)
        target = tmp_path / "answer.md"
        target.write_text("the previous answer\n", encoding="utf-8")

        assert cli.main(["run", "x", "-w", str(tmp_path), "-o", str(target), "--no-color"]) == 1

        assert target.read_text(encoding="utf-8") == "the previous answer\n"
        assert not target.with_suffix(".md.json").exists()
        assert "was left alone" in " ".join(capsys.readouterr().err.split())


class TestSavingAOneShotRun:
    """`jaigent usage` counts saved conversations, so a run has to be savable.

    A one-shot run spends money and, unless it is kept, leaves nothing behind:
    the transcript is gone and the cost never reaches the report. Chat saves by
    default and offers `--no-save`; a run is the other way round, because it is
    often one line of a script that would leave a file behind every time.
    """

    @pytest.fixture(autouse=True)
    def _provider_and_sessions(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            "jaigent.agent.get_provider", lambda s: FakeProvider([AssistantMessage(content="hi")])
        )
        monkeypatch.setenv("JAIGENT_SESSION_DIR", str(tmp_path / "sessions"))

    def _saved(self, tmp_path: Path) -> list[Path]:
        return sorted((tmp_path / "sessions").glob("*.json"))

    def test_without_the_flag_it_saves_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert cli.main(["run", "x", "-w", str(tmp_path), "--no-color"]) == 0

        assert "saved as" not in capsys.readouterr().out
        assert self._saved(tmp_path) == []

    def test_with_the_flag_the_transcript_is_there(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert (
            cli.main(["run", "summarise notes", "-w", str(tmp_path), "--save", "--no-color"]) == 0
        )

        out = capsys.readouterr().out
        assert "saved as" in out
        saved = self._saved(tmp_path)
        assert len(saved) == 1

        # The point of saving it: `sessions` and `usage` can both see it now.
        session_id = saved[0].stem
        capsys.readouterr()
        assert cli.main(["sessions", "show", session_id]) == 0
        assert "summarise notes" in capsys.readouterr().out

    def test_json_stays_one_object_and_names_the_session(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """stdout is parsed, so the notice goes to stderr with the trace."""
        assert cli.main(["run", "x", "-w", str(tmp_path), "--save", "--json", "--no-color"]) == 0

        captured = capsys.readouterr()
        body = json.loads(captured.out)
        assert body["ok"] is True
        assert body["saved_session"] == self._saved(tmp_path)[0].stem
        assert "saved as" in captured.err

    def test_the_output_file_sidecar_names_it_too(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        target = tmp_path / "answer.md"

        assert (
            cli.main(["run", "x", "-w", str(tmp_path), "--save", "-o", str(target), "--no-color"])
            == 0
        )

        sidecar = json.loads((tmp_path / "answer.md.json").read_text(encoding="utf-8"))
        assert sidecar["saved_session"] == self._saved(tmp_path)[0].stem


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
        captured = capsys.readouterr()

        assert "list_files" in captured.err
        assert "done" in captured.out

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
