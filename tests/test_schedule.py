"""Scheduled tasks: interval parsing, due logic and the store."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jaigent import schedule
from jaigent.errors import ConfigurationError
from jaigent.schedule import Task, next_occurrence, parse_interval


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "schedules.json"
    monkeypatch.setenv("JAIGENT_SCHEDULE_FILE", str(path))
    return path


class TestParseInterval:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("30m", 1800),
            ("every 30m", 1800),
            ("2h", 7200),
            ("1d", 86400),
            ("1w", 604800),
            ("hourly", 3600),
            ("daily", 86400),
            ("weekly", 604800),
            ("daily at 09:00", 86400),
        ],
    )
    def test_understood_forms(self, text: str, seconds: int) -> None:
        assert parse_interval(text)[0] == seconds

    def test_case_and_spacing(self) -> None:
        assert parse_interval("  EVERY 2H  ")[0] == 7200

    def test_canonical_text(self) -> None:
        assert parse_interval("every 45m")[1] == "every 45m"
        assert parse_interval("daily at 9:05")[1] == "daily at 09:05"

    @pytest.mark.parametrize("bad", ["", "sometimes", "5x", "every day-ish", "0m"])
    def test_rejected(self, bad: str) -> None:
        with pytest.raises(ConfigurationError):
            parse_interval(bad)

    def test_sub_minute_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="1 minute"):
            parse_interval("30s")

    def test_impossible_time_of_day(self) -> None:
        with pytest.raises(ConfigurationError, match="valid time of day"):
            parse_interval("daily at 99:00")


class TestNextOccurrence:
    def test_relative_interval(self) -> None:
        now = time.time()
        assert next_occurrence("30m", after=now) == pytest.approx(now + 1800, abs=2)

    def test_time_of_day_today_when_still_ahead(self) -> None:
        base = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        result = datetime.fromtimestamp(next_occurrence("daily at 09:00", after=base.timestamp()))

        assert (result.hour, result.minute) == (9, 0)
        assert result.date() == base.date()

    def test_time_of_day_rolls_to_tomorrow(self) -> None:
        base = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        result = datetime.fromtimestamp(next_occurrence("daily at 09:00", after=base.timestamp()))

        assert (result.hour, result.minute) == (9, 0)
        assert result.date() == (base + timedelta(days=1)).date()


class TestTask:
    def test_schedules_itself_on_creation(self) -> None:
        assert Task(id="t", prompt="p", interval="1h").next_run > time.time()

    def test_zero_next_run_means_overdue_not_unset(self) -> None:
        # Regression: 0.0 is a real timestamp meaning "due", not "unscheduled".
        task = Task(id="t", prompt="p", interval="1h", next_run=0.0)

        assert task.next_run == 0.0
        assert task.is_due() is True

    def test_not_due_before_its_time(self) -> None:
        assert Task(id="t", prompt="p", interval="1h").is_due() is False

    def test_paused_tasks_are_never_due(self) -> None:
        task = Task(id="t", prompt="p", interval="1h", next_run=0.0, enabled=False)
        assert task.is_due() is False

    def test_record_success(self) -> None:
        task = Task(id="t", prompt="p", interval="1h", next_run=0.0)
        task.record("ok", "all good")

        assert task.runs == 1
        assert task.failures == 0
        assert task.last_status == "ok"
        assert task.is_due() is False  # rescheduled

    def test_record_failure_counts(self) -> None:
        task = Task(id="t", prompt="p", interval="1h")
        task.record("error", "boom")

        assert task.failures == 1

    def test_long_output_is_trimmed(self) -> None:
        task = Task(id="t", prompt="p", interval="1h")
        task.record("ok", "x" * 5000)

        assert len(task.last_output) <= 2000

    @pytest.mark.parametrize(
        ("offset", "expected"),
        [(-10, "due now"), (600, "in 10m"), (7200, "in 2h"), (172800, "in 2d")],
    )
    def test_due_in(self, offset: float, expected: str) -> None:
        task = Task(id="t", prompt="p", interval="1h", next_run=time.time() + offset)
        assert task.due_in() == expected

    def test_paused_reads_as_paused(self) -> None:
        task = Task(id="t", prompt="p", interval="1h", enabled=False)
        assert task.due_in() == "paused"

    def test_round_trip(self) -> None:
        original = Task(id="t", prompt="p", interval="2h", workspace="/tmp", model="gpt-4o")
        restored = Task.from_dict(original.to_dict())

        assert restored.id == original.id
        assert restored.interval == "2h"
        assert restored.model == "gpt-4o"
        assert restored.next_run == pytest.approx(original.next_run)


class TestStore:
    def test_empty(self) -> None:
        assert schedule.load_all() == []

    def test_add_and_load(self) -> None:
        task = schedule.add("do a thing", "1h", workspace="/tmp")

        loaded = schedule.load_all()
        assert len(loaded) == 1
        assert loaded[0].prompt == "do a thing"
        assert loaded[0].id == task.id

    def test_ids_increment(self) -> None:
        schedule.add("a", "1h")
        schedule.add("b", "1h")

        assert [t.id for t in schedule.load_all()] == ["task-1", "task-2"]

    def test_add_validates_the_interval(self) -> None:
        with pytest.raises(ConfigurationError):
            schedule.add("a", "whenever")
        assert schedule.load_all() == []

    def test_add_requires_a_prompt(self) -> None:
        with pytest.raises(ConfigurationError, match="needs a prompt"):
            schedule.add("   ", "1h")

    def test_get_by_exact_id(self) -> None:
        schedule.add("a", "1h")
        assert schedule.get("task-1") is not None

    def test_get_by_prefix(self) -> None:
        schedule.add("a", "1h")
        assert schedule.get("task") is not None

    def test_get_missing(self) -> None:
        assert schedule.get("nope") is None

    def test_update(self) -> None:
        task = schedule.add("a", "1h")
        task.prompt = "changed"
        schedule.update(task)

        assert schedule.get(task.id).prompt == "changed"  # type: ignore[union-attr]

    def test_remove(self) -> None:
        schedule.add("a", "1h")
        assert schedule.remove("task-1") is True
        assert schedule.load_all() == []

    def test_remove_missing(self) -> None:
        assert schedule.remove("task-9") is False

    def test_pause_and_resume(self) -> None:
        schedule.add("a", "1h")

        assert schedule.set_enabled("task-1", False).enabled is False  # type: ignore[union-attr]
        assert schedule.set_enabled("task-1", True).enabled is True  # type: ignore[union-attr]

    def test_pause_missing(self) -> None:
        assert schedule.set_enabled("nope", False) is None

    def test_due_tasks_filters(self) -> None:
        schedule.add("later", "1h")
        due = schedule.add("now", "1h")
        due.next_run = 0.0
        schedule.update(due)

        assert [t.prompt for t in schedule.due_tasks()] == ["now"]

    def test_corrupt_file_yields_nothing(self, isolated_store: Path) -> None:
        isolated_store.parent.mkdir(parents=True, exist_ok=True)
        isolated_store.write_text("{broken", encoding="utf-8")

        assert schedule.load_all() == []

    def test_bad_entries_are_skipped(self, isolated_store: Path) -> None:
        isolated_store.parent.mkdir(parents=True, exist_ok=True)
        isolated_store.write_text(
            json.dumps({"version": 1, "tasks": [{"id": "ok", "prompt": "p", "interval": "1h"}]}),
            encoding="utf-8",
        )
        assert len(schedule.load_all()) == 1

    def test_persisted_file_shape(self, isolated_store: Path) -> None:
        schedule.add("a", "1h")
        data = json.loads(isolated_store.read_text(encoding="utf-8"))

        assert data["version"] == schedule.SCHEDULE_VERSION
        assert isinstance(data["tasks"], list)


def test_schedule_path_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAIGENT_SCHEDULE_FILE", str(tmp_path / "custom.json"))
    assert schedule.schedules_path() == tmp_path / "custom.json"


def test_schedule_path_follows_jaigent_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JAIGENT_SCHEDULE_FILE", raising=False)
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "h"))
    assert schedule.schedules_path() == tmp_path / "h" / "schedules.json"
