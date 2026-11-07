"""`jaigent usage`: adding up what saved sessions actually cost."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from jaigent import cli, usage
from jaigent.session import Session


def make_session(
    tmp_path: Path,
    *,
    model: str = "gpt-4o-mini",
    prompt_tokens: int = 1_000,
    completion_tokens: int = 500,
    updated: float | None = None,
    title: str = "a chat",
) -> Session:
    """Save one session with usage recorded, without running an agent."""
    session = Session.new(provider="openai", model=model)
    session.title = title
    session.messages = [{"role": "user", "content": "hi"}]
    session.usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if updated is not None:
        session.updated = updated
    session.save()
    return session


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JAIGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


class TestReport:
    def test_sums_tokens_and_price(self, tmp_path: Path) -> None:
        make_session(tmp_path)
        make_session(tmp_path, prompt_tokens=2_000, completion_tokens=1_000)

        report = usage.build_report()

        assert len(report.rows) == 2
        assert report.input_tokens == 3_000
        assert report.output_tokens == 1_500
        assert report.usd > 0

    def test_an_unknown_model_is_not_free(self, tmp_path: Path) -> None:
        make_session(tmp_path, model="mystery-model-x")

        report = usage.build_report()

        assert report.unpriced == 1
        assert report.rows[0].priced is False
        # No price is invented for it; the total excludes it and says so.
        assert report.usd == 0
        assert report.total_tokens == 1_500

    def test_periods_exclude_older_sessions(self, tmp_path: Path) -> None:
        now = time.time()
        make_session(tmp_path, updated=now - 60, title="fresh")
        make_session(tmp_path, updated=now - 10 * 24 * 3600, title="stale")

        week = usage.build_report("week", now=now)
        everything = usage.build_report("all", now=now)

        assert [row.session.title for row in week.rows] == ["fresh"]
        assert len(everything.rows) == 2

    def test_a_session_counts_by_when_it_was_last_used(self, tmp_path: Path) -> None:
        now = time.time()
        old = make_session(tmp_path, updated=now - 30 * 24 * 3600)
        # Started long ago, used today: the tokens were spent today.
        old.created = now - 30 * 24 * 3600
        old.updated = now - 60
        old.save()

        assert len(usage.build_report("day", now=now).rows) == 1

    def test_an_unknown_period_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unknown period"):
            usage.parse_period("fortnight")

    def test_by_model_lists_the_costliest_first(self, tmp_path: Path) -> None:
        make_session(tmp_path, model="gpt-4o-mini", prompt_tokens=10, completion_tokens=10)
        make_session(
            tmp_path, model="claude-sonnet-4", prompt_tokens=100_000, completion_tokens=50_000
        )

        ranked = usage.build_report().by_model()

        assert ranked[0][0] == "claude-sonnet-4"
        assert ranked[1][0] == "gpt-4o-mini"

    def test_day_starts_have_no_gaps(self, tmp_path: Path) -> None:
        make_session(tmp_path, updated=time.time() - 3 * 24 * 3600)

        series = usage.day_starts(usage.build_report(), days=7)

        assert len(series) == 7
        assert sum(entry[3] for entry in series) == 1
        assert series == sorted(series)


class TestCommand:
    def test_it_renders_a_summary(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        make_session(tmp_path)

        assert cli.main(["usage", "--no-color"]) == 0

        out = capsys.readouterr().out
        assert "estimated across 1 session" in out
        assert "gpt-4o-mini" in out

    def test_json_is_one_object(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        make_session(tmp_path)

        assert cli.main(["usage", "--json", "--no-color"]) == 0

        body = json.loads(capsys.readouterr().out)
        assert body["ok"] is True
        assert body["sessions"] == 1
        assert body["total_tokens"] == 1_500
        assert body["unpriced_sessions"] == 0
        assert body["by_model"][0]["model"] == "gpt-4o-mini"

    def test_json_reports_unpriced_sessions(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        make_session(tmp_path, model="mystery-model-x")

        cli.main(["usage", "--json", "--no-color"])

        body = json.loads(capsys.readouterr().out)
        assert body["unpriced_sessions"] == 1

    def test_an_empty_store_says_so(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["usage", "--no-color"]) == 0

        assert "No saved sessions" in capsys.readouterr().out

    def test_a_bad_period_exits_2(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["usage", "--period", "fortnight", "--no-color"]) == 2

        assert "Unknown period" in capsys.readouterr().err

    def test_sessions_zero_hides_the_list(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        make_session(tmp_path)

        cli.main(["usage", "--sessions", "0", "--no-color"])

        assert "most recent" not in capsys.readouterr().out


def test_the_command_runs_instead_of_becoming_a_prompt() -> None:
    """`normalise_argv` maps unknown words to `run <prompt>`.

    Adding a subcommand without adding it to ``COMMANDS`` therefore turns
    `jaigent usage` into a prompt for the model — silently, and billed to the
    user. The two lists must agree.
    """
    import argparse

    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    registered = set(subparsers.choices)

    assert registered == set(cli.COMMANDS)
    assert cli.normalise_argv(["usage", "--json"])[0] == "usage"
