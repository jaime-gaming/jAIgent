"""Checking for, and installing, new versions of jAIgent.

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
Guessing wrong would either fail confusingly or, worse, leave two jAIgents on
the PATH.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import platform
import re
import shutil
import ssl
import subprocess  # noqa: S404 - used to run pip/installers, never shell input
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jaigent import __version__
from jaigent.errors import JaigentError
from jaigent.paths import user_home

#: Where the release list lives.
REPO = "jaime-gaming/jaigent"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
COMMITS_URL = f"https://api.github.com/repos/{REPO}/commits/main"
BETA_COMMITS_URL = f"https://api.github.com/repos/{REPO}/commits/beta"
REPO_URL = f"https://github.com/{REPO}"
BETA_BRANCH = "beta"

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

    Pre-release suffixes are dropped: ``1.2.3rc1`` sorts as ``1.2.3``. jAIgent
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


def _padded(parts: tuple[int, ...], length: int) -> tuple[int, ...]:
    """Pad with zeros so ``1.0`` and ``1.0.0`` compare equal."""
    if len(parts) >= length:
        return parts
    return parts + (0,) * (length - len(parts))


def is_newer(candidate: str, current: str) -> bool:
    """Whether ``candidate`` is a later version than ``current``."""
    left, right = parse_version(candidate), parse_version(current)
    length = max(len(left), len(right), 3)
    return _padded(left, length) > _padded(right, length)


# ----------------------------------------------------------------------
# How was this copy installed?
# ----------------------------------------------------------------------
@dataclass(slots=True)
class Install:
    """How this copy of jAIgent got here, and how to upgrade it."""

    #: One of "binary", "pip", "pipx", "source".
    kind: str
    #: Human-readable location.
    location: str
    #: For a standalone binary, the directory it lives in. The installers read
    #: ``JAIGENT_BIN_DIR``, so an update can be told to replace *this* binary
    #: rather than whatever their default happens to be.
    bin_dir: str = ""

    @property
    def upgradable(self) -> bool:
        """Whether ``jaigent update`` can do this automatically."""
        return self.kind in {"binary", "pip", "pipx", "source"}

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
        executable = Path(sys.executable).resolve()
        return Install(kind="binary", location=str(executable), bin_dir=str(executable.parent))

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


@dataclass(slots=True)
class FetchResult:
    """What asking GitHub for the latest release found.

    The passive check only cares about ``release``, but the ``update`` command
    owes the user an accurate explanation: \"no releases published yet\" calls
    for a very different next step than \"you appear to be offline\".
    """

    release: Release | None = None
    #: One of "ok", "no-releases", "rate-limited", "unreachable".
    reason: str = "unreachable"
    #: Extra detail for the message, e.g. an HTTP status.
    detail: str = ""


def _github_headers() -> dict[str, str]:
    """GitHub rejects requests with no User-Agent; identify this client."""
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"jAIgent/{__version__} (+https://github.com/{REPO})",
    }


def _github_get(url: str, *, timeout: float):
    """Fetch a GitHub URL with the platform trust store.

    ``httpx`` normally uses the bundled ``certifi`` store. That is usually
    correct, but it breaks on machines whose corporate proxy (or Linux image)
    installs its CA into the operating-system store instead. ``curl`` and the
    GitHub CLI then work while ``jaigent update`` incorrectly reports that
    GitHub is unreachable. Use Python's platform store here so the update
    command behaves like the rest of the user's system without weakening TLS.
    """
    import httpx

    return httpx.get(
        url,
        timeout=timeout,
        headers=_github_headers(),
        follow_redirects=True,
        verify=ssl.create_default_context(),
    )


def _rate_limited(status: int, response: Any) -> bool:
    """Whether an HTTP failure is GitHub's rate limit rather than a real error."""
    if status not in (403, 429):
        return False
    try:
        headers = getattr(response, "headers", {}) or {}
        if str(headers.get("x-ratelimit-remaining", "")).strip() == "0":
            return True
        body = str(getattr(response, "text", "") or "").lower()
    except Exception:  # noqa: BLE001 - header reading must never raise
        return status == 429
    return "rate limit" in body or "rate_limit" in body or status == 429


def fetch_latest_detailed(timeout: float = FETCH_TIMEOUT) -> FetchResult:
    """Ask GitHub for the newest release, explaining failures instead of hiding them.

    Every failure mode returns a result rather than raising: a version check
    is never important enough to interrupt what the user was doing, but the
    ``update`` command uses ``reason`` to say what actually went wrong.
    """
    import httpx

    try:
        response = _github_get(RELEASES_URL, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        # 404 means "no release yet" — not a network error.
        if status == 404:
            return FetchResult(reason="no-releases")
        if _rate_limited(status, exc.response):
            return FetchResult(reason="rate-limited")
        return FetchResult(reason="unreachable", detail=f"HTTP {status}")
    except Exception:  # noqa: BLE001 - deliberately total; see the docstring
        return FetchResult(reason="unreachable")

    if not isinstance(data, dict):
        return FetchResult(reason="unreachable", detail="unexpected response")
    raw = data.get("tag_name")
    tag = str(raw).strip() if raw is not None else ""
    if not tag:
        return FetchResult(reason="unreachable", detail="unexpected response")

    return FetchResult(
        release=Release(
            version=tag.lstrip("vV"),
            url=str(data.get("html_url") or f"https://github.com/{REPO}/releases"),
            notes=str(data.get("body") or ""),
            published=str(data.get("published_at") or ""),
        ),
        reason="ok",
    )


def fetch_latest(timeout: float = FETCH_TIMEOUT) -> Release | None:
    """Ask GitHub for the newest release, or ``None`` if that fails.

    Every failure mode — offline, DNS, rate limit, malformed JSON, no releases
    yet — returns ``None`` rather than raising. A version check is never
    important enough to interrupt what the user was doing.
    """
    return fetch_latest_detailed(timeout=timeout).release


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
    raw_last = _read_state().get("last_check", 0.0)
    try:
        last = float(raw_last)
    except (TypeError, ValueError):
        # A hand-edited or interrupted cache should behave like a first run,
        # not make every CLI command crash while checking for updates.
        last = 0.0
    current = time.time() if now is None else now
    return current - last >= CHECK_INTERVAL


def record_check(release: Release | None, now: float | None = None) -> None:
    """Remember that a check happened, and what it found.

    When the check failed (``release is None``), the previously cached
    ``latest`` version is preserved so a transient network issue never
    hides a known update from the user.
    """
    state = _read_state()
    state["last_check"] = time.time() if now is None else now
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
        f"jAIgent {latest} is available (you have {__version__}). Run `jaigent update` to upgrade."
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
# Source checkout vs the GitHub repo
# ----------------------------------------------------------------------
@dataclass(slots=True)
class SourceSync:
    """How this working tree compares to ``origin`` / the update channel.

    Version tags can match while the tree is still behind (or dirty). The
    update command reports that immediately instead of saying "up to date".

    ``ahead`` / ``behind`` count commits each side has that the other lacks.
    They distinguish \"there is an update\" (behind) from \"pulling would do
    nothing\" (ahead only) — offering a pull for the latter used to end in a
    confusing \"nothing changed\" failure.
    """

    local_sha: str = ""
    remote_sha: str = ""
    dirty: bool = False
    root: str = ""
    error: str = ""
    #: The local branch, e.g. "main", "beta", or a feature branch.
    branch: str = ""
    #: The channel compared against: "main" or "beta".
    channel: str = "main"
    #: Commits only the local tree has, or ``None`` when that is unknown.
    ahead: int | None = None
    #: Commits only the remote has, or ``None`` when that is unknown.
    behind: int | None = None

    @property
    def available(self) -> bool:
        return bool(self.local_sha)

    @property
    def synced(self) -> bool:
        return bool(self.local_sha and self.remote_sha and self.local_sha == self.remote_sha)

    @property
    def update_available(self) -> bool:
        """Whether pulling the channel would actually bring new commits."""
        if not self.available or not self.remote_sha or self.synced:
            return False
        if self.behind is not None:
            return self.behind > 0
        # The commit counts are unknown (the fetch failed), so fall back to
        # comparing SHAs: different means *something* changed remotely.
        return True

    @property
    def ahead_only(self) -> bool:
        """Whether the tree only has commits the channel lacks.

        Pulling then says \"Already up to date\" and changes nothing, so the
        update command reports this instead of offering a useless pull.
        """
        return (
            self.available
            and bool(self.remote_sha)
            and not self.synced
            and (self.behind is not None and self.behind == 0)
        )

    def summary_cap(self) -> str:
        """The summary as a sentence: "Source is 2 commits ahead …"."""
        text = self.summary()
        return text[:1].upper() + text[1:] if text else text

    def summary(self) -> str:
        if self.error and not self.local_sha:
            return self.error
        local = self.local_sha[:7] or "?"
        remote = self.remote_sha[:7] or "?"
        if self.synced and not self.dirty:
            return f"source matches {self.channel} ({local})"
        if self.synced and self.dirty:
            return f"source matches {self.channel} ({local}) but the working tree has local changes"
        if self.local_sha and self.remote_sha and not self.synced:
            extra = ", and the working tree has local changes" if self.dirty else ""
            behind, ahead = self.behind, self.ahead
            if behind is not None and behind > 0 and (ahead or 0) == 0:
                plural = "s" if behind != 1 else ""
                return (
                    f"source is {behind} commit{plural} behind {self.channel}"
                    f" — update available{extra}"
                )
            if ahead is not None and ahead > 0 and (behind or 0) == 0:
                plural = "s" if ahead != 1 else ""
                return (
                    f"source is {ahead} commit{plural} ahead of {self.channel} "
                    f"— nothing to pull{extra}"
                )
            if behind is not None and ahead is not None and behind > 0 and ahead > 0:
                return (
                    f"source has diverged from {self.channel} "
                    f"({behind} behind, {ahead} ahead){extra}"
                )
            return (
                f"source is not synced with {self.channel} (local {local}, remote {remote}){extra}"
            )
        if self.error:
            return f"source {local}; {self.error}"
        return f"source {local}"


def _is_jaigent_project(directory: Path) -> bool:
    """Whether ``directory`` looks like the jAIgent checkout itself.

    A bare ``.git`` + ``pyproject.toml`` test also matches the user's own
    projects — a pip install inside a venv in one used to report that
    project's commits as jAIgent's source being "behind main".
    """
    manifest = directory / "pyproject.toml"
    if not manifest.is_file():
        return False
    if (directory / "src" / "jaigent" / "__init__.py").is_file():
        return True
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r'^name\s*=\s*["\']jaigent["\']', text, re.MULTILINE) is not None


def find_source_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for a jAIgent git checkout."""
    here = (Path(start) if start is not None else Path(__file__)).resolve()
    if not here.is_dir():
        here = here.parent
    for directory in [here, *here.parents]:
        if (directory / ".git").exists() and _is_jaigent_project(directory):
            return directory
    return None


def _git(*args: str, cwd: Path, timeout: float = 8.0) -> str | None:
    """Run a git command and return stdout, or ``None`` on any failure.

    stdin is closed so git can never stop to ask for credentials on the
    terminal; a fetch that needs them fails instead of hanging the command.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _ahead_behind(root: Path, local: str, remote: str) -> tuple[int | None, int | None]:
    """How many commits each side has that the other lacks, or ``(None, None)``.

    Works on SHAs, so no branch needs to be checked out — but both objects
    must exist locally, which is why the caller fetches first.
    """
    raw = _git("rev-list", "--left-right", "--count", f"{local}...{remote}", cwd=root)
    if not raw:
        return None, None
    try:
        left, right = raw.split()
        return int(left), int(right)
    except ValueError:
        return None, None


def beta_enabled() -> bool:
    """Whether the user opted into the beta channel."""
    raw = os.getenv("JAIGENT_BETA", "")
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    try:
        from jaigent.settings_store import load_layers

        return bool(load_layers().get("beta"))
    except Exception:  # noqa: BLE001 - a settings glitch must not break updates
        return False


def channel_name(*, beta: bool | None = None) -> str:
    """``beta`` or ``main``, the branch updates pull from."""
    if beta is None:
        beta = beta_enabled()
    return BETA_BRANCH if beta else "main"


def fetch_branch_sha(branch: str, timeout: float = FETCH_TIMEOUT) -> tuple[str | None, str]:
    """The current commit on GitHub for ``branch``, plus why it may be missing.

    Returns ``(sha, reason)`` where reason is one of "ok", "no-branch" (the
    branch does not exist on GitHub — GitHub answers 404 or 422 here) or
    "unreachable". A sync check must never raise, but it should still tell
    "the beta branch was deleted" apart from "you are offline".
    """
    import httpx

    url = BETA_COMMITS_URL if branch == BETA_BRANCH else COMMITS_URL
    if branch not in {"main", BETA_BRANCH}:
        url = f"https://api.github.com/repos/{REPO}/commits/{branch}"
    try:
        response = _github_get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 422):
            return None, "no-branch"
        return None, "unreachable"
    except Exception:  # noqa: BLE001 - a sync check must never raise
        return None, "unreachable"
    if not isinstance(data, dict):
        return None, "unreachable"
    sha = data.get("sha")
    return (str(sha), "ok") if sha else (None, "unreachable")


def fetch_main_sha(timeout: float = FETCH_TIMEOUT, *, branch: str | None = None) -> str | None:
    """The current commit on GitHub for ``branch`` (default ``main``)."""
    sha, _ = fetch_branch_sha(branch or "main", timeout)
    return sha


def inspect_source(
    *,
    start: Path | None = None,
    timeout: float = FETCH_TIMEOUT,
    fetch_remote: bool = True,
    branch: str | None = None,
) -> SourceSync:
    """Compare this checkout to the ``branch`` on GitHub (default: the channel).

    One HTTP GET plus local git. ``origin`` is fetched first so the
    ahead/behind counts are real rather than guessed from SHAs; a fetch only
    moves remote-tracking refs, never the working tree.
    """
    channel = branch or channel_name()
    install = detect_install()
    search_start = start or (Path(install.location) if install.location else None)
    root = find_source_root(search_start)
    if root is None:
        return SourceSync()
    local = _git("rev-parse", "HEAD", cwd=root)
    if not local:
        return SourceSync(error="not a git checkout", root=str(root))
    status = _git("status", "--porcelain", cwd=root)
    dirty = bool(status)
    current = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root) or ""
    ahead: int | None = None
    behind: int | None = None
    remote = ""
    error = ""
    if fetch_remote:
        found, reason = fetch_branch_sha(channel, timeout)
        remote = found or ""
        if not remote:
            if reason == "no-branch":
                error = f"no {channel!r} branch on github.com"
            else:
                error = "could not reach github.com"
        elif remote != local:
            # Fetch so both commits exist locally for the comparison. When it
            # fails (offline, or a remote that needs credentials), the counts
            # stay unknown and the SHAs are compared instead.
            _git("fetch", "--quiet", "origin", channel, cwd=root, timeout=timeout)
            ahead, behind = _ahead_behind(root, local, remote)
    return SourceSync(
        local_sha=local,
        remote_sha=remote or "",
        dirty=dirty,
        root=str(root),
        error=error,
        branch=current,
        channel=channel,
        ahead=ahead,
        behind=behind,
    )


# ----------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------
@contextlib.contextmanager
def _environment(environment: dict[str, str]):  # noqa: ANN201
    """Set the environment an upgrade command inherits, then put it back."""
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _run(command: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    """Run an upgrade command. The argument list is built here, never by a user."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def pipx_command() -> list[str]:
    """How to start pipx.

    Through this interpreter when it is installed here, because the ``pipx`` on
    PATH may belong to a different Python than the one running the app it is
    about to upgrade.
    """
    if importlib.util.find_spec("pipx") is not None:
        return [sys.executable, "-m", "pipx"]
    return ["pipx"]


def source_update_steps(root: Path, channel: str) -> list[list[str]]:
    """Every git step that moves this checkout onto ``origin/<channel>``.

    A checkout already on the channel is fast-forwarded in place. One on the
    *other* channel (main vs beta) is switched over, which is what asking for
    that channel means. Anything else — a feature branch, a detached HEAD —
    is refused with instructions, because pulling there would report success
    while updating nothing.
    """
    current = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root) or ""
    if current == "HEAD":
        raise UpdateError(
            f"{root} has a detached HEAD, not a branch. Updating it would strand the "
            f"new commits where no branch points. Run `git -C {root} switch {channel}` "
            "first."
        )
    if current and current not in {"main", BETA_BRANCH}:
        raise UpdateError(
            f"{root} is on branch {current!r}, not {channel}. Updating there would leave "
            f"this checkout untouched. Run `git -C {root} switch {channel}` first, "
            "or finish your work there and update afterwards."
        )
    steps = [["git", "-C", str(root), "fetch", "origin", channel]]
    if current and current != channel:
        steps.append(["git", "-C", str(root), "switch", channel])
    steps.append(["git", "-C", str(root), "merge", "--ff-only", f"origin/{channel}"])
    return steps


def upgrade_command(install: Install, *, beta: bool | None = None) -> list[str]:
    """The command that upgrades this kind of install.

    For a source checkout this is the first step (the fetch); the full plan
    is :func:`source_update_steps` plus refreshing the editable install.
    """
    use_beta = beta_enabled() if beta is None else beta
    if install.kind == "pip":
        if use_beta:
            return [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                f"git+{REPO_URL}.git@{BETA_BRANCH}",
            ]
        return [sys.executable, "-m", "pip", "install", "--upgrade", "jaigent"]
    if install.kind == "pipx":
        if use_beta:
            return [*pipx_command(), "install", "--force", f"git+{REPO_URL}.git@{BETA_BRANCH}"]
        return [*pipx_command(), "upgrade", "jaigent"]
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
    if install.kind == "source":
        root = find_source_root(Path(install.location) if install.location else None)
        if root is None:
            raise UpdateError(
                "Could not find git source repository to update. "
                "Run `pip install -e .` in your checkout."
            )
        return ["git", "-C", str(root), "fetch", "origin", channel_name(beta=use_beta)]
    raise UpdateError(
        f"Cannot upgrade a {install.kind!r} install automatically. "
        f"See https://github.com/{REPO}#install"
    )


def upgrade_summary(install: Install, *, beta: bool | None = None) -> str:
    """What ``jaigent update`` will actually run, for the confirmation prompt."""
    use_beta = beta_enabled() if beta is None else beta
    if install.kind == "source":
        root = find_source_root(Path(install.location) if install.location else None)
        if root is not None:
            channel = channel_name(beta=use_beta)
            try:
                steps = source_update_steps(root, channel)
            except UpdateError:
                # The refusal is raised again when it actually runs; show the
                # fetch so the prompt still says something truthful.
                steps = [["git", "-C", str(root), "fetch", "origin", channel]]
            steps.append([sys.executable, "-m", "pip", "install", "-e", str(root)])
            return " && ".join(" ".join(step) for step in steps)
    return " ".join(upgrade_command(install, beta=use_beta))


def _source_step_error(step: list[str], channel: str, detail: str, root: Path) -> str:
    """A friendly explanation for a failed source-update step, with a next action."""
    verb = step[3] if len(step) > 3 else ""
    tail = detail[-800:] if detail else "no further detail"
    if verb == "fetch":
        if "couldn't find remote ref" in detail or "remote ref" in detail:
            return (
                f"There is no {channel!r} branch on origin to update from.\n"
                f"{tail}\n"
                "If you own this repository, create it first "
                f"(`git push origin HEAD:{channel}`); otherwise use --stable."
            )
        return (
            f"Could not fetch {channel} from origin:\n{tail}\n"
            "Check your network connection, then try again."
        )
    if verb == "switch":
        return (
            f"Could not switch to {channel}:\n{tail}\n"
            f"Uncommitted changes may be in the way — stash or commit them "
            f"first (`git -C {root} stash`)."
        )
    if verb == "merge":
        return (
            f"This checkout has diverged from {channel} and cannot be fast-forwarded:\n"
            f"{tail}\n"
            f"Rebase it yourself (`git -C {root} pull --rebase`), or reset to the "
            f"channel (`git -C {root} reset --hard origin/{channel}`), then run "
            "`jaigent update` again."
        )
    return f"The upgrade failed:\n{tail}"


def _retry_merge_after_stash(root: Path, step: list[str]) -> subprocess.CompletedProcess[str]:
    """Stash local changes, retry the merge, and always restore the stash.

    Rescues the common case (a dirty tree blocking a fast-forward) without
    ever rewriting history. The stash is popped even when the retry fails, so
    the user's work is never left sitting in the stash.
    """
    _run(["git", "-C", str(root), "stash"])
    try:
        return _run(step)
    finally:
        _run(["git", "-C", str(root), "stash", "pop"])


def _update_source(channel: str, location: str) -> str:
    """Bring a source checkout onto ``origin/<channel>`` and reinstall it."""
    root = find_source_root(Path(location) if location else None)
    if root is None:
        raise UpdateError(
            "Could not find git source repository to update. "
            "Run `pip install -e .` in your checkout."
        )
    steps = source_update_steps(root, channel)
    outputs: list[str] = []
    for step in steps:
        try:
            completed = _run(step)
        except FileNotFoundError as exc:
            raise UpdateError(f"Could not run 'git': is git installed? ({exc})") from exc
        except subprocess.TimeoutExpired as exc:
            raise UpdateError("The upgrade timed out.") from exc
        if completed.returncode != 0:
            if len(step) > 3 and step[3] == "merge":
                try:
                    retried = _retry_merge_after_stash(root, step)
                except subprocess.TimeoutExpired as exc:
                    raise UpdateError("The upgrade timed out.") from exc
                if retried.returncode == 0:
                    outputs.append((retried.stdout or "").strip())
                    continue
                completed = retried
            detail = (completed.stderr or completed.stdout or "").strip()
            raise UpdateError(_source_step_error(step, channel, detail, root))
        outputs.append((completed.stdout or "").strip())

    # The tree is new but Python still imports the old code until the editable
    # install is refreshed, so a failed refresh is a failed update: the tree
    # is new and the import is old, which is worse than either on its own and
    # used to be reported as "Updated successfully".
    try:
        reinstall = _run([sys.executable, "-m", "pip", "install", "-e", str(root)])
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"The reinstall step could not run to completion: {exc}") from exc
    if reinstall.returncode != 0:
        detail = (reinstall.stderr or reinstall.stdout or "").strip()
        raise UpdateError(
            "The checkout is updated but `pip install -e .` failed, so Python is "
            f"still importing the old code:\n{detail[-800:]}"
        )
    outputs.append((reinstall.stdout or "").strip())
    return "\n".join(part for part in outputs if part)


def perform_update(install: Install | None = None, *, beta: bool | None = None) -> str:
    """Upgrade this installation in place with resilient fallbacks. Returns output.

    Raises:
        UpdateError: if the install kind cannot be upgraded automatically, or
            the upgrade command fails after all fallbacks.
    """
    install = install or detect_install()
    use_beta = beta_enabled() if beta is None else beta

    # A source checkout updates in several git steps (fetch, maybe switch,
    # fast-forward, reinstall), not one command — and on a feature branch it
    # refuses outright, because `git pull` there says "Already up to date",
    # exits 0, and the release code never arrives.
    if install.kind == "source":
        return _update_source(channel_name(beta=use_beta), install.location)

    command = upgrade_command(install, beta=use_beta)

    # Both installers take JAIGENT_BIN_DIR. Without it a binary update installs
    # to their default (~/.local/bin, %LOCALAPPDATA%) which is not necessarily
    # the directory this binary came from — the update would "succeed" and the
    # shell would keep running the old file.
    environment = dict(os.environ)
    if install.kind == "binary" and install.bin_dir:
        environment["JAIGENT_BIN_DIR"] = install.bin_dir

    try:
        with _environment(environment):
            completed = _run(command)
    except FileNotFoundError as exc:
        if install.kind == "pipx" and command[:1] == [sys.executable]:
            # pipx is not importable here after all; try the one on PATH.
            with _environment(environment):
                completed = _run(["pipx", *command[3:]])
        else:
            raise UpdateError(f"Could not run {command[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("The upgrade timed out.") from exc

    try:
        if completed.returncode != 0 and install.kind == "pip":
            completed = _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    f"git+{REPO_URL}.git@{BETA_BRANCH}" if use_beta else f"git+{REPO_URL}.git",
                ]
            )

        # `pipx upgrade` only works for an app installed from a registry; one that
        # came from a git URL is refused outright, and so is an app that is not on
        # PyPI yet. Reinstalling in place is the same outcome the user asked for.
        if completed.returncode != 0 and install.kind == "pipx":
            pipx = pipx_command()
            completed = _run([*pipx, "install", "--force", "jaigent"])
            if completed.returncode != 0:
                completed = _run(
                    [
                        *pipx,
                        "install",
                        "--force",
                        (
                            f"git+{REPO_URL}.git@{BETA_BRANCH}"
                            if use_beta
                            else f"git+{REPO_URL}.git"
                        ),
                    ]
                )
    except FileNotFoundError as exc:
        # A pipx install whose `pipx` left the PATH used to end in a traceback
        # here; the first command was guarded, the fallbacks were not.
        raise UpdateError(f"Could not run the upgrade command: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("The upgrade timed out.") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise UpdateError(f"The upgrade failed:\n{detail[-800:]}")

    return (completed.stdout or "").strip()


# ----------------------------------------------------------------------
# Proving the update took effect
# ----------------------------------------------------------------------
#: The console scripts this package installs.
SCRIPT_NAMES = ("jaigent", "jgt")

#: Long enough for a frozen binary to start, short enough to stay unnoticed.
VERIFY_TIMEOUT = 15.0

#: How many PATH entries to ask for their version. Each is a process start.
MAX_PATH_COPIES = 5


def same_path(left: str | None, right: str | None) -> bool:
    """Whether two path strings name the same file.

    Case-folded, because Windows and macOS both resolve paths in a case the
    caller did not necessarily write, and a comparison that misses would list
    the copy just upgraded as "another copy on PATH".
    """
    if not left or not right:
        return False
    return os.path.normcase(left) == os.path.normcase(right)


def candidate_paths() -> list[Path]:
    """Every ``jaigent`` on PATH, in the order the shell would find them."""
    names = [SCRIPT_NAMES[0]]
    if os.name == "nt":
        extensions = os.getenv("PATHEXT", ".EXE").split(os.pathsep)
        names += [f"{SCRIPT_NAMES[0]}{ext}" for ext in extensions]
    found: list[Path] = []
    for directory in (os.getenv("PATH") or "").split(os.pathsep):
        if not directory:
            continue
        for name in names:
            candidate = Path(directory, name)
            try:
                if candidate.is_file() and candidate not in found:
                    found.append(candidate)
            except OSError:
                continue
    return found


def version_of(command: list[str]) -> str | None:
    """``<command> --version`` reduced to just the version, or ``None``."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell input
            [*command, "--version"],
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_version_text(completed.stdout or completed.stderr)


def parse_version_text(text: str) -> str | None:
    """Pull the version out of ``"jaigent 0.5.3"``.

    The flag is argparse's ``action="version"``, which prints
    ``"jaigent <version>"`` and exits 0; the last field is the version.
    """
    fields = text.strip().split()
    return fields[-1] if fields else None


def run_command(install: Install) -> list[str]:
    """How to start the CLI that ``perform_update`` just replaced."""
    if install.kind == "binary":
        return [install.location or sys.executable]
    if install.kind == "pipx":
        return ["jaigent"]
    # pip, pipx-managed venvs and editable installs all provide the script, but
    # only this interpreter is guaranteed to have the package we just upgraded.
    return [sys.executable, "-m", "jaigent"]


@dataclass(slots=True)
class Verification:
    """What answers to ``jaigent`` after an upgrade.

    Every upgrade command exits 0 in situations that changed nothing — pip
    finding no newer version on the index, a git checkout that is already at
    the remote commit, a package that is not on PyPI at all and therefore
    silently falling back to a git install. Reporting success on the exit code
    alone is how "it says updated but nothing changed" happens.

    Two separate questions are answered, because they fail independently:
    did the copy we replaced actually change, and is that copy the one the
    shell will start.
    """

    #: The copy that was replaced, as a command list.
    command: list[str] = field(default_factory=list)
    #: What the replaced copy reports, if it answered.
    reported: str | None = None
    #: What it should report after a successful upgrade.
    expected: str = ""
    #: What the running process was, before the upgrade.
    before: str = ""
    #: The ``jaigent`` the shell would start, and what it reports.
    resolved: str | None = None
    resolved_version: str | None = None
    resolved_path: str | None = None
    #: The replaced copy's real path, when it was started as a file. A
    #: ``python -m jaigent`` command names a module, so there is no path.
    installed_path: str | None = None
    #: Other copies on PATH, as ``"path (version)"``.
    others: tuple[str, ...] = ()
    #: Why nothing could be determined, if that is what happened.
    error: str = ""

    @property
    def updated(self) -> bool:
        """The copy we replaced now reports the version we were aiming at."""
        return self.reported is not None and self.reported == self.expected

    @property
    def same_copy(self) -> bool | None:
        """Whether the shell starts the file we replaced, or ``None`` if unknown.

        Unknown is the interesting case: a pip install is upgraded in place and
        reached as ``python -m jaigent``, so there is no path to compare with
        the one the shell resolved.
        """
        if not self.resolved_path or not self.installed_path:
            return None
        return same_path(self.resolved_path, self.installed_path)

    @property
    def elsewhere(self) -> bool:
        """The shell provably starts a different file from the one replaced."""
        return self.same_copy is False

    @property
    def shadowed(self) -> bool:
        """A stale copy earlier on PATH is what the shell will run.

        This is the "it says updated but nothing changed" case: the upgrade
        landed in one copy and the terminal keeps starting another, so the
        version the user sees never moves.
        """
        if self.updated:
            # The copy we replaced is right, so the only way the user still
            # sees the old version is a different copy earlier on PATH. A
            # module command cannot be compared by path, so the version that
            # copy reports is the evidence instead.
            return self.resolved_version is not None and self.resolved_version != self.expected
        if self.reported != self.before or self.resolved_version is None:
            return False
        # Nothing was installed. The user keeps seeing the old version if the
        # copy on PATH reports something other than the one we just asked —
        # or if it reports the same thing and is provably a different file.
        return self.resolved_version != self.reported or self.same_copy is False

    @property
    def own_location(self) -> str:
        """The replaced copy as a path, or "" when it was reached as a module."""
        return self.installed_path or ""

    def line(self) -> str:
        """A one-line account of the copy that was replaced."""
        where = self.resolved or " ".join(self.command) or "?"
        if self.reported is None:
            return where
        return f"{self.reported} ({where})"

    def shell_line(self) -> str:
        """A one-line account of the copy the shell will start."""
        version = self.resolved_version or "no version"
        return f"{version} ({self.resolved})"

    def other_lines(self) -> list[str]:
        """The other copies on PATH, which is the usual reason for a stale one."""
        own = os.path.normcase(self.resolved or "")
        return [
            line
            for line in self.others
            if not own or not os.path.normcase(line).startswith(f"{own} ")
        ]


def verify_update(install: Install, *, expected: str) -> Verification:
    """Ask the installed CLI what version it is. Never raises."""
    verification = Verification(expected=expected, before=__version__)
    try:
        command = run_command(install)
        verification.command = command

        which = shutil.which(SCRIPT_NAMES[0])
        if which:
            verification.resolved = which
            with contextlib.suppress(OSError):
                verification.resolved_path = str(Path(which).resolve())

        # Only a command that starts a file has a path to compare with the one
        # the shell resolves; `python -m jaigent` names a module instead.
        if len(command) == 1:
            with contextlib.suppress(OSError):
                verification.installed_path = str(Path(command[0]).resolve())

        verification.reported = version_of(command)
        if verification.reported is None:
            verification.error = (
                f"`{' '.join(command)} --version` produced no version. "
                "The upgrade may still have worked."
            )

        # What the shell will actually start, which is not necessarily the
        # copy that was just replaced.
        if which:
            verification.resolved_version = version_of([which])

        # The copy a module command loads has no path of its own, but the
        # script the same package installed does: comparing against it keeps
        # the upgraded copy out of the "also on PATH" list.
        known = [verification.resolved_path, verification.installed_path, which]
        if verification.installed_path is None and command:
            known.append(str(Path(command[-1])))

        others: list[str] = []
        for path in candidate_paths()[:MAX_PATH_COPIES]:
            try:
                real = str(path.resolve())
            except OSError:
                real = str(path)
            if any(same_path(real, item) or same_path(str(path), item) for item in known):
                continue
            found = version_of([str(path)])
            others.append(f"{path} ({found or 'no version'})")
        verification.others = tuple(others)
    except Exception as exc:  # noqa: BLE001 - a check must never break an update
        verification.error = str(exc) or exc.__class__.__name__
    return verification
