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
    # Wide consoles: rich wraps at the terminal width, and a message broken in
    # the middle would make every assertion below depend on the layout.
    cli.console.width = 400
    cli.err_console.width = 400
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
        from jaigent.updater import FetchResult, Release

        monkeypatch.setattr(
            "jaigent.updater.fetch_latest_detailed",
            lambda **k: FetchResult(release=Release(version="99.0.0", url="u"), reason="ok"),
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
        from jaigent.updater import FetchResult, Release

        monkeypatch.setattr(
            "jaigent.updater.fetch_latest_detailed",
            lambda **k: FetchResult(release=Release(version=__version__, url="u"), reason="ok"),
        )

        monkeypatch.setattr(
            "jaigent.updater.inspect_source",
            lambda **k: __import__("jaigent.updater", fromlist=["SourceSync"]).SourceSync(),
        )
        assert cli.main(["update", "--check", "--no-color"]) == 0
        out = capsys.readouterr().out.lower()
        assert "in sync" in out or "up to date" in out

    def test_a_missing_or_unreachable_release_is_reported_not_raised(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The real failure path: httpx blows up somewhere inside the fetch.

        The fetch promises to swallow every failure and report it, so this
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

    # ------------------------------------------------ the update is verified

    @staticmethod
    def _newer_release(monkeypatch: pytest.MonkeyPatch) -> str:  # noqa: ANN001
        """Pretend GitHub has a newer release, and the source check is inert."""
        from jaigent.updater import FetchResult, Release, SourceSync

        monkeypatch.setattr(
            "jaigent.updater.fetch_latest_detailed",
            lambda **k: FetchResult(release=Release(version="99.0.0", url="u"), reason="ok"),
        )
        monkeypatch.setattr("jaigent.updater.inspect_source", lambda **k: SourceSync())
        monkeypatch.setattr("jaigent.updater.perform_update", lambda *a, **k: "done")
        return "99.0.0"

    def test_a_verified_upgrade_reports_the_version_it_proved(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent.updater import Verification

        self._newer_release(monkeypatch)
        seen: dict[str, object] = {}

        def fake_verify(install, *, expected):  # noqa: ANN001, ANN202
            seen["expected"] = expected
            return Verification(
                command=["jaigent"], reported=expected, expected=expected, before="0.0.1"
            )

        monkeypatch.setattr("jaigent.updater.verify_update", fake_verify)

        code = cli.main(["update", "--no-color", "--yes"])

        assert code == 0
        assert seen["expected"] == "99.0.0"
        assert "Updated to 99.0.0" in capsys.readouterr().out

    def test_an_update_that_changed_nothing_is_not_reported_as_success(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The bug: the upgrade command exited 0 and nothing was installed."""
        from jaigent.updater import Verification

        self._newer_release(monkeypatch)
        monkeypatch.setattr(
            "jaigent.updater.verify_update",
            lambda install, *, expected: Verification(
                command=["jaigent"], reported="0.0.1", expected=expected, before="0.0.1"
            ),
        )

        code = cli.main(["update", "--no-color", "--yes"])
        captured = capsys.readouterr()

        assert code == 1
        assert "Updated successfully" not in captured.out
        assert "nothing changed" in captured.err

    def test_a_stale_copy_on_the_path_is_named(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent import __version__
        from jaigent.updater import Verification

        self._newer_release(monkeypatch)
        # The upgrade landed in one copy and the shell starts another, which
        # still reports the version we began with: that is shadowing.
        monkeypatch.setattr(
            "jaigent.updater.verify_update",
            lambda install, *, expected: Verification(
                command=["/opt/venv/bin/jaigent"],
                reported=expected,
                expected=expected,
                before=__version__,
                resolved="/usr/local/bin/jaigent",
                resolved_version=__version__,
                resolved_path="/usr/local/bin/jaigent",
                installed_path="/opt/venv/bin/jaigent",
                others=("/home/me/.local/bin/jaigent (99.0.0)",),
            ),
        )

        code = cli.main(["update", "--no-color", "--yes"])
        err = capsys.readouterr().err

        assert code == 1
        assert "99.0.0 is installed, but in a copy it does not start" in err
        assert "Remove /usr/local/bin/jaigent" in err
        assert "/home/me/.local/bin/jaigent (99.0.0)" in err

    def test_a_stale_copy_is_named_when_nothing_was_installed(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent import __version__
        from jaigent.updater import Verification

        self._newer_release(monkeypatch)
        monkeypatch.setattr(
            "jaigent.updater.verify_update",
            lambda install, *, expected: Verification(
                command=["/opt/venv/bin/jaigent"],
                reported=__version__,
                expected=expected,
                before=__version__,
                resolved="/usr/local/bin/jaigent",
                resolved_version=__version__,
                resolved_path="/usr/local/bin/jaigent",
                installed_path="/opt/venv/bin/jaigent",
            ),
        )

        code = cli.main(["update", "--no-color", "--yes"])
        err = capsys.readouterr().err

        assert code == 1
        assert "an older copy this update did not touch" in err

    def test_a_pip_install_is_told_about_the_git_fallback(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """jaigent is not on PyPI yet, so pip quietly installs nothing newer."""
        from jaigent.updater import Install, Verification

        self._newer_release(monkeypatch)
        monkeypatch.setattr(
            "jaigent.updater.detect_install", lambda: Install(kind="pip", location="/x")
        )
        monkeypatch.setattr(
            "jaigent.updater.verify_update",
            lambda install, *, expected: Verification(
                command=["python", "-m", "jaigent"],
                reported="0.0.1",
                expected=expected,
                before="0.0.1",
            ),
        )

        code = cli.main(["update", "--no-color", "--yes"])
        err = capsys.readouterr().err

        assert code == 1
        assert "git+https://github.com/jaime-gaming/jaigent.git" in err

    # ------------------------------------------------- channel-aware checking

    def test_beta_check_compares_against_beta(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """`--beta` must compare the checkout against beta, not main."""
        from jaigent import __version__
        from jaigent.updater import FetchResult, Release, SourceSync

        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "jaigent.updater.fetch_latest_detailed",
            lambda **k: FetchResult(release=Release(version=__version__, url="u"), reason="ok"),
        )

        def fake_inspect(**kwargs):  # noqa: ANN003, ANN202
            seen.update(kwargs)
            return SourceSync(
                local_sha="a" * 40, remote_sha="a" * 40, branch="beta", channel="beta"
            )

        monkeypatch.setattr("jaigent.updater.inspect_source", fake_inspect)

        assert cli.main(["update", "--check", "--beta", "--no-color"]) == 0
        assert seen.get("branch") == "beta"
        assert "up to date" in capsys.readouterr().out.lower()

    def test_an_ahead_checkout_is_not_offered_a_useless_pull(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent import __version__
        from jaigent.updater import FetchResult, Release, SourceSync

        monkeypatch.setattr(
            "jaigent.updater.fetch_latest_detailed",
            lambda **k: FetchResult(release=Release(version=__version__, url="u"), reason="ok"),
        )
        monkeypatch.setattr(
            "jaigent.updater.inspect_source",
            lambda **k: SourceSync(
                local_sha="a" * 40,
                remote_sha="b" * 40,
                branch="main",
                channel="main",
                ahead=2,
                behind=0,
            ),
        )
        monkeypatch.setattr(
            "jaigent.updater.perform_update",
            lambda *a, **k: pytest.fail("nothing to pull, so nothing must run"),
        )

        assert cli.main(["update", "--check", "--no-color"]) == 0
        assert "nothing to pull" in capsys.readouterr().out.lower()

    def test_no_releases_on_a_synced_checkout_is_up_to_date(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent.updater import FetchResult, SourceSync

        monkeypatch.setattr(
            "jaigent.updater.fetch_latest_detailed",
            lambda **k: FetchResult(release=None, reason="no-releases"),
        )
        monkeypatch.setattr(
            "jaigent.updater.inspect_source",
            lambda **k: SourceSync(local_sha="a" * 40, remote_sha="a" * 40),
        )

        assert cli.main(["update", "--check", "--no-color"]) == 0
        assert "up to date" in capsys.readouterr().out.lower()

    def test_a_rate_limit_names_itself(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from jaigent.updater import FetchResult, SourceSync

        monkeypatch.setattr(
            "jaigent.updater.fetch_latest_detailed",
            lambda **k: FetchResult(release=None, reason="rate-limited"),
        )
        monkeypatch.setattr("jaigent.updater.inspect_source", lambda **k: SourceSync())

        assert cli.main(["update", "--check", "--no-color"]) == 1
        assert "rate limit" in capsys.readouterr().err.lower()

    def test_a_source_sync_that_moved_the_version_counts_as_updated(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Beta commits can bump the version without a newer release existing."""
        from jaigent import __version__
        from jaigent.updater import FetchResult, Install, Release, SourceSync, Verification

        monkeypatch.setattr(
            "jaigent.updater.fetch_latest_detailed",
            lambda **k: FetchResult(release=Release(version=__version__, url="u"), reason="ok"),
        )
        monkeypatch.setattr(
            "jaigent.updater.inspect_source",
            lambda **k: SourceSync(
                local_sha="a" * 40,
                remote_sha="b" * 40,
                branch="beta",
                channel="beta",
                ahead=0,
                behind=5,
            ),
        )
        monkeypatch.setattr(
            "jaigent.updater.detect_install",
            lambda: Install(kind="source", location="/x"),
        )
        monkeypatch.setattr("jaigent.updater.upgrade_summary", lambda *a, **k: "git ...")
        monkeypatch.setattr("jaigent.updater.perform_update", lambda *a, **k: "done")
        monkeypatch.setattr(
            "jaigent.updater.verify_update",
            lambda install, *, expected: Verification(
                command=["python", "-m", "jaigent"],
                reported="99.0.0",
                expected=expected,
                before=__version__,
            ),
        )

        code = cli.main(["update", "--beta", "--no-color", "--yes"])
        out = capsys.readouterr().out

        assert code == 0
        assert "in sync with beta" in out
