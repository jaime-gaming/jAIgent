"""Send feedback to the maintainers as a GitHub issue.

jAIgent has no telemetry and no backend, so feedback travels as a public
GitHub issue. :func:`deliver` prefers the ``gh`` CLI when it is installed
and authenticated, and otherwise hands back a pre-filled issue URL for the
browser — headless terminals print it so it can be opened elsewhere.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import urllib.parse
import webbrowser
from dataclasses import dataclass

from jaigent import __version__
from jaigent.updater import REPO

ISSUES_URL = f"https://github.com/{REPO}/issues/new"

#: `gh` subprocesses must never hang the command: the auth check can hit
#: the network on some setups.
_GH_TIMEOUT = 10.0


def environment_footer() -> str:
    """One line of context appended to every report. No secrets, ever."""
    system = f"{platform.system()} {platform.release()}".strip()
    return f"jaigent {__version__} · python {platform.python_version()} · {system}"


def issue_title(message: str) -> str:
    """``[feedback] <first line>``, trimmed to something a list can show."""
    first_line = (message or "").strip().splitlines()[0] if message.strip() else ""
    if len(first_line) > 80:
        first_line = first_line[:77].rstrip() + "..."
    return f"[feedback] {first_line}" if first_line else "[feedback]"


def issue_body(message: str) -> str:
    """The report text plus the environment footer."""
    return f"{message.strip()}\n\n---\n{environment_footer()}\n"


def issue_url(message: str) -> str:
    """A pre-filled "new issue" form for ``message``."""
    query = urllib.parse.urlencode({"title": issue_title(message), "body": issue_body(message)})
    return f"{ISSUES_URL}?{query}"


def gh_available() -> bool:
    """Whether ``gh`` is installed *and* logged in."""
    if shutil.which("gh") is None:
        return False
    try:
        completed = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            timeout=_GH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def create_issue_via_gh(title: str, body: str) -> str | None:
    """File the issue with ``gh``. Returns its URL, or ``None`` on failure."""
    try:
        completed = subprocess.run(
            ["gh", "issue", "create", "--repo", REPO, "--title", title, "--body", body],
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    for line in reversed((completed.stdout or "").strip().splitlines()):
        if line.startswith("http"):
            return line
    return None


@dataclass(slots=True)
class Delivery:
    """Where the feedback went."""

    url: str
    #: ``"gh"`` when the issue was filed directly, ``"browser"`` when the
    #: user finishes the job from a pre-filled form.
    method: str
    #: Browser path only: whether a browser was actually launched.
    opened: bool = False


def deliver(message: str, *, open_browser: bool = True) -> Delivery:
    """Send ``message`` to the maintainers.

    Files the issue directly when ``gh`` can, otherwise returns a pre-filled
    form URL and opens it unless ``open_browser`` is false. Never raises for
    delivery problems: the worst case is a URL the user opens by hand.
    """
    title = issue_title(message)
    body = issue_body(message)
    if gh_available():
        created = create_issue_via_gh(title, body)
        if created is not None:
            return Delivery(url=created, method="gh")
    url = issue_url(message)
    opened = False
    if open_browser:
        try:
            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001 - the URL below is the fallback
            opened = False
    return Delivery(url=url, method="browser", opened=opened)
