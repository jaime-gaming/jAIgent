"""Saving, listing and resuming sessions."""

from __future__ import annotations

import json
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jaigent import session as sessions
from jaigent.session import Session


@pytest.fixture(autouse=True)
def isolated_session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real ~/.jaigent during tests."""
    directory = tmp_path / "sessions"
    monkeypatch.setenv("JAIGENT_SESSION_DIR", str(directory))
    return directory


def make(title: str = "a task", **kw) -> Session:
    session = Session.new(provider="openai", model="gpt-4o-mini", workspace="/tmp", **kw)
    session.title = title
    return session


class TestIds:
    def test_two_sessions_in_the_same_second_do_not_share_an_id(
        self, isolated_session_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frozen = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        monkeypatch.setattr(sessions, "datetime", FrozenDateTime)
        first = Session.new()
        first.save()
        second = Session.new()

        assert first.id != second.id
        assert second.id.startswith(first.id)


class TestRoundTrip:
    def test_save_then_load(self) -> None:
        session = make("summarise the docs")
        session.messages = [{"role": "user", "content": "hi"}]
        session.save()

        loaded = sessions.load(session.id)
        assert loaded is not None
        assert loaded.title == "summarise the docs"
        assert loaded.messages == [{"role": "user", "content": "hi"}]
        assert loaded.model == "gpt-4o-mini"

    def test_file_is_valid_json(self, isolated_session_dir: Path) -> None:
        session = make()
        path = session.save()

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["id"] == session.id

    def test_save_is_idempotent(self) -> None:
        session = make()
        session.save()
        session.messages = [{"role": "user", "content": "later"}]
        session.save()

        loaded = sessions.load(session.id)
        assert loaded is not None
        assert loaded.messages == [{"role": "user", "content": "later"}]

    def test_load_missing_returns_none(self) -> None:
        assert sessions.load("nope") is None

    def test_corrupt_file_returns_none(self, isolated_session_dir: Path) -> None:
        isolated_session_dir.mkdir(parents=True, exist_ok=True)
        (isolated_session_dir / "broken.json").write_text("{oops", encoding="utf-8")
        assert sessions.load("broken") is None


class TestListing:
    def test_empty_directory(self) -> None:
        assert sessions.list_sessions() == []

    def test_newest_first(self) -> None:
        old = make("old")
        old.updated = time.time() - 500
        old.save()
        new = make("new")
        new.id = "99999999-999999"
        new.save()

        listed = sessions.list_sessions()
        assert [s.title for s in listed] == ["new", "old"]

    def test_limit(self) -> None:
        for index in range(5):
            session = make(f"task {index}")
            session.id = f"2026010{index}-000000"
            session.save()
        assert len(sessions.list_sessions(limit=2)) == 2

    def test_corrupt_files_are_skipped(self, isolated_session_dir: Path) -> None:
        make("good").save()
        (isolated_session_dir / "bad.json").write_text("nope", encoding="utf-8")

        listed = sessions.list_sessions()
        assert len(listed) == 1
        assert listed[0].title == "good"


class TestResolve:
    def test_last_returns_newest(self) -> None:
        first = make("first")
        first.id = "20260101-000000"
        first.updated = 1.0
        first.save()
        second = make("second")
        second.id = "20260102-000000"
        second.updated = 2.0
        second.save()

        assert sessions.resolve("last").title == "second"  # type: ignore[union-attr]

    def test_last_prefers_the_later_id_when_timestamps_match(self) -> None:
        """Windows often stamps two saves with the same time.time()."""
        first = make("first")
        first.id = "20260101-000000"
        first.updated = 1.0
        first.save()
        second = make("second")
        second.id = "20260102-000000"
        second.updated = 1.0
        second.save()

        assert sessions.resolve("last").title == "second"  # type: ignore[union-attr]

    @pytest.mark.parametrize("reference", [None, "", "last", "latest"])
    def test_aliases_for_newest(self, reference: str | None) -> None:
        make("only").save()
        assert sessions.resolve(reference) is not None

    def test_exact_id(self) -> None:
        session = make("exact")
        session.save()
        assert sessions.resolve(session.id).title == "exact"  # type: ignore[union-attr]

    def test_prefix_match(self) -> None:
        session = make("prefixed")
        session.id = "20260817-123456"
        session.save()
        assert sessions.resolve("20260817").title == "prefixed"  # type: ignore[union-attr]

    def test_no_match(self) -> None:
        make().save()
        assert sessions.resolve("zzzz") is None

    def test_resolve_on_empty_store(self) -> None:
        assert sessions.resolve("last") is None


class TestMetadata:
    def test_turns_counts_user_messages(self) -> None:
        session = make()
        session.messages = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "2"},
        ]
        assert session.turns == 2

    def test_title_derived_from_first_prompt(self) -> None:
        session = Session.new()
        session.set_title_from("  Summarise   the   README  ")
        assert session.title == "Summarise the README"

    def test_title_is_truncated(self) -> None:
        session = Session.new()
        session.set_title_from("x" * 200)
        assert len(session.title) <= 71
        assert session.title.endswith("…")

    def test_title_is_not_overwritten(self) -> None:
        session = make("original")
        session.set_title_from("something else")
        assert session.title == "original"

    def test_touch_updates_messages_and_usage(self) -> None:
        session = make()
        session.touch([{"role": "user", "content": "x"}], {"total_tokens": 100})
        session.touch([{"role": "user", "content": "y"}], {"total_tokens": 50})

        assert session.usage["total_tokens"] == 150
        assert len(session.messages) == 1

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [(5, "just now"), (120, "2m ago"), (7200, "2h ago"), (172800, "2d ago")],
    )
    def test_age(self, delta: float, expected: str) -> None:
        session = make()
        session.updated = time.time() - delta
        assert session.age() == expected


class TestDelete:
    def test_delete_removes_the_file(self) -> None:
        session = make()
        session.save()
        assert session.delete() is True
        assert sessions.load(session.id) is None

    def test_delete_missing_is_false(self) -> None:
        assert make().delete() is False


def test_session_dir_honours_the_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAIGENT_SESSION_DIR", str(tmp_path / "custom"))
    assert sessions.session_dir() == tmp_path / "custom"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits")
def test_session_file_is_owner_only(isolated_session_dir: Path) -> None:
    path = make().save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_api_key_never_reaches_a_session_file(isolated_session_dir: Path) -> None:
    session = make()
    session.messages = [{"role": "user", "content": "hello"}]
    path = session.save()
    assert "api_key" not in path.read_text(encoding="utf-8")
