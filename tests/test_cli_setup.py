"""`jaigent init`, `doctor` and `update` at the command level.

None of the three had a test that went through `cli.main`. `init` matters most:
it is the command that writes a live API key to disk.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jaigent import cli


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JAIGENT_NO_UPDATE_CHECK", "1")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    return ws


class TestInitWritesTheKeySafely:
    """`jaigent init` creates a .env holding a live API key."""

    @pytest.fixture()
    def answers(self, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
        """Feed the interactive prompts, and stop it calling a real provider."""

        def make(
            provider: str = "1", key: str = "sk-secret", model: str = "", extra: str = ""
        ) -> None:
            replies = iter([provider, key, model, extra])
            monkeypatch.setattr("jaigent.cli.console.input", lambda *a, **k: next(replies, ""))

            class Reply:
                output = "ready"
                cost = type("C", (), {"usd": None, "format_usd": lambda self: "$0"})()

            monkeypatch.setattr(
                "jaigent.cli.Agent",
                lambda *a, **k: type("A", (), {"run": lambda self, p: Reply()})(),
            )

        return make

    def test_it_writes_a_dotenv(self, home: Path, answers) -> None:  # noqa: ANN001
        answers()
        assert cli.main(["init"]) == 0

        env = home / ".env"
        assert env.is_file()
        body = env.read_text(encoding="utf-8")
        assert "OPENAI_API_KEY=sk-secret" in body
        assert "JAIGENT_PROVIDER=openai" in body

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_the_dotenv_is_not_readable_by_anyone_else(self, home: Path, answers) -> None:  # noqa: ANN001
        answers()
        cli.main(["init"])

        mode = stat.S_IMODE((home / ".env").stat().st_mode)

        assert not mode & stat.S_IRGRP, f"group can read the API key (mode {mode:o})"
        assert not mode & stat.S_IROTH, f"world can read the API key (mode {mode:o})"

    def test_an_empty_key_is_refused_and_writes_nothing(self, home: Path, answers) -> None:  # noqa: ANN001
        answers(key="   ")

        assert cli.main(["init"]) == 1
        assert not (home / ".env").exists()

    def test_a_named_provider_is_accepted(self, home: Path, answers) -> None:  # noqa: ANN001
        answers(provider="anthropic", key="sk-ant")
        cli.main(["init"])

        body = (home / ".env").read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-ant" in body

    def test_an_out_of_range_choice_falls_back_rather_than_crashing(
        self, home: Path, answers, capsys: pytest.CaptureFixture
    ) -> None:  # noqa: ANN001
        answers(provider="999", key="sk-x")

        assert cli.main(["init", "--no-color"]) == 0
        assert "JAIGENT_PROVIDER=openai" in (home / ".env").read_text(encoding="utf-8")
        # The fallback is announced, never silent: choosing the wrong provider
        # sends the key to the wrong company.
        assert "using openai" in capsys.readouterr().out.lower()

    def test_a_key_pasted_with_wrapping_quotes_is_unwrapped(self, home: Path, answers) -> None:  # noqa: ANN001
        answers(key='"sk-secret"')

        assert cli.main(["init"]) == 0
        assert 'OPENAI_API_KEY="sk-secret"' not in (home / ".env").read_text(encoding="utf-8")
        assert "OPENAI_API_KEY=sk-secret" in (home / ".env").read_text(encoding="utf-8")

    def test_a_key_pasted_with_a_bearer_prefix_is_stripped(self, home: Path, answers) -> None:  # noqa: ANN001
        answers(key="Bearer sk-secret")

        assert cli.main(["init"]) == 0
        assert "OPENAI_API_KEY=sk-secret" in (home / ".env").read_text(encoding="utf-8")

    def test_an_empty_key_gets_exactly_one_more_chance(self, home: Path, answers) -> None:  # noqa: ANN001
        # Prompt order: provider, key, key (retry), model — so the third reply
        # is the second attempt at the key.
        answers(provider="1", key="", model="sk-late")

        assert cli.main(["init"]) == 0
        assert "OPENAI_API_KEY=sk-late" in (home / ".env").read_text(encoding="utf-8")

    def test_an_unknown_model_is_kept_when_confirmed(self, home: Path, answers) -> None:  # noqa: ANN001
        # Fourth reply answers "Use it anyway?" — empty means the default, yes.
        answers(key="sk-x", model="gpt-not-in-catalogue", extra="")

        assert cli.main(["init"]) == 0
        assert "JAIGENT_MODEL=gpt-not-in-catalogue" in (home / ".env").read_text(encoding="utf-8")

    def test_an_unknown_model_falls_back_to_the_default_when_declined(
        self, home: Path, answers
    ) -> None:  # noqa: ANN001
        answers(key="sk-x", model="gpt-not-in-catalogue", extra="n")

        assert cli.main(["init"]) == 0
        assert "JAIGENT_MODEL=gpt-4o-mini" in (home / ".env").read_text(encoding="utf-8")


class TestDoctor:
    def test_it_runs_and_reports(self, home: Path, capsys: pytest.CaptureFixture) -> None:
        code = cli.main(["doctor", "--no-color"])

        out = capsys.readouterr().out
        assert code in (0, 1)  # 1 when something is wrong, e.g. no API key
        assert "version" in out.lower()

    def test_it_does_not_crash_without_any_configuration(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JAIGENT_API_KEY"):
            monkeypatch.delenv(var, raising=False)

        assert cli.main(["doctor", "--no-color"]) in (0, 1)


class TestUpdateCommand:
    def test_check_reports_without_installing(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent.updater import Release

        monkeypatch.setattr(
            "jaigent.updater.fetch_latest", lambda **k: Release(version="99.0.0", url="u")
        )
        # Reaching the installer at all would mean --check ignored its own flag.
        monkeypatch.setattr(
            "jaigent.updater.perform_update",
            lambda *a, **k: pytest.fail("--check must not install anything"),
        )

        code = cli.main(["update", "--check", "--no-color"])

        assert code == 0
        assert "99.0.0" in capsys.readouterr().out

    def test_being_up_to_date_is_reported(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent import __version__
        from jaigent.updater import Release

        monkeypatch.setattr(
            "jaigent.updater.fetch_latest", lambda **k: Release(version=__version__, url="u")
        )

        assert cli.main(["update", "--check", "--no-color"]) == 0
        assert "up to date" in capsys.readouterr().out.lower()

    def test_a_missing_or_unreachable_release_is_reported_not_raised(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The real failure path: httpx blows up somewhere inside fetch_latest.

        fetch_latest promises to swallow every failure and return None, so this
        drives it through the actual guard rather than replacing the function.
        """
        import httpx

        def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(httpx, "get", boom)

        code = cli.main(["update", "--check", "--no-color"])

        assert code == 1
        err = capsys.readouterr().err.lower()
        assert "could not find" in err
