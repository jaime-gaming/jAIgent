"""Update checking and self-upgrade."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
        ("1.0.1", "1.0"),
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
        ("1.0.0", "1.0"),
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
    [("binary", True), ("pip", True), ("pipx", True), ("source", True)],
)
def test_which_installs_can_self_upgrade(kind: str, upgradable: bool) -> None:
    assert Install(kind=kind, location="x").upgradable is upgradable


def test_every_kind_has_a_description() -> None:
    for kind in ("binary", "pip", "pipx", "source"):
        assert Install(kind=kind, location="x").describe()


def test_beta_pip_installs_from_the_beta_branch() -> None:
    command = upgrade_command(Install(kind="pip", location="x"), beta=True)
    assert command[-1].endswith("@beta")


def test_beta_source_fetches_the_beta_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`update --beta` on a checkout pulls beta down; it never pushes up.

    The old behaviour published the local checkout onto origin/beta, which is
    a release action, not an update — and a surprising one to take implicitly.
    """
    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    command = upgrade_command(Install(kind="source", location=str(tmp_path)), beta=True)
    assert command[-2:] == ["origin", "beta"]
    assert "push" not in command


def test_beta_source_steps_switch_from_main_to_beta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    steps = updater.source_update_steps(tmp_path, "beta")
    assert steps[0][-2:] == ["origin", "beta"]
    assert steps[1][-2:] == ["switch", "beta"]
    assert steps[2][-2:] == ["--ff-only", "origin/beta"]


def test_stable_source_steps_switch_from_beta_to_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "beta")
    steps = updater.source_update_steps(tmp_path, "main")
    assert steps[1][-2:] == ["switch", "main"]
    assert steps[2][-1] == "origin/main"


def test_source_steps_stay_put_when_already_on_the_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    steps = updater.source_update_steps(tmp_path, "main")
    assert len(steps) == 2  # fetch, then fast-forward
    assert all("switch" not in step for step in steps)


def test_source_steps_refuse_a_feature_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "arena/some-session")
    with pytest.raises(UpdateError, match="arena/some-session"):
        updater.source_update_steps(tmp_path, "main")


def test_source_steps_refuse_a_detached_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "HEAD")
    with pytest.raises(UpdateError, match="detached HEAD"):
        updater.source_update_steps(tmp_path, "main")


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


def test_a_source_install_without_git_explains_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "find_source_root", lambda start=None: None)
    with pytest.raises(UpdateError, match="pip install -e"):
        upgrade_command(Install(kind="source", location="x"))


def test_upgrade_summary_names_the_reinstall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    summary = updater.upgrade_summary(Install(kind="source", location=str(tmp_path)))
    assert "git" in summary and "pip install -e" in summary


def test_a_source_install_fetches_then_fast_forwards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    command = upgrade_command(Install(kind="source", location=str(tmp_path)))
    assert command[:2] == ["git", "-C"]
    assert command[-3:] == ["fetch", "origin", "main"]
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    steps = updater.source_update_steps(tmp_path, "main")
    assert steps[-1][-2:] == ["--ff-only", "origin/main"]


# ----------------------------------------------------------------- fetching


def test_github_requests_send_a_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []

    def get(*args: object, **kwargs: object) -> httpx.Response:
        headers = kwargs.get("headers") or {}
        seen.append(dict(headers))
        return httpx.Response(
            200, json={"tag_name": "v1.0.0"}, request=httpx.Request("GET", updater.RELEASES_URL)
        )

    monkeypatch.setattr(httpx, "get", get)
    updater.fetch_latest()
    assert seen and "jAIgent/" in seen[0].get("User-Agent", "")


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


def test_a_missing_release_is_distinguished_from_being_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "get", fake_response({}, status=404))

    result = updater.fetch_latest_detailed()

    assert result.release is None
    assert result.reason == "no-releases"


def test_a_rate_limit_is_reported_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    def limited(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0"},
            request=httpx.Request("GET", updater.RELEASES_URL),
        )

    monkeypatch.setattr(httpx, "get", limited)

    result = updater.fetch_latest_detailed()

    assert result.release is None
    assert result.reason == "rate-limited"


def test_a_dropped_connection_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", boom)

    result = updater.fetch_latest_detailed()

    assert result.release is None
    assert result.reason == "unreachable"


def test_a_successful_fetch_reports_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", fake_response({"tag_name": "v1.2.3"}))

    result = updater.fetch_latest_detailed()

    assert result.reason == "ok"
    assert result.release is not None and result.release.version == "1.2.3"


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


def test_source_sync_summary_when_behind() -> None:
    sync = updater.SourceSync(
        local_sha="aaa1111dead",
        remote_sha="bbb2222beef",
        root="/tmp",
        ahead=0,
        behind=3,
    )
    assert sync.available is True
    assert sync.synced is False
    assert sync.update_available is True
    assert sync.ahead_only is False
    assert "3 commits behind main" in sync.summary()


def test_source_sync_summary_when_ahead_only() -> None:
    """Ahead of the channel, a pull would do nothing — say so, not \"not synced\"."""
    sync = updater.SourceSync(
        local_sha="aaa1111dead",
        remote_sha="bbb2222beef",
        root="/tmp",
        ahead=2,
        behind=0,
    )
    assert sync.update_available is False
    assert sync.ahead_only is True
    assert "2 commits ahead of main" in sync.summary()
    assert "nothing to pull" in sync.summary()


def test_source_sync_summary_when_diverged() -> None:
    sync = updater.SourceSync(
        local_sha="aaa1111dead",
        remote_sha="bbb2222beef",
        root="/tmp",
        ahead=1,
        behind=4,
    )
    assert sync.update_available is True
    assert "diverged" in sync.summary()


def test_source_sync_summary_without_counts_falls_back_to_shas() -> None:
    sync = updater.SourceSync(local_sha="aaa1111dead", remote_sha="bbb2222beef", root="/tmp")
    assert sync.update_available is True
    assert "not synced" in sync.summary()
    assert "aaa1111" in sync.summary()


def test_source_sync_summary_uses_the_compared_channel() -> None:
    sync = updater.SourceSync(local_sha="aaa1111dead", remote_sha="aaa1111dead", channel="beta")
    assert "matches beta" in sync.summary()


def test_source_sync_summary_when_matched() -> None:
    sha = "abc1234ffff"
    sync = updater.SourceSync(local_sha=sha, remote_sha=sha, root="/tmp")
    assert sync.synced is True
    assert "matches main" in sync.summary()


def test_inspect_source_without_a_checkout(tmp_path: Path) -> None:
    assert updater.inspect_source(start=tmp_path).available is False


def test_inspect_source_compares_against_the_requested_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--beta` must compare against beta, not main.

    The regression: the flag changed the channel line while the source check
    still fetched main's SHA, so a beta checkout was always \"not synced\".
    """
    seen: dict[str, str] = {}

    def fake_sha(branch: str, timeout: float = 1.0) -> tuple[str | None, str]:
        seen["branch"] = branch or ""
        return "bbb2222beef", "ok"

    real_git = updater._git

    def fake_git(*args: str, **kwargs: object) -> str | None:
        if args[:2] == ("rev-parse", "HEAD"):
            return "aaa1111dead"
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return "beta"
        if args[0] == "status":
            return ""
        if args[0] == "fetch":
            return ""
        if args[0] == "rev-list":
            return "2\t0"
        return real_git(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "fetch_branch_sha", fake_sha)
    monkeypatch.setattr(updater, "_git", fake_git)

    sync = updater.inspect_source(branch="beta")

    assert seen["branch"] == "beta"
    assert sync.channel == "beta"
    assert sync.branch == "beta"
    assert sync.ahead == 2
    assert sync.behind == 0
    assert sync.ahead_only is True


def test_a_deleted_channel_branch_is_named_not_called_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub answers 404/422 when the branch is gone; the offline message lies."""

    def gone(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            422,
            json={"message": "No commit found for SHA: beta"},
            request=httpx.Request("GET", updater.BETA_COMMITS_URL),
        )

    monkeypatch.setattr(httpx, "get", gone)

    sha, reason = updater.fetch_branch_sha("beta")

    assert sha is None
    assert reason == "no-branch"
    assert updater.fetch_main_sha(branch="beta") is None


def test_inspect_source_reports_a_missing_channel_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main" if "rev-parse" in a else "")
    monkeypatch.setattr(updater, "fetch_branch_sha", lambda branch, timeout: (None, "no-branch"))

    sync = updater.inspect_source(branch="beta")

    assert sync.available is True
    assert sync.update_available is False
    assert "no 'beta' branch" in sync.summary()


def test_fetching_a_missing_channel_branch_explains_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        if "fetch" in command:
            return subprocess.CompletedProcess(
                command, 1, "", "error: couldn't find remote ref beta"
            )
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    monkeypatch.setattr(updater, "_run", fake_run)

    with pytest.raises(UpdateError, match="no 'beta' branch"):
        updater.perform_update(Install(kind="source", location=str(tmp_path)), beta=True)


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


def test_a_source_install_without_git_cannot_self_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updater, "find_source_root", lambda start=None: None)
    with pytest.raises(UpdateError):
        updater.perform_update(Install(kind="source", location="x"))


def test_a_source_upgrade_reinstalls_after_pull(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    monkeypatch.setattr(updater, "_run", fake_run)

    updater.perform_update(Install(kind="source", location=str(tmp_path)))

    assert seen[0][:2] == ["git", "-C"]
    assert seen[0][-3:] == ["fetch", "origin", "main"]
    assert seen[1][-2:] == ["--ff-only", "origin/main"]
    assert seen[2][-3:] == ["pip", "install", "-e"] or "pip" in seen[2]


def test_a_source_upgrade_on_main_beta_switches_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    monkeypatch.setattr(updater, "_run", fake_run)

    updater.perform_update(Install(kind="source", location=str(tmp_path)), beta=True)

    joined = [" ".join(command) for command in seen]
    assert any(command.endswith("fetch origin beta") for command in joined)
    assert any("switch beta" in command for command in joined)
    assert any("merge --ff-only origin/beta" in command for command in joined)


def test_source_update_fallback_stashes_and_retries_the_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        # The first merge fails on a dirty tree; stash -> merge -> pop rescues it.
        if "merge" in command and not any("stash" in " ".join(c) for c in seen[:-1]):
            return subprocess.CompletedProcess(command, 1, "", "local changes would be lost")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    monkeypatch.setattr(updater, "_run", fake_run)

    output = updater.perform_update(Install(kind="source", location=str(tmp_path)))
    assert "ok" in output
    commands_run = [" ".join(c) for c in seen]
    assert any(c.endswith("stash") for c in commands_run)
    assert any("stash pop" in c for c in commands_run)


def test_a_diverged_source_checkout_explains_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        if "merge" in command:
            return subprocess.CompletedProcess(command, 1, "", "not possible to fast-forward")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    monkeypatch.setattr(updater, "_run", fake_run)

    with pytest.raises(UpdateError, match="diverged"):
        updater.perform_update(Install(kind="source", location=str(tmp_path)))


def test_a_failed_fetch_explains_itself(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        if "fetch" in command:
            return subprocess.CompletedProcess(command, 1, "", "could not resolve host")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    monkeypatch.setattr(updater, "_run", fake_run)

    with pytest.raises(UpdateError, match="Could not fetch main"):
        updater.perform_update(Install(kind="source", location=str(tmp_path)))


def test_pip_update_fallback_to_git_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        if command[-1] == "jaigent":
            return subprocess.CompletedProcess(
                command, 1, "", "No matching distribution found for jaigent"
            )
        return subprocess.CompletedProcess(
            command, 0, "Successfully installed jaigent from git", ""
        )

    monkeypatch.setattr(updater, "_run", fake_run)

    output = updater.perform_update(Install(kind="pip", location="x"))
    assert "from git" in output
    assert len(seen) == 2
    assert "git+https://github.com/jaime-gaming/jaigent.git" in seen[1][-1]


# ------------------------------------------------- proving the update worked


#: Two of these tests have to *execute* the stub they write. A `#!/bin/sh`
#: script cannot be run by subprocess on Windows, and a `.bat` written to a
#: temp directory did not come back through `--version` intact on the
#: windows-latest runner either, so those two are exercised on Linux and macOS
#: and skipped there. Everything that only classifies results runs everywhere,
#: against a table instead of a process.
needs_executable_stub = pytest.mark.skipif(
    os.name == "nt",
    reason="executing a stub from a temp directory is not reliable on Windows",
)


def script(body: str) -> str:
    """A fake `jaigent`, as Python source."""
    import textwrap

    return textwrap.dedent(body)


def stub(directory: Path, text: str, *, name: str = "jaigent") -> Path:
    """Write a fake `jaigent` where a PATH lookup will find it.

    Windows only resolves names listed in PATHEXT, so the file is `jaigent.bat`
    there and plain `jaigent` elsewhere. The contents only matter to the two
    tests marked `needs_executable_stub`; the rest never run it.
    """
    path = directory / f"{name}.bat" if os.name == "nt" else directory / name
    path.write_text(script(f'\nprint("{text}")\n'), encoding="utf-8")
    path.chmod(0o755)
    return path


def invoke(path: Path) -> list[str]:
    """How to start the stub. A .bat runs itself; anything else needs Python."""
    return [str(path)] if os.name == "nt" else [sys.executable, str(path)]


def fake_versions(monkeypatch: pytest.MonkeyPatch, table: dict[str, str | None]) -> None:
    """Answer `--version` from a table instead of starting a process.

    The classification below is what is under test, and starting real binaries
    to exercise it would make every case depend on the platform's idea of an
    executable. `version_of` itself is tested separately, for real.
    """

    def version_of(command: list[str]) -> str | None:
        return table.get(" ".join(command))

    monkeypatch.setattr(updater, "version_of", version_of)


def test_the_version_is_the_last_field_of_the_version_line() -> None:
    assert updater.parse_version_text("jaigent 0.5.3\n") == "0.5.3"
    assert updater.parse_version_text("") is None
    assert updater.parse_version_text("   \n") is None


@needs_executable_stub
def test_version_of_reads_a_real_process(tmp_path: Path) -> None:
    binary = stub(tmp_path, "jaigent 0.5.3")

    assert updater.version_of(invoke(binary)) == "0.5.3"


@needs_executable_stub
def test_version_of_a_broken_binary_is_none(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text(script("\nraise SystemExit(3)\n"), encoding="utf-8")

    assert updater.version_of(invoke(broken)) is None


def test_every_copy_on_the_path_is_found_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "old"
    second = tmp_path / "new"
    first.mkdir()
    second.mkdir()
    stub(first, "jaigent 0.5.2")
    stub(second, "jaigent 0.5.3")
    monkeypatch.setenv("PATH", f"{first}{os.pathsep}{second}")

    assert [path.parent.name for path in updater.candidate_paths()] == ["old", "new"]


def test_an_install_that_took_effect_is_reported_as_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = stub(tmp_path, f"jaigent {__version__}")
    monkeypatch.setenv("PATH", str(tmp_path))
    fake_versions(monkeypatch, {str(binary): __version__})
    monkeypatch.setattr(updater, "run_command", lambda install: [str(binary)])

    check = updater.verify_update(
        Install(kind="binary", location=str(binary)), expected=__version__
    )

    assert check.updated is True
    assert check.same_copy is True
    assert check.elsewhere is False
    assert check.shadowed is False
    assert check.reported == __version__


def test_reinstalling_the_copy_the_shell_runs_is_not_shadowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary install replaces exactly the file the shell starts.

    The reported version equals the one from before the upgrade, which only
    reads as shadowing when a *different* file answers to `jaigent`.
    """
    binary = stub(tmp_path, "jaigent 0.5.3")
    monkeypatch.setattr(updater, "__version__", "0.5.2")
    fake_versions(monkeypatch, {str(binary): "0.5.3"})
    monkeypatch.setattr(updater.shutil, "which", lambda name: str(binary))
    monkeypatch.setattr(updater, "candidate_paths", lambda: [binary])
    monkeypatch.setattr(updater, "run_command", lambda install: [str(binary)])

    check = updater.verify_update(Install(kind="binary", location=str(binary)), expected="0.5.3")

    assert check.updated is True
    assert check.same_copy is True
    assert check.elsewhere is False
    assert check.shadowed is False


def test_an_older_copy_on_the_path_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real "it says updated but nothing changed": two installs, one stale.

    A pip install is what makes it possible — the copy that was upgraded is
    reached through the interpreter, while the shell keeps starting a binary
    from somewhere else on PATH.
    """
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    stale = stub(old_dir, "jaigent 0.5.2")
    fresh = stub(new_dir, "jaigent 0.5.3")
    monkeypatch.setenv("PATH", f"{old_dir}{os.pathsep}{new_dir}")
    monkeypatch.setattr(updater, "__version__", "0.5.2")
    # Patch the lookup rather than relying on PATH: Windows resolves to a case
    # the test did not write, so a table keyed on `str(path)` would miss.
    monkeypatch.setattr(updater.shutil, "which", lambda name: str(stale))
    fresh_command = " ".join([sys.executable, str(fresh)])
    fake_versions(monkeypatch, {str(stale): "0.5.2", fresh_command: "0.5.3", str(fresh): "0.5.3"})
    # The upgrade replaced the copy in new_dir; the shell starts the other one.
    monkeypatch.setattr(updater, "run_command", lambda install: [sys.executable, str(fresh)])

    check = updater.verify_update(Install(kind="pip", location=str(new_dir)), expected="0.5.3")

    assert check.reported == "0.5.3", "the upgraded copy did move"
    assert check.updated is True
    assert check.same_copy is None, "a module command has no path to compare"
    assert check.elsewhere is False, "so nothing is provable by path"
    assert check.shadowed is True, "but the copy on PATH still reports 0.5.2"
    assert check.resolved == str(stale)
    assert "0.5.2" in check.shell_line()
    assert not check.other_lines(), "both copies are already accounted for"


def test_a_pip_run_with_nothing_newer_on_the_index(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """pip exits 0 and installs nothing when the index has no newer version."""
    installed = stub(tmp_path, f"jaigent {__version__}")
    monkeypatch.setenv("PATH", str(tmp_path))
    module_command = f"{sys.executable} -m jaigent"
    fake_versions(monkeypatch, {str(installed): __version__, module_command: __version__})
    monkeypatch.setattr(updater.shutil, "which", lambda name: str(installed))
    monkeypatch.setattr(updater, "run_command", lambda install: [sys.executable, "-m", "jaigent"])

    check = updater.verify_update(Install(kind="pip", location="/x"), expected="0.6.0")

    assert check.updated is False
    assert check.same_copy is None, "a module command has no path to compare"
    assert check.elsewhere is False, "so nothing is provable by path"
    assert check.shadowed is False, "and the copy on PATH reports the same version"
    assert check.own_location == "", "a module command has no path to name"
    assert check.error == ""


def test_an_upgrade_that_landed_but_a_stale_copy_the_shell_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update worked; the terminal will still start the old one."""
    stale = stub(tmp_path, "jaigent 0.5.2")
    fresh = stub(tmp_path, "jaigent 0.5.3", name="jaigent-new")
    fake_versions(monkeypatch, {str(stale): "0.5.2", str(fresh): "0.5.3"})
    monkeypatch.setattr(updater.shutil, "which", lambda name: str(stale))
    monkeypatch.setattr(updater, "candidate_paths", lambda: [stale, fresh])
    monkeypatch.setattr(updater, "run_command", lambda install: [str(fresh)])

    check = updater.verify_update(Install(kind="binary", location=str(fresh)), expected="0.5.3")

    assert check.updated is True
    assert check.same_copy is False
    assert check.elsewhere is True
    assert check.shadowed is True
    assert "0.5.2" in check.shell_line()
    assert check.other_lines() == [], "both copies are already accounted for"


def test_a_copy_that_reports_nothing_is_an_error_not_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = stub(tmp_path, "")
    monkeypatch.setenv("PATH", str(tmp_path))
    fake_versions(monkeypatch, {str(broken): None})
    monkeypatch.setattr(updater, "run_command", lambda install: [str(broken)])

    check = updater.verify_update(Install(kind="binary", location=str(broken)), expected="0.5.3")

    assert check.updated is False
    assert check.shadowed is False
    assert check.error


def test_a_pip_install_is_asked_through_this_interpreter() -> None:
    command = updater.run_command(Install(kind="pip", location="x"))

    assert command == [updater.sys.executable, "-m", "jaigent"]


# --------------------------------------------------------------- pipx, source


def test_pipx_falls_back_to_a_forced_reinstall(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        # `pipx upgrade` refuses an app that did not come from a registry.
        if command[:2] == ["pipx", "upgrade"]:
            return subprocess.CompletedProcess(command, 1, "", "not installed from a registry")
        return subprocess.CompletedProcess(command, 0, "installed", "")

    monkeypatch.setattr(updater, "_run", fake_run)

    updater.perform_update(Install(kind="pipx", location="x"))

    assert seen[1] == ["pipx", "install", "--force", "jaigent"]


def test_pipx_falls_back_to_git_when_the_package_is_not_on_pypi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        if command[:2] == ["pipx", "upgrade"] or command[-1] == "jaigent":
            return subprocess.CompletedProcess(command, 1, "", "No package found")
        return subprocess.CompletedProcess(command, 0, "installed from git", "")

    monkeypatch.setattr(updater, "_run", fake_run)

    output = updater.perform_update(Install(kind="pipx", location="x"))

    assert "from git" in output
    assert seen[-1] == ["pipx", "install", "--force", f"git+{updater.REPO_URL}.git"]


def test_a_source_checkout_on_a_feature_branch_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`git pull --ff-only` exits 0 there and updates nothing."""
    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "arena/some-session")
    monkeypatch.setattr(
        updater,
        "_run",
        lambda *a, **k: pytest.fail("nothing should be run on the wrong branch"),
    )

    with pytest.raises(UpdateError, match="arena/some-session"):
        updater.perform_update(Install(kind="source", location=str(tmp_path)))


def test_a_source_checkout_on_main_is_updated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")
    monkeypatch.setattr(
        updater,
        "_run",
        lambda command, timeout=600.0: subprocess.CompletedProcess(
            command, 0, "Already up to date", ""
        ),
    )

    assert "Already up to date" in updater.perform_update(
        Install(kind="source", location=str(tmp_path))
    )


# ------------------------------------------- where an update actually installs


def test_a_binary_update_replaces_the_binary_that_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both installers default to their own directory, not to this one.

    Without JAIGENT_BIN_DIR an update installs to ~/.local/bin while the shell
    keeps running the binary it started, which is the update that reports
    success and changes nothing.
    """
    seen: dict[str, str | None] = {}

    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        seen["bin_dir"] = os.environ.get("JAIGENT_BIN_DIR")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(updater, "_run", fake_run)
    monkeypatch.setattr(updater.platform, "system", lambda: "Linux")
    install = Install(
        kind="binary", location="/opt/jaigent/bin/jaigent", bin_dir="/opt/jaigent/bin"
    )

    updater.perform_update(install)

    assert seen["bin_dir"] == "/opt/jaigent/bin"


def test_the_bin_dir_override_does_not_leak_into_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JAIGENT_BIN_DIR", raising=False)
    monkeypatch.setattr(
        updater,
        "_run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "ok", ""),
    )
    install = Install(
        kind="binary", location="/opt/jaigent/bin/jaigent", bin_dir="/opt/jaigent/bin"
    )

    updater.perform_update(install)

    assert "JAIGENT_BIN_DIR" not in os.environ


def test_a_frozen_binary_records_its_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Under tmp_path rather than a literal "/opt/...": Path.resolve() turns the
    # latter into "D:\opt\..." on Windows, where that assertion was simply wrong.
    binary = tmp_path / "jaigent"
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(binary), raising=False)

    install = detect_install()

    assert install.location == str(binary.resolve())
    assert install.bin_dir == str(binary.resolve().parent)


def test_a_failed_editable_reinstall_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`git pull` succeeded, so the tree is new and the import is still old."""
    monkeypatch.setattr(updater, "find_source_root", lambda start=None: tmp_path)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "main")

    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        if "-e" in command:
            return subprocess.CompletedProcess(
                command, 1, "", "error: externally-managed-environment"
            )
        return subprocess.CompletedProcess(command, 0, "Already up to date.", "")

    monkeypatch.setattr(updater, "_run", fake_run)

    with pytest.raises(UpdateError, match="externally-managed-environment"):
        updater.perform_update(Install(kind="source", location=str(tmp_path)))


def test_pipx_is_run_through_this_interpreter_when_it_is_installed_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updater.importlib.util, "find_spec", lambda name: object())

    command = upgrade_command(Install(kind="pipx", location="x"))

    assert command == [updater.sys.executable, "-m", "pipx", "upgrade", "jaigent"]


def test_pipx_falls_back_to_the_path_when_it_is_not_a_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updater.importlib.util, "find_spec", lambda name: None)

    assert upgrade_command(Install(kind="pipx", location="x")) == ["pipx", "upgrade", "jaigent"]


def test_a_missing_pipx_module_falls_back_to_the_one_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(updater.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(updater, "_run", fake_run)
    original = updater._run

    def failing_first(command: list[str], timeout: float = 600.0):  # noqa: ANN202
        if not seen:
            seen.append(command)
            raise FileNotFoundError("No module named pipx")
        return original(command)

    monkeypatch.setattr(updater, "_run", failing_first)

    updater.perform_update(Install(kind="pipx", location="x"))

    assert seen[0][:2] == [updater.sys.executable, "-m"]
    assert seen[1][0] == "pipx"
