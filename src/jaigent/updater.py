"""Checking for, and installing, new versions of jaigent.

Two rules shape everything here:

1. **A version check must never get in the way.** It runs at most once a day, in
   the background, with a short timeout, and any failure is swallowed. Being
   offline, behind a proxy, or rate-limited by GitHub must never slow down or
   break a command the user actually asked for.
2. **Nothing is installed without consent.** ``jaigent update`` is an explicit
   command. The passive check only ever prints one line telling you a release
   exists.

The install method is detected rather than assumed: a pip install is upgraded
with pip, a standalone binary is replaced by re-running the platform installer.
Guessing wrong would either fail confusingly or, worse, leave two jaigents on
the PATH.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import subprocess  # noqa: S404 - used to run pip/installers, never shell input
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jaigent import __version__
from jaigent.errors import JaigentError
from jaigent.paths import user_home

#: Where the release list lives.
REPO = "jaime-gaming/jaigent"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

#: Installer scripts used to replace a standalone binary.
INSTALL_SH = f"https://raw.githubusercontent.com/{REPO}/main/packaging/install.sh"
INSTALL_PS1 = f"https://raw.githubusercontent.com/{REPO}/main/packaging/install.ps1"

#: How long between passive checks. Once a day is enough to be useful without
#: being a nuisance, and keeps well clear of GitHub's unauthenticated limits.
CHECK_INTERVAL = 60 * 60 * 24

#: The passive check must never delay the command the user actually ran.
CHECK_TIMEOUT = 3.0

#: An explicit `jaigent update` can afford to wait a little longer.
FETCH_TIMEOUT = 15.0

#: Set any of these to skip the passive check entirely.
OPT_OUT_VARS = ("JAIGENT_NO_UPDATE_CHECK", "NO_UPDATE_NOTIFIER", "CI")


class UpdateError(JaigentError):
    """An update could not be completed."""


def state_path() -> Path:
    """Where the last-check timestamp is remembered."""
    return user_home() / "update-check.json"


# ----------------------------------------------------------------------
# Version comparison
# ----------------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    """Turn ``"v1.2.3"`` into ``(1, 2, 3)`` for comparison.

    Pre-release suffixes are dropped: ``1.2.3rc1`` sorts as ``1.2.3``. jaigent
    does not publish pre-releases, and treating one as newer than the final
    release would be worse than ignoring the suffix.
    """
    cleaned = text.strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def is_newer(candidate: str, current: str) -> bool:
    """Whether ``candidate`` is a later version than ``current``."""
    return parse_version(candidate) > parse_version(current)


# ----------------------------------------------------------------------
# How was this copy installed?
# ----------------------------------------------------------------------
@dataclass(slots=True)
class Install:
    """How this copy of jaigent got here, and how to upgrade it."""

    #: One of "binary", "pip", "pipx", "source".
    kind: str
    #: Human-readable location.
    location: str

    @property
    def upgradable(self) -> bool:
        """Whether ``jaigent update`` can do this automatically."""
        return self.kind in {"binary", "pip", "pipx"}

    def describe(self) -> str:
        return {
            "binary": "standalone binary",
            "pip": "pip install",
            "pipx": "pipx install",
            "source": "editable install from source",
        }.get(self.kind, self.kind)


def detect_install() -> Install:
    """Work out how this copy was installed.

    ``sys.frozen`` is set by PyInstaller and cx_Freeze, so a standalone binary
    identifies itself. Otherwise the package location tells us: a path under a
    pipx venv means pipx, an editable install points back at a source checkout.
    """
    if getattr(sys, "frozen", False):
        return Install(kind="binary", location=str(Path(sys.executable).resolve()))

    module = Path(__file__).resolve()
    location = str(module.parent)

    if "pipx" in module.parts:
        return Install(kind="pipx", location=location)

    # An editable install leaves the package in the working tree, next to the
    # project files, rather than in site-packages.
    if "site-packages" not in module.parts and "dist-packages" not in module.parts:
        return Install(kind="source", location=location)

    return Install(kind="pip", location=location)


# ----------------------------------------------------------------------
# Talking to GitHub
# ----------------------------------------------------------------------
@dataclass(slots=True)
class Release:
    """The latest published release."""

    version: str
    url: str
    notes: str = ""
    published: str = ""

    @property
    def is_newer(self) -> bool:
        return is_newer(self.version, __version__)


def fetch_latest(timeout: float = FETCH_TIMEOUT) -> Release | None:
    """Ask GitHub for the newest release, or ``None`` if that fails.

    Every failure mode — offline, DNS, rate limit, malformed JSON, no releases
    yet — returns ``None`` rather than raising. A version check is never
    important enough to interrupt what the user was doing.
    """
    import httpx

    try:
        response = httpx.get(
            RELEASES_URL,
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        # 404 means "no release yet" — not a network error.
        if exc.response.status_code == 404:
            return None
        return None
    except Exception:  # noqa: BLE001 - deliberately total; see the docstring
        return None

    if not isinstance(data, dict):
        return None
    raw = data.get("tag_name")
    tag = str(raw).strip() if raw is not None else ""
    if not tag:
        return None

    return Release(
        version=tag.lstrip("vV"),
        url=str(data.get("html_url") or f"https://github.com/{REPO}/releases"),
        notes=str(data.get("body") or ""),
        published=str(data.get("published_at") or ""),
    )


# ----------------------------------------------------------------------
# The once-a-day passive check
# ----------------------------------------------------------------------
def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict[str, Any]) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass  # a cache we cannot write is not worth an error


def checks_disabled() -> bool:
    """Whether the user (or CI) has opted out of update checks."""
    return any(os.getenv(name) for name in OPT_OUT_VARS)


def due_for_check(now: float | None = None) -> bool:
    """Whether enough time has passed since the last check."""
    if checks_disabled():
        return False
    last = float(_read_state().get("last_check", 0.0))
    return (now or time.time()) - last >= CHECK_INTERVAL


def record_check(release: Release | None, now: float | None = None) -> None:
    """Remember that a check happened, and what it found.

    When the check failed (``release is None``), the previously cached
    ``latest`` version is preserved so a transient network issue never
    hides a known update from the user.
    """
    state = _read_state()
    state["last_check"] = now or time.time()
    state["version"] = __version__
    if release is not None:
        state["latest"] = release.version
        state["url"] = release.url
    _write_state(state)


def cached_notice() -> str:
    """A one-line upgrade notice from the last check, if one is warranted.

    Reads only the cache, so it costs nothing and can run on every invocation.
    """
    state = _read_state()
    latest = str(state.get("latest") or "")
    if not latest or not is_newer(latest, __version__):
        return ""
    return (
        f"jaigent {latest} is available (you have {__version__}). Run `jaigent update` to upgrade."
    )


#: How long to let an in-flight check finish once the command is done.
#: A daemon thread is killed at interpreter exit, so without a brief join the
#: request is cancelled every time and the cache never gets written.
JOIN_TIMEOUT = 1.0


def check_in_background() -> threading.Thread | None:
    """Refresh the cached release info without blocking anything.

    The thread is a daemon so it can never hold the process open. Its result is
    only used on the *next* run, which is what makes the check free from the
    user's point of view.
    """
    if not due_for_check():
        return None

    def worker() -> None:
        # A background check must never surface an error to the user.
        with contextlib.suppress(Exception):
            record_check(fetch_latest(timeout=CHECK_TIMEOUT))

    thread = threading.Thread(target=worker, daemon=True, name="jaigent-update-check")
    thread.start()
    return thread


def finish_check(thread: threading.Thread | None, timeout: float = JOIN_TIMEOUT) -> None:
    """Give a background check a moment to land, then move on regardless.

    Bounded by ``timeout``: a slow network delays exit by at most that, and the
    daemon flag means even a hung request cannot prevent the process ending.
    """
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


# ----------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------
def _run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    """Run an upgrade command. The argument list is built here, never by a user."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def upgrade_command(install: Install) -> list[str]:
    """The command that upgrades this kind of install."""
    if install.kind == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", "jaigent"]
    if install.kind == "pipx":
        return ["pipx", "upgrade", "jaigent"]
    if install.kind == "binary":
        if platform.system() == "Windows":
            return [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f"irm {INSTALL_PS1} | iex",
            ]
        return ["sh", "-c", f"curl -fsSL {INSTALL_SH} | sh"]
    raise UpdateError(
        "This looks like an editable install from source. Upgrade it with:\n"
        "  git pull && pip install -e ."
    )


def perform_update(install: Install | None = None) -> str:
    """Upgrade this installation in place. Returns the command's output.

    Raises:
        UpdateError: if the install kind cannot be upgraded automatically, or
            the upgrade command fails.
    """
    install = install or detect_install()
    command = upgrade_command(install)

    try:
        completed = _run(command)
    except FileNotFoundError as exc:
        raise UpdateError(f"Could not run {command[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("The upgrade timed out.") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise UpdateError(f"The upgrade failed:\n{detail[-800:]}")

    return (completed.stdout or "").strip()
