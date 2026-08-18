"""Update checking and self-upgrade."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from jaigent import __version__, updater
from jaigent.updater import (
    CHECK_INTERVAL,
    Install,
    Release,
    UpdateError,
    detect_install,
    is_newer,
    parse_version,
    upgrade_command,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the check cache out of the real config directory."""
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))
    for name in updater.OPT_OUT_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def fake_response(payload: object, status: int = 200):  # noqa: ANN201
    def get(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            status, json=payload, request=httpx.Request("GET", updater.RELEASES_URL)
        )

    return get


# ------------------------------------------------------------ version parsing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2.3", (1, 2, 3)),
        ("v1.2.3", (1, 2, 3)),
        ("V1.2.3", (1, 2, 3)),
        ("0.5.0", (0, 5, 0)),
        ("  1.0  ", (1, 0)),
        ("2", (2,)),
    ],
)
def test_parse_version(text: str, expected: tuple[int, ...]) -> None:
    assert parse_version(text) == expected


def test_a_prerelease_suffix_is_ignored() -> None:
    assert parse_version("1.2.3rc1") == (1, 2, 3)


def test_nonsense_parses_to_zero_rather_than_raising() -> None:
    assert parse_version("not-a-version") == (0,)


@pytest.mark.parametrize(
    ("candidate", "current"),
    [
        ("0.5.1", "0.5.0"),
        ("1.0.0", "0.9.9"),
        ("v0.6.0", "0.5.1"),
        ("0.5.10", "0.5.9"),  # numeric, not lexicographic
        ("1.0.0", "1.0"),
    ],
)
def test_newer_versions_are_detected(candidate: str, current: str) -> None:
    assert is_newer(candidate, current) is True


@pytest.mark.parametrize(
    ("candidate", "current"),
    [
        ("0.5.0", "0.5.0"),
        ("0.4.9", "0.5.0"),
        ("0.5.9", "0.5.10"),
        ("1.0", "1.0.0"),
    ],
)
def test_same_or_older_versions_are_not_newer(candidate: str, current: str) -> None:
    assert is_newer(candidate, current) is False


# --------------------------------------------------------- install detection


def test_a_frozen_binary_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)

    assert detect_install().kind == "binary"


def test_a_source_checkout_is_detected() -> None:
    """The test suite runs from an editable install."""
    assert detect_install().kind == "source"


@pytest.mark.parametrize(
    ("kind", "upgradable"),
    [("binary", True), ("pip", True), ("pipx", True), ("source", False)],
)
def test_which_installs_can_self_upgrade(kind: str, upgradable: bool) -> None:
    assert Install(kind=kind, location="x").upgradable is upgradable


def test_every_kind_has_a_description() -> None:
    for kind in ("binary", "pip", "pipx", "source"):
        assert Install(kind=kind, location="x").describe()


def test_pip_upgrade_uses_this_interpreter() -> None:
    command = upgrade_command(Install(kind="pip", location="x"))

    assert command[:3] == [updater.sys.executable, "-m", "pip"]
    assert "--upgrade" in command


def test_pipx_upgrade() -> None:
    assert upgrade_command(Install(kind="pipx", location="x")) == ["pipx", "upgrade", "jaigent"]


def test_a_binary_is_upgraded_by_the_installer_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater.platform, "system", lambda: "Linux")

    command = upgrade_command(Install(kind="binary", location="x"))

    assert updater.INSTALL_SH in " ".join(command)


def test_windows_uses_the_powershell_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")

    command = upgrade_command(Install(kind="binary", location="x"))

    assert "powershell" in command[0]
    assert updater.INSTALL_PS1 in " ".join(command)


def test_a_source_install_explains_itself_instead() -> None:
    with pytest.raises(UpdateError, match="pip install -e"):
        upgrade_command(Install(kind="source", location="x"))


# ----------------------------------------------------------------- fetching


def test_a_release_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "get",
        fake_response(
            {
                "tag_name": "v9.9.9",
                "html_url": "https://example.com/r",
                "body": "notes",
                "published_at": "2026-01-01T00:00:00Z",
            }
        ),
    )

    release = updater.fetch_latest()

    assert release is not None
    assert release.version == "9.9.9"
    assert release.is_newer is True


def test_the_v_prefix_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", fake_response({"tag_name": "v1.2.3"}))

    assert updater.fetch_latest().version == "1.2.3"


def test_an_http_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo with no releases yet answers 404; that is not an error worth raising."""
    monkeypatch.setattr(httpx, "get", fake_response({}, status=404))

    assert updater.fetch_latest() is None


def test_being_offline_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", boom)

    assert updater.fetch_latest() is None


def test_a_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx, "get", boom)

    assert updater.fetch_latest() is None


def test_malformed_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, text="not json", request=httpx.Request("GET", updater.RELEASES_URL)
        )

    monkeypatch.setattr(httpx, "get", get)

    assert updater.fetch_latest() is None


def test_a_payload_without_a_tag_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", fake_response({"html_url": "x"}))

    assert updater.fetch_latest() is None


def test_a_non_object_payload_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", fake_response(["a", "list"]))

    assert updater.fetch_latest() is None


# ------------------------------------------------------------- check cadence


def test_a_first_run_is_due() -> None:
    assert updater.due_for_check() is True


def test_a_recent_check_is_not_due() -> None:
    updater.record_check(None)

    assert updater.due_for_check() is False


def test_an_old_check_is_due_again() -> None:
    updater.record_check(None, now=time.time() - CHECK_INTERVAL - 1)

    assert updater.due_for_check() is True


@pytest.mark.parametrize("name", updater.OPT_OUT_VARS)
def test_opting_out_disables_checking(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name, "1")

    assert updater.checks_disabled() is True
    assert updater.due_for_check() is False


def test_the_timestamp_is_recorded_even_when_the_fetch_failed() -> None:
    """Otherwise a repo with no releases would be re-checked on every command."""
    updater.record_check(None)

    assert json.loads(updater.state_path().read_text())["last_check"] > 0


def test_a_found_release_is_remembered() -> None:
    updater.record_check(Release(version="9.9.9", url="https://example.com/r"))

    state = json.loads(updater.state_path().read_text())
    assert state["latest"] == "9.9.9"


def test_a_corrupt_state_file_is_ignored() -> None:
    updater.state_path().write_text("{ not json")

    assert updater.due_for_check() is True


def test_an_unwritable_state_directory_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", boom)

    updater.record_check(None)  # must not raise


# ------------------------------------------------------------------- notices


def test_no_notice_without_a_cached_release() -> None:
    assert updater.cached_notice() == ""


def test_no_notice_when_up_to_date() -> None:
    updater.record_check(Release(version=__version__, url="x"))

    assert updater.cached_notice() == ""


def test_no_notice_for_an_older_release() -> None:
    updater.record_check(Release(version="0.0.1", url="x"))

    assert updater.cached_notice() == ""


def test_a_newer_release_produces_a_notice() -> None:
    updater.record_check(Release(version="99.0.0", url="x"))

    notice = updater.cached_notice()

    assert "99.0.0" in notice
    assert "jaigent update" in notice


# --------------------------------------------------------- background thread


def test_the_background_check_writes_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", fake_response({"tag_name": "v9.9.9"}))

    thread = updater.check_in_background()
    updater.finish_check(thread, timeout=5)

    assert json.loads(updater.state_path().read_text())["latest"] == "9.9.9"


def test_no_thread_is_started_when_not_due() -> None:
    updater.record_check(None)

    assert updater.check_in_background() is None


def test_no_thread_is_started_when_opted_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAIGENT_NO_UPDATE_CHECK", "1")

    assert updater.check_in_background() is None


def test_the_thread_is_a_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """It must never hold the process open."""
    monkeypatch.setattr(httpx, "get", fake_response({"tag_name": "v9.9.9"}))

    thread = updater.check_in_background()

    assert thread is not None
    assert thread.daemon is True
    updater.finish_check(thread, timeout=5)


def test_a_failing_background_check_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)

    updater.finish_check(updater.check_in_background(), timeout=5)  # must not raise


def test_finish_check_tolerates_none() -> None:
    updater.finish_check(None)


# ------------------------------------------------------------------ installing


def test_a_successful_upgrade_returns_its_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        updater,
        "_run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "Successfully installed", ""),
    )

    assert "Successfully installed" in updater.perform_update(Install(kind="pip", location="x"))


def test_a_failing_upgrade_raises_with_the_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        updater,
        "_run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "permission denied"),
    )

    with pytest.raises(UpdateError, match="permission denied"):
        updater.perform_update(Install(kind="pip", location="x"))


def test_a_missing_upgrade_tool_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("pipx not found")

    monkeypatch.setattr(updater, "_run", boom)

    with pytest.raises(UpdateError, match="pipx"):
        updater.perform_update(Install(kind="pipx", location="x"))


def test_a_hanging_upgrade_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("pip", 600)

    monkeypatch.setattr(updater, "_run", boom)

    with pytest.raises(UpdateError, match="timed out"):
        updater.perform_update(Install(kind="pip", location="x"))


def test_a_source_install_cannot_self_upgrade() -> None:
    with pytest.raises(UpdateError):
        updater.perform_update(Install(kind="source", location="x"))
