"""The models, settings, skills and schedule subcommands."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import FakeProvider
from jaigent import cli, schedule
from jaigent.llm.base import AssistantMessage


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every command away from the real ~/.jaigent."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("JAIGENT_HOME", str(home))
    monkeypatch.setenv("JAIGENT_SCHEDULE_FILE", str(home / "schedules.json"))
    monkeypatch.chdir(project)
    return project


class TestModelsCommand:
    def test_lists_the_catalogue(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["models"]) == 0
        out = capsys.readouterr().out

        assert "gpt-4o-mini" in out
        assert "claude" in out.lower()

    def test_filters_by_provider(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["models", "--only", "ollama"])
        out = capsys.readouterr().out

        assert "qwen2.5:14b" in out
        assert "gpt-4o-mini " not in out

    def test_search_term(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["models", "deepseek"])
        out = capsys.readouterr().out

        assert "deepseek" in out.lower()

    def test_no_match_is_an_error(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["models", "zzz-nothing"]) == 1

    def test_shows_prices(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["models", "--only", "openai"])
        assert "Mtok" in capsys.readouterr().out

    def test_openrouter_is_listed_as_a_provider(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["models"])
        assert "openrouter" in capsys.readouterr().out


class TestSettingsCommand:
    def test_empty_list(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["settings", "list"]) == 0
        assert "No stored settings" in capsys.readouterr().out

    def test_bare_settings_lists(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["settings"]) == 0

    def test_set_then_list(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["settings", "set", "model", "gpt-4o"]) == 0
        capsys.readouterr()

        cli.main(["settings", "list"])
        out = capsys.readouterr().out
        assert "model" in out
        assert "gpt-4o" in out

    def test_project_scope(self, isolated_home: Path, capsys: pytest.CaptureFixture) -> None:
        cli.main(["settings", "set", "model", "proj", "--project"])
        assert (isolated_home / ".jaigent" / "settings.json").is_file()

    def test_unset(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["settings", "set", "model", "x"])
        assert cli.main(["settings", "unset", "model"]) == 0

    def test_unset_missing_returns_one(self) -> None:
        assert cli.main(["settings", "unset", "model"]) == 1

    def test_rejects_a_secret(self, capsys: pytest.CaptureFixture) -> None:
        # Exit 78 is EX_CONFIG, raised as ConfigurationError by the store.
        assert cli.main(["settings", "set", "api_key", "sk-leak"]) == 78
        assert "never be stored" in capsys.readouterr().err

    def test_rejects_unknown_key(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["settings", "set", "modle", "x"]) == 78

    def test_path_prints_both_locations(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["settings", "path"]) == 0
        out = capsys.readouterr().out

        assert "user:" in out
        assert "project:" in out

    def test_stored_setting_changes_resolution(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["settings", "set", "max_steps", "42"])
        args = cli.build_parser().parse_args(["run", "x"])

        assert cli.resolve_settings(args).max_steps == 42


class TestSkillsCommand:
    def test_empty_list(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["skills", "list"]) == 0
        out = capsys.readouterr().out
        assert "spend-cap" in out
        assert "compact" in out

    def test_new_then_list(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["skills", "new", "changelog", "-d", "Write a changelog"]) == 0
        capsys.readouterr()

        cli.main(["skills", "list"])
        out = capsys.readouterr().out
        assert "changelog" in out
        assert "Write a changelog" in out

    def test_new_writes_to_the_project_by_default(self, isolated_home: Path) -> None:
        cli.main(["skills", "new", "demo", "-d", "d", "-b", "body"])
        assert (isolated_home / ".jaigent" / "skills" / "demo.md").is_file()

    def test_new_user_scope(self, tmp_path: Path) -> None:
        cli.main(["skills", "new", "mine", "-d", "d", "--user"])
        assert (tmp_path / "home" / "skills" / "mine.md").is_file()

    def test_show(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["skills", "new", "demo", "-d", "d", "-b", "THE BODY TEXT"])
        capsys.readouterr()

        assert cli.main(["skills", "show", "demo"]) == 0
        assert "THE BODY" in capsys.readouterr().out

    def test_show_missing(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["skills", "show", "ghost"]) == 1

    def test_remove(self, isolated_home: Path) -> None:
        cli.main(["skills", "new", "demo", "-d", "d", "-b", "b"])
        assert cli.main(["skills", "remove", "demo"]) == 0
        assert not (isolated_home / ".jaigent" / "skills" / "demo.md").exists()

    def test_remove_missing(self) -> None:
        assert cli.main(["skills", "remove", "ghost"]) == 1

    def test_invalid_name(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["skills", "new", "!!!", "-d", "d"]) == 1

    def test_new_skill_reaches_the_toolset(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["skills", "new", "demo", "-d", "A demo skill", "-b", "b"])
        capsys.readouterr()

        cli.main(["tools"])
        assert "load_skill" in capsys.readouterr().out


class TestScheduleCommand:
    def test_empty_list(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["schedule", "list"]) == 0
        assert "No scheduled tasks" in capsys.readouterr().out

    def test_add(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["schedule", "add", "check the news", "--every", "2h"]) == 0
        assert len(schedule.load_all()) == 1

    def test_add_rejects_a_bad_interval(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["schedule", "add", "x", "--every", "whenever"]) == 1
        assert schedule.load_all() == []

    def test_add_records_the_workspace(self, tmp_path: Path) -> None:
        target = tmp_path / "ws"
        target.mkdir()
        cli.main(["schedule", "add", "x", "--every", "1h", "-w", str(target)])

        assert schedule.load_all()[0].workspace == str(target.resolve())

    def test_list_shows_tasks(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["schedule", "add", "nightly build", "--every", "daily"])
        capsys.readouterr()

        cli.main(["schedule", "list"])
        out = capsys.readouterr().out
        assert "task-1" in out
        assert "nightly build" in out

    def test_pause_and_resume(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["schedule", "add", "x", "--every", "1h"])

        assert cli.main(["schedule", "pause", "task-1"]) == 0
        assert schedule.get("task-1").enabled is False  # type: ignore[union-attr]

        assert cli.main(["schedule", "resume", "task-1"]) == 0
        assert schedule.get("task-1").enabled is True  # type: ignore[union-attr]

    def test_remove(self) -> None:
        cli.main(["schedule", "add", "x", "--every", "1h"])
        assert cli.main(["schedule", "remove", "task-1"]) == 0
        assert schedule.load_all() == []

    def test_show(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["schedule", "add", "inspect this", "--every", "1h"])
        capsys.readouterr()

        assert cli.main(["schedule", "show", "task-1"]) == 0
        assert "inspect this" in capsys.readouterr().out

    def test_missing_task_is_an_error(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["schedule", "show", "task-9"]) == 1

    def test_run_with_nothing_due(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["schedule", "add", "later", "--every", "1h"])
        capsys.readouterr()

        assert cli.main(["schedule", "run"]) == 0
        assert "Nothing due" in capsys.readouterr().out

    def test_run_executes_a_due_task(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "jaigent.agent.get_provider",
            lambda settings: FakeProvider([AssistantMessage(content="task finished")]),
        )
        cli.main(["schedule", "add", "do it", "--every", "1h", "-w", str(tmp_path)])
        task = schedule.get("task-1")
        assert task is not None
        task.next_run = 0.0
        schedule.update(task)
        capsys.readouterr()

        assert cli.main(["schedule", "run"]) == 0
        out = capsys.readouterr().out

        assert "task finished" in out
        after = schedule.get("task-1")
        assert after is not None
        assert after.runs == 1
        assert after.last_status == "ok"
        assert after.next_run > time.time()  # rescheduled

    def test_run_one_task_by_id_even_if_not_due(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "jaigent.agent.get_provider",
            lambda settings: FakeProvider([AssistantMessage(content="forced")]),
        )
        cli.main(["schedule", "add", "x", "--every", "1h", "-w", str(tmp_path)])
        capsys.readouterr()

        assert cli.main(["schedule", "run", "--id", "task-1"]) == 0
        assert schedule.get("task-1").runs == 1  # type: ignore[union-attr]

    def test_a_failing_task_is_recorded_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        from jaigent.errors import ProviderError

        def explode(settings):  # noqa: ANN001, ANN202
            raise ProviderError("provider is down")

        monkeypatch.setattr("jaigent.agent.get_provider", explode)
        cli.main(["schedule", "add", "x", "--every", "1h", "-w", str(tmp_path)])
        capsys.readouterr()

        assert cli.main(["schedule", "run", "--id", "task-1"]) == 1
        task = schedule.get("task-1")
        assert task is not None
        assert task.last_status == "error"
        assert task.failures == 1

    def test_scheduled_runs_never_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Nobody is watching, so approval must be forced to auto."""
        seen: dict[str, str] = {}

        def capture(settings):  # noqa: ANN001, ANN202
            seen["approval"] = settings.approval
            return FakeProvider([AssistantMessage(content="ok")])

        monkeypatch.setattr("jaigent.agent.get_provider", capture)
        monkeypatch.setenv("JAIGENT_APPROVAL", "ask")
        cli.main(["schedule", "add", "x", "--every", "1h", "-w", str(tmp_path)])
        cli.main(["schedule", "run", "--id", "task-1"])

        assert seen["approval"] == "auto"

    def test_run_missing_id(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["schedule", "run", "--id", "task-9"]) == 1


def test_new_commands_are_recognised() -> None:
    for command in ("models", "settings", "skills", "schedule"):
        assert command in cli.COMMANDS


def test_bare_prompt_shorthand_still_works() -> None:
    # A prompt starting with a command-like word must not be swallowed.
    assert cli.normalise_argv(["summarise", "the", "readme"])[0] == "run"
    assert cli.normalise_argv(["models"])[0] == "models"
