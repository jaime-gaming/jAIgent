"""The beta channel command and the feedback command."""

from __future__ import annotations

import json
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from jaigent import cli, feedback
from jaigent.settings_store import user_settings_path


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Settings and cwd that never touch the real machine."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("JAIGENT_HOME", str(home))
    monkeypatch.delenv("JAIGENT_BETA", raising=False)
    monkeypatch.chdir(project)
    return home


class TestBetaCommand:
    def test_join_turns_the_channel_on(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert cli.main(["beta", "join"]) == 0

        stored = json.loads(user_settings_path().read_text(encoding="utf-8"))
        assert stored == {"beta": True}
        assert "beta channel is on" in capsys.readouterr().out

    def test_join_is_idempotent(self, isolated_home: Path) -> None:
        assert cli.main(["beta", "join"]) == 0
        assert cli.main(["beta", "join"]) == 0

        stored = json.loads(user_settings_path().read_text(encoding="utf-8"))
        assert stored == {"beta": True}

    def test_leave_removes_the_key(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        cli.main(["beta", "join"])

        assert cli.main(["beta", "leave"]) == 0

        stored = json.loads(user_settings_path().read_text(encoding="utf-8"))
        assert stored == {}
        assert "beta channel is off" in capsys.readouterr().out

    def test_leave_when_off_is_a_noop(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert cli.main(["beta", "leave"]) == 0
        assert "already off" in capsys.readouterr().out

    def test_status_names_the_source(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert cli.main(["beta", "status"]) == 0
        assert "off" in capsys.readouterr().out

        cli.main(["beta", "join"])
        assert cli.main(["beta", "status"]) == 0
        out = capsys.readouterr().out
        assert "on" in out
        assert "user settings" in out

    def test_bare_beta_shows_status(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert cli.main(["beta"]) == 0
        assert "off" in capsys.readouterr().out

    def test_env_override_is_reported(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("JAIGENT_BETA", "1")

        assert cli.main(["beta", "status"]) == 0
        assert "JAIGENT_BETA" in capsys.readouterr().out

        assert cli.main(["beta", "leave"]) == 0
        out = capsys.readouterr().out
        assert "stays on (JAIGENT_BETA=1)" in out
        assert "unset it to leave" in out


class TestFeedbackText:
    def test_title_uses_the_first_line(self) -> None:
        assert feedback.issue_title("hello world") == "[feedback] hello world"
        assert feedback.issue_title("one\ntwo") == "[feedback] one"
        assert feedback.issue_title("") == "[feedback]"
        assert feedback.issue_title("   ") == "[feedback]"

    def test_long_titles_are_trimmed(self) -> None:
        title = feedback.issue_title("x" * 200)

        assert title.startswith("[feedback] ")
        assert len(title) <= len("[feedback] ") + 80

    def test_body_carries_the_message_and_a_footer(self) -> None:
        body = feedback.issue_body("the picker rules")

        assert body.startswith("the picker rules\n\n---\n")
        assert "jaigent" in body and "python" in body

    def test_footer_has_no_secrets(self) -> None:
        footer = feedback.environment_footer()

        assert "sk-" not in footer and "key" not in footer.lower()

    def test_url_round_trips(self) -> None:
        url = feedback.issue_url("hello & goodbye")

        assert url.startswith(feedback.ISSUES_URL + "?")
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert parsed["title"] == ["[feedback] hello & goodbye"]
        assert parsed["body"][0].startswith("hello & goodbye")


class TestGhDelivery:
    def _completed(self, returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["gh"], returncode=returncode, stdout=stdout, stderr=""
        )

    def test_no_gh_means_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feedback.shutil, "which", lambda name: None)

        assert feedback.gh_available() is False

    def test_auth_status_decides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feedback.shutil, "which", lambda name: "/usr/bin/gh")
        monkeypatch.setattr(feedback.subprocess, "run", lambda *a, **k: self._completed(1))
        assert feedback.gh_available() is False

        monkeypatch.setattr(feedback.subprocess, "run", lambda *a, **k: self._completed(0))
        assert feedback.gh_available() is True

    def test_gh_errors_mean_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feedback.shutil, "which", lambda name: "/usr/bin/gh")

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("noexec")

        monkeypatch.setattr(feedback.subprocess, "run", boom)

        assert feedback.gh_available() is False

    def test_create_returns_the_issue_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            seen["argv"] = args[0]
            return self._completed(0, "https://github.com/x/y/issues/12\n")

        monkeypatch.setattr(feedback.subprocess, "run", fake_run)

        assert (
            feedback.create_issue_via_gh("[feedback] hi", "body")
            == "https://github.com/x/y/issues/12"
        )
        assert seen["argv"][:3] == ["gh", "issue", "create"]
        assert "--repo" in seen["argv"]

    def test_create_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feedback.subprocess, "run", lambda *a, **k: self._completed(1, "oops"))
        assert feedback.create_issue_via_gh("t", "b") is None

        monkeypatch.setattr(
            feedback.subprocess, "run", lambda *a, **k: self._completed(0, "no url here")
        )
        assert feedback.create_issue_via_gh("t", "b") is None

    def test_deliver_prefers_gh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feedback, "gh_available", lambda: True)
        monkeypatch.setattr(
            feedback, "create_issue_via_gh", lambda title, body: "https://x/issues/1"
        )
        opened: list[str] = []
        monkeypatch.setattr(feedback.webbrowser, "open", lambda url: opened.append(url) or True)

        delivery = feedback.deliver("hi")

        assert delivery == feedback.Delivery(url="https://x/issues/1", method="gh")
        assert opened == []

    def test_deliver_falls_back_to_a_form_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feedback, "gh_available", lambda: False)
        opened: list[str] = []
        monkeypatch.setattr(feedback.webbrowser, "open", lambda url: opened.append(url) or True)

        delivery = feedback.deliver("hi & bye")

        assert delivery.method == "browser"
        assert delivery.opened is True
        assert delivery.url.startswith(feedback.ISSUES_URL + "?")
        assert opened == [delivery.url]

    def test_deliver_survives_a_browser_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feedback, "gh_available", lambda: False)

        def boom(url: str) -> bool:
            raise RuntimeError("no display")

        monkeypatch.setattr(feedback.webbrowser, "open", boom)

        delivery = feedback.deliver("hi")

        assert delivery.method == "browser"
        assert delivery.opened is False
        assert delivery.url.startswith(feedback.ISSUES_URL + "?")

    def test_deliver_with_no_open_stays_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feedback, "gh_available", lambda: False)

        def boom(url: str) -> bool:
            raise AssertionError("browser must not open")

        monkeypatch.setattr(feedback.webbrowser, "open", boom)

        delivery = feedback.deliver("hi", open_browser=False)

        assert delivery.opened is False


class TestFeedbackCommand:
    def test_message_goes_straight_through(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_deliver(message: str, *, open_browser: bool = True) -> feedback.Delivery:
            seen["message"] = message
            seen["open_browser"] = open_browser
            return feedback.Delivery(url="https://x/issues/9", method="gh")

        monkeypatch.setattr(feedback, "deliver", fake_deliver)

        assert cli.main(["feedback", "the", "picker", "rules"]) == 0

        assert seen == {"message": "the picker rules", "open_browser": True}
        assert "feedback sent" in capsys.readouterr().out

    def test_no_open_reaches_deliver(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_deliver(message: str, *, open_browser: bool = True) -> feedback.Delivery:
            seen["open_browser"] = open_browser
            return feedback.Delivery(url="https://x/form", method="browser")

        monkeypatch.setattr(feedback, "deliver", fake_deliver)

        assert cli.main(["feedback", "hi", "--no-open"]) == 0
        assert seen == {"open_browser": False}

    def test_browser_url_is_printed(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            feedback,
            "deliver",
            lambda message, *, open_browser=True: feedback.Delivery(
                url=feedback.ISSUES_URL + "?title=x", method="browser", opened=True
            ),
        )

        assert cli.main(["feedback", "hi"]) == 0
        assert (feedback.ISSUES_URL + "?title=x") in capsys.readouterr().out

    def test_missing_message_is_asked_for(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr("jaigent.cli.console.input", lambda *a, **k: "typed it")
        monkeypatch.setattr(
            feedback,
            "deliver",
            lambda message, *, open_browser=True: feedback.Delivery(
                url="https://x/issues/2", method="gh"
            ),
        )

        assert cli.main(["feedback"]) == 0
        assert "feedback sent" in capsys.readouterr().out

    def test_eof_is_an_error_not_a_traceback(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> str:
            raise EOFError

        monkeypatch.setattr("jaigent.cli.console.input", boom)

        assert cli.main(["feedback"]) == 1

    def test_blank_answer_is_an_error(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("jaigent.cli.console.input", lambda *a, **k: "   ")

        assert cli.main(["feedback"]) == 1


class TestBetaProjectOverride:
    """Project settings win over user settings, and the messages say so."""

    def _project_beta(self, value: bool) -> None:
        project_file = Path.cwd() / ".jaigent" / "settings.json"
        project_file.parent.mkdir(parents=True, exist_ok=True)
        project_file.write_text(json.dumps({"beta": value}), encoding="utf-8")

    def test_join_warns_when_project_keeps_it_off(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        self._project_beta(False)

        assert cli.main(["beta", "join"]) == 0

        # Rich wraps at the console width, and where it wraps depends on the
        # tmp path length — macOS splits "project settings" across lines.
        out = capsys.readouterr().out.replace("\n", "")
        assert "still off" in out
        assert "project settings" in out

    def test_leave_names_project_settings_not_the_env_var(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        self._project_beta(True)

        assert cli.main(["beta", "leave"]) == 0

        out = capsys.readouterr().out.replace("\n", "")
        assert "project settings" in out
        assert "JAIGENT_BETA" not in out

    def test_status_names_project_settings(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        self._project_beta(True)

        assert cli.main(["beta", "status"]) == 0
        assert "project settings" in capsys.readouterr().out.replace("\n", "")

    def test_unreadable_settings_are_reported_not_hidden(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        user_path = user_settings_path()
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_bytes(b"\xff\xfe")

        assert cli.main(["beta"]) == 0
        assert "unreadable" in capsys.readouterr().out

    def test_join_on_unreadable_settings_is_a_clean_error(
        self, isolated_home: Path, capsys: pytest.CaptureFixture
    ) -> None:
        user_path = user_settings_path()
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_bytes(b"\xff\xfe")

        assert cli.main(["beta", "join"]) == 78
        err = capsys.readouterr().err.replace("\n", "")
        assert "not valid UTF-8" in err
        assert "Traceback" not in err


class TestBracketPaths:
    """`[` in a path must not be eaten as rich markup."""

    def test_join_prints_the_real_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        home = tmp_path / "[weird]" / "home"
        home.mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("JAIGENT_HOME", str(home))
        monkeypatch.delenv("JAIGENT_BETA", raising=False)
        monkeypatch.chdir(project)

        assert cli.main(["beta", "join"]) == 0

        out = capsys.readouterr().out.replace("\n", "")
        assert str(user_settings_path()) in out

    def test_settings_set_prints_the_real_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        home = tmp_path / "[weird]" / "home"
        home.mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("JAIGENT_HOME", str(home))
        monkeypatch.chdir(project)

        assert cli.main(["settings", "set", "model", "x[y]"]) == 0

        out = capsys.readouterr().out.replace("\n", "")
        assert str(user_settings_path()) in out
        assert "x[y]" in out

    def test_config_errors_keep_their_brackets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        home = tmp_path / "[weird]" / "home"
        home.mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("JAIGENT_HOME", str(home))
        monkeypatch.delenv("JAIGENT_BETA", raising=False)
        monkeypatch.chdir(project)
        user_path = user_settings_path()
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_bytes(b"\xff\xfe")

        assert cli.main(["beta", "join"]) == 78

        err = capsys.readouterr().err.replace("\n", "")
        assert str(user_path) in err

    def test_run_errors_keep_their_brackets(self, capsys: pytest.CaptureFixture) -> None:
        from jaigent.errors import ConfigurationError

        cli._print_run_error(ConfigurationError("bad file [/tmp/[x]/f]"), None)

        err = capsys.readouterr().err.replace("\n", "")
        assert "bad file [/tmp/[x]/f]" in err


class TestFeedbackRobustness:
    def test_undecodable_gh_output_is_replaced_not_crashed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=b"https://github.com/x/y/issues/3\n\xff",
            )

        monkeypatch.setattr(feedback.subprocess, "run", fake_run)

        assert feedback.create_issue_via_gh("t", "b") == "https://github.com/x/y/issues/3"

    def test_long_messages_fit_the_form_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = feedback.issue_url("word " * 5000)

        body = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["body"][0]
        assert len(body) < 4000
        assert "truncated" in body

    def test_gh_gets_the_full_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(feedback, "gh_available", lambda: True)

        def fake_create(title: str, body: str) -> str:
            seen["body"] = body
            return "https://x/issues/1"

        monkeypatch.setattr(feedback, "create_issue_via_gh", fake_create)

        feedback.deliver("word " * 5000)

        assert "truncated" not in seen["body"]
        assert seen["body"].count("word") == 5000

    def test_none_message_does_not_crash(self) -> None:
        assert feedback.issue_title(None) == "[feedback]"  # type: ignore[arg-type]
        assert feedback.issue_body(None).endswith("---\n" + feedback.environment_footer() + "\n")  # type: ignore[arg-type]
