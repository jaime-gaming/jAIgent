"""The local web panel: this same jAIgent, in a browser, on this machine.

``jaigent web`` serves the panel that ships in ``web/public`` over loopback and
the small API behind it. Nothing here is a second implementation: the panel
reads the same session store, the same checkpoints and the same settings the
terminal reads, and a turn started from the browser runs through the same
:class:`~jaigent.agent.Agent`, tools, sandbox and approval policy as one
started from ``jaigent chat``.

Two boundaries matter, and both are enforced here rather than left to the
caller:

* **Loopback only.** There is no ``--host 0.0.0.0``. A panel that shows your
  transcripts and can start a turn is not something to publish on the network,
  and a browser page from anywhere else is refused before the route is even
  looked at (``Host`` and ``Origin`` are both checked, so DNS rebinding does
  not get a free pass).
* **One pairing token per run.** It is printed by the CLI, carried in the
  address the browser is opened at, and required on every API request. It is
  never written to disk and never logged. Loopback is a boundary, not proof
  that whoever is asking is you: anything else running on this machine can
  reach this port.

Writing is the point. ``POST /api/chat`` runs a turn, and ``/api/file`` reads
and saves a file in the workspace, taking the same checkpoint the agent takes
so ``jaigent undo`` still reverts it. An ``ask`` policy is answered in the
browser that started the turn, which is the one place somebody is certainly
looking, so no combination of flags leaves a panel that can be opened but not
written to; ``--no-chat`` and ``--dry-run`` are how the writing is taken away
again, on purpose, by whoever starts it.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from stat import S_IMODE
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from rich.console import Console

from jaigent import __version__
from jaigent import session as sessions
from jaigent.agent import Agent, AgentResult
from jaigent.approval import Approver, Decision, Mode
from jaigent.approval import preview as approval_preview
from jaigent.checkpoint import Checkpoint, CheckpointStore
from jaigent.config import Settings
from jaigent.errors import ConfigurationError, SandboxViolation, ToolError
from jaigent.gateway import is_loopback_host
from jaigent.tools import build_default_registry
from jaigent.tools.sandbox import (
    line_ending_of,
    normalise_endings,
    refuse_if_blocked,
    refuse_if_writable,
    relative_to_workspace,
    resolve_in_workspace,
)

#: The panel's port. Not 8787, which is the gateway's, so the two can run
#: side by side without either stealing the other's address.
DEFAULT_PORT = 8765

#: Bytes of randomness in the pairing token, URL-safe.
TOKEN_BYTES = 24

#: How much of a listing one request may ask for.
MAX_LIST = 200
DEFAULT_LIST = 50

#: Longest prompt ``/api/chat`` accepts, and the largest body it reads. A
#: browser paste is a few thousand characters; anything past this is a client
#: treating the panel as a file drop.
MAX_PROMPT_CHARS = 32_000
MAX_BODY_BYTES = 64_000

#: Largest file the panel will read into an editor, and the longest text it will
#: write back. Both are well past any source file and well short of anything
#: that would make a browser tab or a checkpoint store struggle.
MAX_FILE_BYTES = 1_000_000
MAX_WRITE_CHARS = 1_000_000

#: Body cap for a write. The text plus its JSON quoting plus the path, with room
#: to spare: a body limit tighter than the text limit would refuse a file the
#: reader can see in the editor.
MAX_WRITE_BODY_BYTES = 4 * MAX_FILE_BYTES

#: Seconds a turn waits for a human to answer an approval in the browser before
#: it takes the safe answer, which is no. Long enough to read a diff and think,
#: short enough that a tab closed mid-turn does not leave a thread blocked for
#: the rest of the process's life.
APPROVAL_TIMEOUT = 600.0

#: How many conversations may hold "always allow this tool" at once. Losing one
#: only ever means a question is asked again, never that one is skipped, so the
#: cap costs a prompt and nothing else.
_MAX_ALWAYS_CONVERSATIONS = 64

#: How often a turn waiting for an approval says "still here". A blocked wait
#: cannot notice a closed tab, and the next event would only be written when the
#: answer came, which for a reader who left would be the timeout. The heartbeat
#: finds out in seconds instead, and keeps a proxy from dropping an idle stream.
APPROVAL_HEARTBEAT = 15.0

#: How much of one argument value the browser is shown. The diff already carries
#: the content that matters; this is the label on the button, not the file.
MAX_ARGUMENT_PREVIEW = 240

#: Content types, spelled out. ``mimetypes`` reads the platform's table, which
#: on a fresh Windows install does not know ``.webmanifest`` and guesses wrong
#: about ``.js``, and a wrong type is a panel that does not start.
CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}

#: Hosts a browser may name when it talks to this server.
_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "localhost.localdomain"})

#: The panel's policy, in pieces. ``default-src 'none'`` first, then the six
#: things a page of HTML, CSS and one module graph actually needs: its own
#: scripts and styles, images it draws itself, its own API. No inline anything,
#: no third-party origin, no frame ancestor.
_BASE_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'self' blob:; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'none'"
)

#: What is appended once, and only once, ``web/public/firebase-config.js`` holds
#: a real project: the SDK's origin, and the endpoints it calls. Until then the
#: panel may not load remote code at all, which is why the check is on the file
#: the server itself serves rather than on a flag someone could set.
_FIREBASE_CSP = (
    "; script-src-elem 'self' https://www.gstatic.com"
    "; connect-src 'self' https://firestore.googleapis.com https://*.firebaseio.com"
    " https://identitytoolkit.googleapis.com https://securetoken.googleapis.com"
    " https://*.cloudfunctions.net https://*.run.app wss://*.firebaseio.com"
)

#: The line a filled-in config stops containing. Deliberately a textual check on
#: the shipped placeholder: it cannot be tripped by a query parameter, an
#: environment variable or a header, only by editing the file.
_CONFIG_PLACEHOLDER = 'apiKey: ""'

_csp_cache: dict[str, tuple[float, int, bool]] = {}


def firebase_configured(root: Path | None = None) -> bool:
    """Whether ``firebase-config.js`` has been filled in.

    Args:
        root: The asset folder. Defaults to :func:`asset_dir`.

    The check is on the file the server itself serves, so it cannot be tripped by
    a header, a query parameter or an environment variable: only editing the file
    widens what the panel may load.
    """
    folder = root or asset_dir()
    if folder is None:
        return False
    config_file = folder / "firebase-config.js"
    try:
        stat = config_file.stat()
    except OSError:
        return False
    key = str(config_file)
    stamp = (stat.st_mtime, stat.st_size)
    cached = _csp_cache.get(key)
    if cached is not None and (cached[0], cached[1]) == stamp:
        return cached[2]
    try:
        text = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    filled = _CONFIG_PLACEHOLDER not in text
    _csp_cache[key] = (stamp[0], stamp[1], filled)
    return filled


def firebase_auth_summary(root: Path | None = None) -> dict[str, Any]:
    """What ``jaigent web login`` reports about the panel's Firebase sign-in.

    Args:
        root: The asset folder. Defaults to :func:`asset_dir`.

    Returns:
        A small, JSON-shaped summary: whether a project is configured at all,
        whether the sign-in gate is switched on, which providers it offers, and
        the project id and auth domain, so the CLI can point a browser at the
        hosted panel. Everything is read from the same ``firebase-config.js``
        the panel itself loads; a shipped, empty config reports ``configured``
        false and the caller explains that a local panel uses its token instead.

    The parse is deliberately light. The config is an ES module rather than
    JSON, but the fields this needs are simple ``key: "value"`` and
    ``key: true`` pairs, and reading them with a regular expression keeps the
    CLI free of a JavaScript parser for a file the reader wrote by hand.
    """
    folder = root or asset_dir()
    summary: dict[str, Any] = {
        "configured": False,
        "auth_enabled": False,
        "providers": {"google": False, "password": False},
        "project_id": "",
        "auth_domain": "",
    }
    if folder is None:
        return summary
    config_file = folder / "firebase-config.js"
    try:
        text = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return summary

    configured = firebase_configured(folder)
    summary["configured"] = configured

    # The file opens with a block comment showing what a filled config looks
    # like, complete with an example projectId. Read the code, not the docs:
    # strip block comments first so the example values are not mistaken for the
    # reader's. Line comments are left alone, since a URL's own "//" would be
    # caught by a naive line-comment strip.
    code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

    def field(name: str) -> str:
        match = re.search(rf'{name}:\s*"([^"]*)"', code)
        return match.group(1) if match else ""

    def flag(name: str, default: bool) -> bool:
        match = re.search(rf"{name}:\s*(true|false)", code)
        if match is None:
            return default
        return match.group(1) == "true"

    summary["project_id"] = field("projectId")
    summary["auth_domain"] = field("authDomain")
    # The gate only applies to a configured project: signing in needs somewhere
    # to sign in to, so an empty config means no login regardless of the flag.
    summary["auth_enabled"] = configured and flag("enabled", True)
    summary["providers"] = {
        "google": configured and flag("google", True),
        "password": configured and flag("password", True),
    }
    return summary


def content_security_policy(root: Path | None = None, frames: Iterable[str] = ()) -> str:
    """The policy this panel is served under, given what is configured.

    Args:
        root: The asset folder. Defaults to :func:`asset_dir`.
        frames: Origins allowed to frame the panel, from
            :attr:`WebConfig.allowed_frames`.

    Returns:
        The ``content-security-policy`` value. Firebase's origins are appended
        only when ``firebase-config.js`` has been filled in, and
        ``frame-ancestors`` names whoever was explicitly allowed to embed the
        panel instead of refusing everybody.
    """
    policy = _BASE_CSP
    if firebase_configured(root):
        policy += _FIREBASE_CSP
    named = tuple(parse_origins(frames))
    if named:
        ancestors = " ".join(("'self'", *named))
        policy = policy.replace("frame-ancestors 'none'", f"frame-ancestors {ancestors}")
    return policy


# ---------------------------------------------------------------------------
# Where the panel lives
# ---------------------------------------------------------------------------
def asset_dir() -> Path | None:
    """The folder holding the panel's files, or ``None`` if they did not ship.

    Looked for in two places: inside the installed package, where the wheel and
    the frozen binaries both put them, and in the checkout's own ``web/public``,
    which is what an editable install runs against. One copy, in the folder that
    gets deployed, and packaging maps it into the package.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here / "data" / "web", here.parents[1] / "web" / "public"):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def resolve_asset(root: Path, url_path: str) -> Path | None:
    """The file one URL path names, or ``None`` if it is not inside ``root``.

    The path is decoded, joined, resolved and then checked against the root, in
    that order: resolving first is what makes ``../../.env`` and a symlink
    planted inside the panel both come out as "not yours".
    """
    cleaned = unquote(url_path or "").lstrip("/")
    if not cleaned:
        cleaned = "index.html"
    if "\x00" in cleaned:
        return None
    try:
        root_resolved = root.resolve()
        candidate = (root / cleaned).resolve()
    except (OSError, ValueError):
        return None
    if candidate == root_resolved:
        candidate = candidate / "index.html"
    if not candidate.is_relative_to(root_resolved):
        return None
    if not candidate.is_file():
        return None
    return candidate


def content_type_for(path: Path) -> str:
    """The content type one file is served as."""
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def new_token() -> str:
    """A fresh pairing token. Printed once, never stored."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def parse_origins(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalise the origins somebody asked to allow.

    Args:
        raw: A comma-separated string, as an environment variable carries, or an
            iterable of origins, as a repeated flag produces.

    Returns:
        Origins as ``scheme://host[:port]``, lower-cased, with no path and no
        trailing slash, sorted and deduplicated, so two spellings of one origin
        allow it once and the banner prints the same thing every run.

    Raises:
        ConfigurationError: for a wildcard, or for something that is not an
            ``http(s)`` origin. A wildcard in particular would undo the whole
            Origin check, which is the one thing standing between this panel and
            any other page open in the same browser.
    """
    if raw is None:
        return ()
    items = [raw] if isinstance(raw, str) else list(raw)
    found: list[str] = []
    for item in items:
        for piece in str(item).split(","):
            origin = piece.strip().rstrip("/")
            if not origin:
                continue
            if "*" in origin:
                raise ConfigurationError(
                    f"{origin!r} is not an origin this panel can allow: a wildcard would let "
                    "any page in a browser reach it.\n"
                    "  Name the address: --allow-origin https://panel.example.com"
                )
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ConfigurationError(
                    f"{origin!r} is not an origin. It needs a scheme and a host, as in "
                    "https://panel.example.com."
                )
            port = parsed.port
            host = parsed.hostname.lower()
            implicit = (parsed.scheme, port) in {("https", 443), ("http", 80)}
            suffix = "" if implicit or port is None else f":{port}"
            found.append(f"{parsed.scheme}://{host}{suffix}")
    return tuple(sorted(dict.fromkeys(found)))


@dataclass(slots=True)
class WebConfig:
    """How the panel is served.

    Args:
        host: Must be loopback. Refusing anything else is the point: a panel
            that can read transcripts and start turns is not a service.
        port: What to listen on. ``0`` asks the operating system for a free
            port, which is what the tests do.
        token: The pairing token; generated when empty.
        chat: Whether ``POST /api/chat`` may run a turn at all. On by default,
            because a panel you can open but not write to is a panel you will
            stop opening; ``--no-chat`` is how it is switched off, and an
            ``ask`` policy is answered in the browser rather than refused.
        verbose: Log one line per request, never a token or a body.
        allowed_origins: Browser origins to accept besides this panel's own, for
            reaching it through your own tunnel or reverse proxy. The server
            still binds loopback and still wants the token on every request;
            this only says which *page* may talk to it. Left empty, the panel
            answers to nothing but itself.
        allowed_frames: Origins permitted to put the panel in a frame. The
            default policy is ``frame-ancestors 'none'``, because a panel nobody
            can see is a panel somebody is clickjacking; naming an origin here is
            how you embed it in your own dashboard anyway. ``'self'`` is always
            allowed.
    """

    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    token: str = ""
    chat: bool = True
    verbose: bool = False
    allowed_origins: tuple[str, ...] = ()
    allowed_frames: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_loopback_host(self.host):
            raise ConfigurationError(
                f"Refusing to serve the panel on {self.host or 'all interfaces'}: it is "
                "loopback only.\n"
                "  The panel reads your transcripts and can start a turn, so it is not "
                "something to publish.\n"
                "  Reach it from another machine with your own tunnel or VPN instead."
            )
        if not self.token:
            self.token = new_token()
        self.allowed_origins = parse_origins(self.allowed_origins)
        self.allowed_frames = parse_origins(self.allowed_frames)

    @property
    def origin(self) -> str:
        """The address the panel is served on."""
        host = f"[{self.host}]" if self.host in {"::1"} else self.host
        return f"http://{host}:{self.port}"

    @property
    def url(self) -> str:
        """The address to open, carrying the token in the fragment.

        A fragment rather than a query string: the browser never sends it to
        the server, so it cannot end up in a log line or a Referer header. The
        panel reads it once and clears it from the address bar.
        """
        return f"{self.origin}/#token={self.token}"

    def own_origins(self, port: int | None = None) -> set[str]:
        """Every origin this panel can legitimately be opened from."""
        used = self.port if port is None else port
        hosts = {"localhost", "127.0.0.1", "[::1]"}
        if self.host in hosts:
            hosts.add(self.host)
        return {f"http://{name}:{used}" for name in hosts} | set(self.allowed_origins)

    def allowed_hosts(self) -> set[str]:
        """Hostnames ``Host`` may carry besides this machine's own.

        A tunnel or a reverse proxy forwards requests under *its* hostname, and
        refusing those is exactly what makes the DNS-rebinding check work, so an
        extra one is accepted only because somebody named it explicitly.
        """
        return {
            parsed.hostname.lower()
            for parsed in (urlparse(origin) for origin in self.allowed_origins)
            if parsed.hostname
        }


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """The arguments of a call, short enough to put in a browser.

    A ``write_file`` call carries the whole file as one value, and the diff
    below already shows it. What the approval card needs is which file and
    roughly what is happening to it, so every value is clipped and the card
    never has to render a document.
    """
    shown: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > MAX_ARGUMENT_PREVIEW:
            shown[key] = value[:MAX_ARGUMENT_PREVIEW] + f" ... ({len(value)} chars)"
        else:
            shown[key] = value
    return shown


def approval_preview_text(tool: str, arguments: dict[str, Any], workspace: Path) -> str:
    """The preview the terminal would print, rendered to plain text.

    Rendered rather than re-implemented: one description of one change, shown
    on two surfaces, with no chance of the panel saying something the terminal
    would have worded differently.
    """
    buffer = io.StringIO()
    Console(file=buffer, width=100, no_color=True).print(
        approval_preview(tool, arguments, workspace)
    )
    return buffer.getvalue().rstrip()


@dataclass(slots=True)
class ApprovalRequest:
    """One change waiting for somebody to answer it in the browser."""

    id: str
    tool: str
    arguments: dict[str, Any]
    preview: str
    session: str
    created: float
    decided: threading.Event = field(default_factory=threading.Event)
    #: ``allow``, ``always``, ``deny``, ``quit``, or why it was given up on.
    answer: str = ""

    def payload(self) -> dict[str, Any]:
        """What the browser shows: enough to decide, nothing that can draw itself."""
        return {
            "id": self.id,
            "tool": self.tool,
            "arguments": _safe_arguments(self.arguments),
            "preview": self.preview,
            "session": self.session,
            "timeout": APPROVAL_TIMEOUT,
            "age": round(time.time() - self.created, 1),
        }


class ApprovalDesk:
    """Where a turn blocked on a question meets the answer a browser posts.

    The turn runs on one thread and waits inside the approver; the answer
    arrives on another as ``POST /api/approve``. One desk per server, so two
    tabs running two turns keep their own questions apart, and every request
    carries an id that only this run could have handed out.
    """

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._guard = threading.Lock()

    def open(
        self, *, tool: str, arguments: dict[str, Any], preview: str, session: str = ""
    ) -> ApprovalRequest:
        """Register a question and hand back the request to wait on."""
        request = ApprovalRequest(
            id=secrets.token_urlsafe(8),
            tool=tool,
            arguments=dict(arguments or {}),
            preview=preview,
            session=session,
            created=time.time(),
        )
        with self._guard:
            self._pending[request.id] = request
        return request

    def answer(self, identifier: str, decision: str) -> bool:
        """Wake one waiting turn. False when nothing is waiting under that id."""
        with self._guard:
            request = self._pending.get(identifier)
        if request is None:
            return False
        request.answer = decision
        request.decided.set()
        return True

    def give_up(self, reason: str) -> None:
        """Answer everything still waiting with ``reason``, so nothing blocks.

        Called when the browser went away mid-turn: the thread would otherwise
        sit until the timeout for a reader who is no longer there.
        """
        with self._guard:
            waiting = list(self._pending.values())
        for request in waiting:
            request.answer = reason
            request.decided.set()

    def close(self, identifier: str) -> None:
        """Forget a question that has been answered."""
        with self._guard:
            self._pending.pop(identifier, None)

    def pending(self) -> list[dict[str, Any]]:
        """Every question waiting right now, for a panel that has just loaded."""
        with self._guard:
            return [request.payload() for request in self._pending.values()]


#: What the model is told when nobody answers. Wording matters: a model that
#: reads "denied" retries nothing, and one that reads "nobody answered" should
#: not assume the change was wanted either.
_NO_ANSWER = (
    "Nobody answered the {tool} approval in the panel within {seconds:.0f} seconds, "
    "so it did not run and nothing was changed. Do not retry it. Say what you "
    "wanted to do and let the reader decide."
)
_DECLINED = (
    "The reader declined the {tool} call in the panel. Do not retry it; ask what "
    "they would prefer, or continue with the rest of the task."
)
#: The answer :meth:`ApprovalDesk.give_up` uses; not a word a browser can post.
_ABANDONED_KEY = "abandoned"

_ABANDONED = (
    "The panel went away before answering, so {tool} did not run and nothing was "
    "changed. Do not retry it."
)


class PanelApprover(Approver):
    """Asks in the browser that started the turn.

    ``ask`` on a terminal prints a diff and waits for a key. A turn started
    from the panel has no terminal anybody is watching, which used to mean the
    combination was refused outright, and a panel you could not write from.
    This asks where the reader already is: the same preview goes out as one
    server-sent event, the turn blocks, and ``POST /api/approve`` wakes it.
    """

    def __init__(
        self,
        mode: Mode,
        *,
        desk: ApprovalDesk,
        emit: Callable[[str, dict[str, Any]], None],
        session_id: str = "",
        workspace: Path | None = None,
        always: set[str] | None = None,
    ) -> None:
        super().__init__(mode, workspace=workspace)
        self._desk = desk
        self._emit = emit
        self._session = session_id
        if always is not None:
            # Not a copy. ``check`` reads this set before asking and ``_ask``
            # adds to it, so sharing the one the server holds for this
            # conversation is what lets an answer outlive the turn it was given
            # in. See :meth:`PanelServer.always_for`.
            self.always = always

    def _ask(self, tool: str, arguments: dict[str, Any]) -> Decision:
        request = self._desk.open(
            tool=tool,
            arguments=arguments,
            preview=approval_preview_text(tool, arguments, self.workspace),
            session=self._session,
        )
        try:
            self._emit("approval", request.payload())
            waited = 0.0
            while not request.decided.wait(APPROVAL_HEARTBEAT):
                waited += APPROVAL_HEARTBEAT
                if waited >= APPROVAL_TIMEOUT:
                    return Decision(False, _NO_ANSWER.format(tool=tool, seconds=APPROVAL_TIMEOUT))
                # Raises _ClientGone when the tab that was going to answer is not
                # there any more, which is how a blocked turn finds that out.
                self._emit("ping", {"waiting": request.id, "seconds": round(waited)})
            answer = request.answer
        finally:
            self._desk.close(request.id)
        if answer == "allow":
            return Decision(True)
        if answer == "always":
            self.always.add(tool)
            return Decision(True)
        if answer == "quit":
            # The same thing [q] does on a terminal: stop the turn, not the run.
            raise KeyboardInterrupt
        if answer == _ABANDONED_KEY:
            return Decision(False, _ABANDONED.format(tool=tool))
        return Decision(False, _DECLINED.format(tool=tool))


# ---------------------------------------------------------------------------
# Payloads. Plain functions, so they can be tested without a socket.
# ---------------------------------------------------------------------------
def status_payload(settings: Settings, config: WebConfig) -> dict[str, Any]:
    """What the panel shows before it asks for anything else."""
    return {
        "service": "jAIgent",
        "version": __version__,
        "provider": settings.provider,
        "model": settings.model,
        "workspace": str(settings.workspace),
        "approval": settings.approval,
        "chat": config.chat,
        # Whether the editor can save: dry-run refuses every write, and saying
        # so here is what lets the panel show a read-only file instead of a
        # button that only fails once it is pressed.
        "writes": settings.approval != Mode.DRY_RUN.value,
        "approval_timeout": APPROVAL_TIMEOUT,
        "stream": bool(settings.stream),
        "sessions": len(sessions.list_sessions(limit=MAX_LIST)),
        "panel": asset_dir() is not None,
    }


def session_summary(session: sessions.Session) -> dict[str, Any]:
    """One row of the session list: enough to recognise a conversation."""
    return {
        "id": session.id,
        "title": session.title or "untitled",
        "provider": session.provider,
        "model": session.model,
        "workspace": session.workspace,
        "source": session.source or "unknown",
        "turns": session.turns,
        "created": session.created,
        "updated": session.updated,
        "age": session.age(),
        "tokens": int(session.usage.get("total_tokens", 0) or 0),
    }


def session_payload(session: sessions.Session) -> dict[str, Any]:
    """One conversation: the transcript, and what it cost.

    Tool payloads are left out exactly as the terminal's session viewer leaves
    them out, and nothing here is rendered as HTML by the panel: the browser
    builds text nodes, so a transcript containing markup cannot draw itself.
    """
    return {
        **session_summary(session),
        "usage": dict(session.usage),
        "messages": [{"role": role, "text": text} for role, text in session.transcript()],
    }


def saved_payload(session: sessions.Session) -> dict[str, Any]:
    """What the panel needs to update a conversation it already has open.

    Sent after every save rather than left to a refetch: a sidebar that reloads
    the whole list on each turn is a sidebar that flickers, moves the row the
    reader was looking at, and can show a title the server has already replaced.
    """
    return {
        "id": session.id,
        "title": session.title or "untitled",
        "turns": session.turns,
        "updated": session.updated,
        "tokens": int(session.usage.get("total_tokens", 0) or 0),
    }


class FileConflict(Exception):
    """The file changed on disk between the editor reading it and saving it.

    The reader is told rather than overwritten: an agent turn, another tab or a
    text editor can all have written in between, and the one thing a browser
    must never do to a file on your machine is quietly replace it with a copy
    older than what is there.
    """

    def __init__(self, path: str, modified: float) -> None:
        self.path = path
        self.modified = modified
        super().__init__(f"{path} changed on disk since the editor read it")


def _editable(workspace: Path, target: Path) -> bool:
    """Whether the panel may write here, which is whatever the file tools say."""
    try:
        refuse_if_writable(workspace, target)
    except ToolError:
        return False
    return True


def files_payload(workspace: Path, rel: str = ".", limit: int = MAX_LIST) -> dict[str, Any]:
    """One folder of the workspace, as the panel's file list.

    Args:
        workspace: The one folder the panel can reach.
        rel: Which folder to list, relative to the workspace. Empty and ``.``
            both mean the workspace itself.
        limit: Most entries to return, so a folder holding a build tree does not
            become a megabyte of JSON.

    Returns:
        ``path`` and ``parent`` (workspace-relative, forward slashes) and
        ``entries``, folders first and then files, each with its size, its
        modification time and whether the panel may write to it.

    Raises:
        SandboxViolation: for a path that leaves the workspace, or one that is
            not there. The handler turns it into a 403 or a 404, so a browser
            never learns a path it could not already see.
    """
    root = resolve_in_workspace(workspace, rel or ".")
    if not root.exists():
        raise SandboxViolation(f"There is no {rel or '.'} in this workspace.")
    if root.is_file():
        raise SandboxViolation(f"{rel or '.'} is a file, not a folder.")
    try:
        children = sorted(root.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    except OSError as exc:
        raise SandboxViolation(f"{rel or '.'} cannot be listed: {exc}") from exc

    cap = max(1, min(int(limit or MAX_LIST), MAX_LIST))
    entries: list[dict[str, Any]] = []
    for child in children[:cap]:
        try:
            stat = child.stat()
        except OSError:
            continue  # gone between the listing and the stat; nothing to show
        is_dir = child.is_dir()
        entries.append(
            {
                "name": child.name,
                "path": relative_to_workspace(workspace, child),
                "kind": "dir" if is_dir else "file",
                "size": 0 if is_dir else int(stat.st_size),
                "modified": stat.st_mtime,
                "symlink": child.is_symlink(),
                "editable": False if is_dir else _editable(workspace, child),
            }
        )

    at_root = root.resolve() == Path(workspace).expanduser().resolve()
    return {
        "workspace": str(workspace),
        "path": relative_to_workspace(workspace, root),
        "parent": "" if at_root else relative_to_workspace(workspace, root.parent),
        "entries": entries,
        "truncated": len(children) > len(entries),
    }


def file_payload(workspace: Path, rel: str) -> dict[str, Any]:
    """One text file, in the shape the editor needs.

    Raises:
        SandboxViolation: for a path outside the workspace, one the file tools
            may not read, a folder, a file too large to hold in a browser tab,
            or one that is not UTF-8 text.
    """
    target = resolve_in_workspace(workspace, rel)
    refuse_if_blocked(workspace, target)
    if not target.is_file():
        raise SandboxViolation(f"{rel} is not a readable file.")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise SandboxViolation(
            f"{rel} is {size:,} bytes, above the {MAX_FILE_BYTES:,} bytes an editor "
            "tab is asked to hold. Open it in your own editor instead."
        )
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SandboxViolation(
            f"{rel} is not UTF-8 text ({exc.reason}), so it looks binary."
        ) from exc
    if "\x00" in text:
        # Valid UTF-8 and still not text: a NUL is what an executable, an image
        # or a database looks like once decoded, and an editor that showed one
        # would offer to save that file back with the rest of it mangled.
        raise SandboxViolation(f"{rel} contains a NUL byte, so it is not a text file.")
    return {
        "path": relative_to_workspace(workspace, target),
        # Universal newlines: the text box holds one kind of line break, and
        # `eol` is what the save puts back so the file keeps its own.
        "text": text,
        "eol": line_ending_of(target),
        "size": size,
        "lines": len(text.splitlines()),
        "modified": target.stat().st_mtime,
        "editable": _editable(workspace, target),
    }


def write_file_payload(
    workspace: Path,
    rel: str,
    text: str,
    *,
    base_modified: float | None = None,
    checkpoints: CheckpointStore | None = None,
) -> dict[str, Any]:
    """Save one file from the editor, the way the agent would have saved it.

    Three things make this more than ``Path.write_text``:

    * The path is resolved inside the workspace and then held to the same blocks
      the file tools honour, so the panel cannot be pointed at ``.git``, at a
      secret or at a plugin file the next run would import.
    * A checkpoint is taken first, which is what lets ``jaigent undo`` revert an
      edit made in a browser exactly as it reverts one made by the agent.
    * ``base_modified`` is compared with the file on disk, so an edit that
      landed while the tab was open fails loudly instead of being replaced.
    * The file's own line endings are put back. A text box holds ``\\n`` for
      every line, so writing what it holds would re-end the whole file: CRLF to
      LF here, and LF to CRLF on Windows, where a repository that asks for LF
      would show a one-line edit as a change to every line.

    The write goes to a temporary file in the same folder and is renamed over
    the target, which is how a save interrupted by a closed tab or a full disk
    leaves the old file readable rather than half written. The original
    permissions are carried over, so saving a script does not quietly make it
    not one.

    Args:
        workspace: The one folder the panel can reach.
        rel: Where to write, relative to the workspace.
        text: The whole file, as the editor holds it.
        base_modified: The modification time the editor read. Left out for a new
            file, compared with the disk when given.
        checkpoints: The store to snapshot into; defaults to the workspace's.

    Returns:
        The path written, its new size, line count and modification time, and
        the checkpoint that can undo it.

    Raises:
        SandboxViolation: for a path the panel may not write, or text too long.
        FileConflict: when the file changed since the editor read it.
    """
    target = resolve_in_workspace(workspace, rel)
    refuse_if_writable(workspace, target)
    if len(text) > MAX_WRITE_CHARS:
        raise SandboxViolation(
            f"{rel} is {len(text):,} characters, above the {MAX_WRITE_CHARS:,} the "
            "panel will write in one save."
        )
    if target.exists() and not target.is_file():
        raise SandboxViolation(f"{rel} is not a file.")
    exists = target.is_file()
    if exists and base_modified is not None:
        current = target.stat().st_mtime
        if abs(current - float(base_modified)) > 0.01:
            raise FileConflict(relative_to_workspace(workspace, target), current)

    relative = relative_to_workspace(workspace, target)
    store = checkpoints or CheckpointStore(workspace)
    snapshot = store.capture([target], label=f"web panel {relative}", tool="write_file")

    # The ending the file already had, or this platform's own for a new one,
    # which is what the write_file tool would have given it. Written as bytes so
    # that nothing between here and the disk translates it again.
    ending = line_ending_of(target) if exists else os.linesep
    normalised = normalise_endings(text, ending)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.jaigent-saving")
    try:
        temporary.write_bytes(normalised.encode("utf-8"))
        if exists:
            temporary.chmod(S_IMODE(target.stat().st_mode))
        temporary.replace(target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    written = target.stat()
    return {
        "ok": True,
        "path": relative,
        "size": int(written.st_size),
        "lines": len(text.splitlines()),
        "modified": written.st_mtime,
        "checkpoint": snapshot.id if snapshot is not None else "",
    }


def sessions_payload(limit: int = DEFAULT_LIST, query: str = "") -> dict[str, Any]:
    """The saved conversations, newest first, optionally filtered."""
    limit = max(1, min(int(limit or DEFAULT_LIST), MAX_LIST))
    needle = (query or "").strip()
    found = sessions.search(needle, limit=limit) if needle else sessions.list_sessions(limit=limit)
    return {
        "sessions": [session_summary(session) for session in found],
        "count": len(found),
        "limit": limit,
        "query": needle,
    }


def checkpoint_payload(checkpoint: Checkpoint) -> dict[str, Any]:
    """One snapshot: what it holds, never the file contents themselves."""
    return {
        "id": checkpoint.id,
        "label": checkpoint.label,
        "tool": checkpoint.tool,
        "created": checkpoint.created,
        "age": checkpoint.age(),
        "summary": checkpoint.summary(),
        "files": [
            {"path": state.path, "existed": state.existed, "skipped": state.skipped}
            for state in checkpoint.files
        ],
    }


def checkpoints_payload(workspace: Path, limit: int = DEFAULT_LIST) -> dict[str, Any]:
    """The workspace's undo history, newest first."""
    limit = max(1, min(int(limit or DEFAULT_LIST), MAX_LIST))
    store = CheckpointStore(workspace)
    return {
        "workspace": str(workspace),
        "checkpoints": [checkpoint_payload(item) for item in store.history(limit=limit)],
        "size": store.size(),
    }


def settings_payload(settings: Settings) -> dict[str, Any]:
    """The effective configuration, with every secret already masked."""
    return {"settings": settings.redacted()}


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------
class _ClientGone(Exception):
    """The browser stopped reading; nothing to report and nothing to retry."""


#: One lock per session id, so two browser tabs continuing the same
#: conversation cannot interleave their writes and lose one of them. Two
#: different conversations are independent and never wait on each other.
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _lock_for(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


#: Builds the agent for one turn. Swappable so a test can hand the panel a
#: scripted agent instead of a provider, the same seam the gateway has.
AgentFactory = Callable[[Settings], Any]


def build_agent(settings: Settings) -> Agent:
    """The agent a browser turn runs on: the same one the terminal uses.

    ``interactive=False`` because ``ask_user`` has nobody to ask: a picker needs
    a screen of its own and the turn is already holding one, so the model is
    told up front to use its own judgement instead of waiting.

    The approval policy comes from settings and is honoured here like anywhere
    else. Under ``ask`` the approver this builds is replaced, per turn, by
    :class:`PanelApprover`, which asks in the browser that started the turn; see
    :class:`ApprovalDesk` for how the two threads meet.
    """
    return Agent(
        settings,
        tools=build_default_registry(settings, interactive=False),
        approver=Approver(Mode(settings.approval), workspace=settings.workspace),
        surface="in the jAIgent web panel, in a browser",
        interactive=False,
    )


class PanelServer(ThreadingHTTPServer):
    """The HTTP server, carrying the three things every request needs.

    They live on the server rather than as class attributes of the handler
    because a plain function assigned to a class attribute becomes a method:
    ``self.agent_factory(settings)`` would hand the factory the handler as its
    first argument, and a factory built from a closure is not something mypy or
    a reader can tell apart from one that was not.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        settings: Settings,
        config: WebConfig,
        agent_factory: AgentFactory,
    ) -> None:
        self.settings = settings
        self.config = config
        self.agent_factory = agent_factory
        # One desk for the whole server: a turn blocks on its own thread and the
        # answer arrives on another, so the two have to meet somewhere both can
        # see. Keyed by an id this run handed out, so two tabs running two turns
        # never answer each other's questions.
        self.approvals = ApprovalDesk()
        self._always: dict[str, set[str]] = {}
        self._always_guard = threading.Lock()
        # Binding happens in the base constructor, so the attributes above have
        # to exist first: a request can arrive during it.
        super().__init__(address, handler)

    def always_for(self, session_id: str) -> set[str]:
        """The tools this conversation was told to stop asking about.

        One set per conversation, kept for the life of the panel, because that
        is what the terminal does: ``jaigent chat`` builds one approver for the
        whole session, so ``[a]lways`` there means "for the rest of this
        conversation". A panel that rebuilt the set on every turn asked the same
        question again one message later, which reads as a button that does
        nothing, and the reader has no way to tell that from being ignored.

        Two conversations keep two sets, so an answer given while discussing one
        is not permission for another, and a conversation picked up again in a
        later panel asks again rather than inheriting a grant nobody remembers
        making.
        """
        with self._always_guard:
            grant = self._always.get(session_id)
            if grant is None:
                while len(self._always) >= _MAX_ALWAYS_CONVERSATIONS:
                    # Insertion order: whichever has been here longest goes, and
                    # asks again if it comes back.
                    self._always.pop(next(iter(self._always)))
                grant = set()
                self._always[session_id] = grant
            return grant


class _Handler(BaseHTTPRequestHandler):
    """The panel's routes: static files, the read-only API, and one turn."""

    server_version = "jAIgent"

    @property
    def panel(self) -> PanelServer:
        """The server this request arrived on, typed."""
        server = self.server
        if not isinstance(server, PanelServer):  # pragma: no cover - defensive
            raise RuntimeError("the panel's handler is not attached to a PanelServer")
        return server

    @property
    def settings(self) -> Settings:
        """The configuration this panel was started with."""
        return self.panel.settings

    @property
    def config(self) -> WebConfig:
        """Host, port, token and whether chat is allowed."""
        return self.panel.config

    # -- plumbing ------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base class signature
        """One line per request on ``--verbose``, and nothing otherwise.

        Never the query string: a token can ride in one, and a log is the last
        place a secret should end up.
        """
        if not self.config.verbose:
            return
        import sys

        status = args[1] if len(args) > 1 else ""
        print(
            f"web {self.command} {urlparse(self.path).path} {status}",
            file=sys.stderr,
            flush=True,
        )

    #: Set while answering a HEAD, so the headers go out and the body does not.
    head_only = False

    def _write_body(self, raw: bytes) -> None:
        """Send bytes, and treat a vanished browser as the non-event it is.

        A closed tab raises here rather than at the caller, and there is nothing
        useful to do about it: the response has no reader. Reporting it would
        only fill a terminal with tracebacks for people pressing reload.
        """
        if self.head_only:
            return  # the content-length above already described it
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            self.wfile.write(raw)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self._security_headers()
        self.end_headers()
        self._write_body(raw)

    def _security_headers(self) -> None:
        """Headers that make the panel a poor target and a good citizen.

        ``no-store`` because transcripts are not something to leave in a shared
        cache, and the rest because a panel that renders text from a model
        should not be one script tag away from running it.
        """
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header(
            "content-security-policy",
            content_security_policy(frames=self.config.allowed_frames),
        )

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    # -- request-level checks ------------------------------------------
    def _host_allowed(self) -> bool:
        """Refuse a ``Host`` that does not name this machine.

        DNS rebinding is the reason: ``evil.example`` can resolve to
        127.0.0.1, and then the browser treats this server as same-origin with
        a page that has no business here. The one header an attacker cannot
        rewrite is the ``Host`` the browser sends.
        """
        host = (self.headers.get("host") or "").strip().lower()
        if not host:
            return True  # an HTTP/1.0 client with no host is not a browser
        name = host.split("]")[0].lstrip("[") if host.startswith("[") else host.rsplit(":", 1)[0]
        name = name.rstrip(".")
        return name in _ALLOWED_HOSTS or name in self.config.allowed_hosts()

    def _origin_allowed(self) -> bool:
        """Refuse a browser request from anywhere but this panel's own origin.

        No ``Origin`` at all means curl, a script, or the panel's own fetch on
        a same-origin GET, all of which are fine and still need the token. An
        ``Origin`` that is not ours means a page somewhere else in the browser
        is reaching into this port, which is never legitimate: unlike the
        gateway there is no cross-origin client worth allowing, because the
        panel is served from here.
        """
        origin = (self.headers.get("origin") or "").strip().rstrip("/")
        if not origin:
            return True
        port = getattr(self.server, "server_port", None) or self.config.port
        return origin in self.config.own_origins(port)

    def _authorised(self) -> bool:
        """Whether this request carries the pairing token for this run."""
        header = self.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            # Allowed as a fallback for a client that cannot set a header, an
            # EventSource. The fragment form the CLI prints never reaches here.
            token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        return bool(token) and secrets.compare_digest(token, self.config.token)

    def _guarded(self, *, needs_token: bool = True) -> bool:
        """Every check that is not the route's business. False means answered."""
        if not self._host_allowed():
            self._error(403, "The Host header does not name this machine.")
            return False
        if not self._origin_allowed():
            self._error(403, "Cross-origin requests are not allowed.")
            return False
        if needs_token and not self._authorised():
            self._error(
                401,
                "Missing or wrong pairing token. Run `jaigent web` and open the address it prints.",
            )
            return False
        return True

    # -- routes --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - required by the base class
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/health":
            # No token: it says only that something is listening, which is what
            # the panel needs to tell "not running" from "wrong token".
            if not self._guarded(needs_token=False):
                return
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "jAIgent",
                    "version": __version__,
                    "deployment": "local",
                    "chat": self.config.chat,
                },
            )
            return

        if path.startswith("/api/"):
            if not self._guarded():
                return
            self._api_get(path, parse_qs(parsed.query))
            return

        self._static(path)

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        def one(name: str, default: str = "") -> str:
            values = query.get(name) or []
            return str(values[0]) if values else default

        def number(name: str, default: int) -> int:
            # A query string is written by hand and by browsers: a nonsense
            # limit is answered with the default, not with a traceback.
            raw = one(name)
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        if path == "/api/status":
            self._send_json(200, status_payload(self.settings, self.config))
            return
        if path == "/api/sessions":
            self._send_json(
                200,
                sessions_payload(
                    limit=int(one("limit", str(DEFAULT_LIST)) or DEFAULT_LIST),
                    query=one("q"),
                ),
            )
            return
        if path.startswith("/api/sessions/"):
            identifier = path[len("/api/sessions/") :]
            # ``load`` validates the id against the generated format and refuses
            # a file that is a symlink, so a URL segment never reaches a path.
            session = sessions.load(identifier) if identifier else None
            if session is None:
                self._error(404, f"No session named {identifier!r}.")
                return
            self._send_json(200, session_payload(session))
            return
        if path == "/api/checkpoints":
            limit = int(one("limit", str(DEFAULT_LIST)) or DEFAULT_LIST)
            self._send_json(200, checkpoints_payload(self.settings.workspace, limit=limit))
            return
        if path == "/api/settings":
            self._send_json(200, settings_payload(self.settings))
            return
        if path == "/api/files":
            try:
                payload = files_payload(
                    self.settings.workspace,
                    one("path") or ".",
                    limit=number("limit", MAX_LIST),
                )
            except SandboxViolation as exc:
                self._error(404, str(exc))
                return
            self._send_json(200, payload)
            return
        if path == "/api/file":
            wanted = one("path")
            if not wanted:
                self._error(400, "A path is required: /api/file?path=notes.md.")
                return
            try:
                payload = file_payload(self.settings.workspace, wanted)
            except SandboxViolation as exc:
                self._error(404, str(exc))
                return
            self._send_json(200, payload)
            return
        if path == "/api/approvals":
            # What a tab that has just loaded, or just come back, is missing:
            # a question asked while it was away is still waiting.
            self._send_json(200, {"approvals": self.panel.approvals.pending()})
            return
        self._error(404, f"No API route named {path}.")

    def _static(self, path: str) -> None:
        """Serve one file of the panel."""
        if not self._guarded(needs_token=False):
            return
        root = asset_dir()
        if root is None:
            self._error(
                500,
                "The panel's files are not installed. In a source checkout they live in "
                "web/public; in a wheel or a binary they are packaged under jaigent/data/web.",
            )
            return
        target = resolve_asset(root, path)
        if target is None:
            self._error(404, f"Nothing is served at {path}.")
            return
        try:
            raw = target.read_bytes()
        except OSError as exc:
            self._error(500, f"Could not read the panel: {exc}")
            return
        self.send_response(200)
        self.send_header("content-type", content_type_for(target))
        self.send_header("content-length", str(len(raw)))
        self._security_headers()
        self.end_headers()
        self._write_body(raw)

    def do_HEAD(self) -> None:  # noqa: N802 - required by the base class
        """Answer a HEAD exactly as a GET, minus the body.

        A browser rarely sends one, but proxies, uptime checks and the preview a
        page gets when somebody pastes a link all do, and a 501 for a method the
        server otherwise serves reads as "broken" from the outside.
        """
        self.head_only = True
        try:
            self.do_GET()
        finally:
            self.head_only = False

    def do_POST(self) -> None:  # noqa: N802 - required by the base class
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path not in {"/api/chat", "/api/file", "/api/approve"}:
            if not self._guarded(needs_token=False):
                return
            self._error(404, f"No API route named {path}.")
            return
        if not self._guarded():
            return
        if path == "/api/chat":
            self._chat()
        elif path == "/api/file":
            self._save_file()
        else:
            self._approve()

    def do_OPTIONS(self) -> None:  # noqa: N802 - required by the base class
        """Answer a preflight for our own origin only.

        The panel is same-origin with its API, so a browser only preflights
        when something *else* is trying. Answering with no CORS headers at all
        is what makes that attempt fail in the browser before it is sent.
        """
        if not self._host_allowed() or not self._origin_allowed():
            self._error(403, "Cross-origin requests are not allowed.")
            return
        self.send_response(204)
        self.send_header("content-length", "0")
        self.send_header("allow", "GET, POST, OPTIONS")
        self.end_headers()

    # -- an answer, and a save -----------------------------------------
    def _approve(self) -> None:
        """Answer one approval a turn is blocked on.

        The decision is a word rather than a keystroke because it travels as
        JSON, and the set is the terminal's: allow this one, allow this tool for
        the rest of the run, decline it, or stop the turn.
        """
        body = self._read_body()
        identifier = str(body.get("id") or "").strip()
        decision = str(body.get("decision") or "").strip().lower()
        if not identifier:
            self._error(400, 'An approval id is required: {"id": "...", "decision": "allow"}.')
            return
        if decision not in {"allow", "always", "deny", "quit"}:
            self._error(
                400,
                "A decision is required, and it is one of allow, always, deny or quit.",
            )
            return
        if not self.panel.approvals.answer(identifier, decision):
            self._error(404, "Nothing is waiting on that approval any more.")
            return
        self._send_json(200, {"ok": True, "id": identifier, "decision": decision})

    def _save_file(self) -> None:
        """Write one file from the editor into the workspace on this machine."""
        if self.settings.approval == Mode.DRY_RUN.value:
            self._error(
                403,
                "This panel is in dry-run mode, so it does not write files. Restart it "
                "without --dry-run to save from here.",
            )
            return
        body = self._read_body(limit=MAX_WRITE_BODY_BYTES)
        rel = str(body.get("path") or "").strip()
        if not rel:
            self._error(400, 'A path is required: {"path": "notes.md", "text": "..."}.')
            return
        text = body.get("text")
        if not isinstance(text, str):
            self._error(400, "The text to write is required, as a string.")
            return
        if self.settings.approval == Mode.ASK.value and body.get("approved") is not True:
            # The policy is ask and the reader is right here, so the panel shows
            # what is about to be written and posts it again with the answer in
            # it. Requiring the flag is what keeps "ask" meaning something on
            # this route instead of being a setting only the chat honours.
            self._error(
                428,
                "This panel asks before writing. Show the change to the reader and send "
                "it again with approved set to true.",
            )
            return
        base = body.get("base_modified")
        try:
            when = float(base) if base is not None else None
        except (TypeError, ValueError):
            when = None
        try:
            payload = write_file_payload(self.settings.workspace, rel, text, base_modified=when)
        except FileConflict as exc:
            self._send_json(
                409,
                {
                    "error": f"{exc.path} changed on disk since this editor read it. "
                    "Reload it before saving, so the newer copy is not replaced.",
                    "path": exc.path,
                    "modified": exc.modified,
                },
            )
            return
        except SandboxViolation as exc:
            self._error(403, str(exc))
            return
        except OSError as exc:
            self._error(500, f"Could not write {rel}: {exc}")
            return
        self._send_json(200, payload)

    # -- the one turn --------------------------------------------------
    def _read_body(self, limit: int = MAX_BODY_BYTES) -> dict[str, Any]:
        """The JSON body of a POST, or an empty dict if it is not one.

        Args:
            limit: Most bytes to accept. A prompt is small and a file is not, so
                the two routes ask for different caps rather than sharing one
                that is either too tight for an editor or too loose for a chat.
        """
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            return {}
        try:
            raw = self.rfile.read(length)
        except (OSError, ValueError):  # pragma: no cover - client went away
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        """One server-sent event. Raises :class:`_ClientGone` if nobody listens."""
        chunk = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise _ClientGone from exc

    def _chat(self) -> None:
        """Run one turn and stream what happens, event by event."""
        if not self.config.chat:
            self._error(
                403,
                "This panel was started read-only, with --no-chat. Restart it without that "
                "flag to run turns from the browser.",
            )
            return
        # An `ask` policy is not refused here any more. It used to be, on the
        # reasoning that a browser cannot answer a prompt printed on a terminal
        # nobody is watching, which was true of the terminal and says nothing
        # about the panel: the question is asked in the tab that started the
        # turn, and PanelApprover below is what asks it.

        body = self._read_body()
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            self._error(400, 'A prompt is required: {"prompt": "..."}.')
            return
        if len(prompt) > MAX_PROMPT_CHARS:
            self._error(400, f"A prompt is at most {MAX_PROMPT_CHARS} characters.")
            return
        requested = str(body.get("session") or "").strip()
        if requested and sessions.load(requested) is None:
            self._error(404, "No session with that id; leave `session` out to start one.")
            return

        # A new conversation gets its id before the lock, so two tabs both
        # starting one do not queue behind each other; continuing an existing
        # one locks on the id the caller named.
        session = None if requested else self._new_session()
        key = requested or (session.id if session is not None else "")

        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-accel-buffering", "no")
        self.end_headers()

        try:
            with _lock_for(key):
                if requested:
                    # Loaded inside the lock. Another tab may have written to
                    # this conversation while this request was waiting for it,
                    # and saving over a copy read before then would quietly
                    # delete whatever that tab just added.
                    session = sessions.load(requested)
                if session is None:
                    self._emit("error", {"error": "That conversation is gone; start a new one."})
                    return
                self._run_turn(prompt, session)
        except _ClientGone:
            # The tab stopped reading. Anything still waiting on an answer from
            # it is told that nobody is there, so the turn ends now instead of
            # blocking until the timeout on a reader who has gone.
            self.panel.approvals.give_up(_ABANDONED_KEY)
            return
        except Exception as exc:  # noqa: BLE001 - one turn must not kill the server
            with contextlib.suppress(_ClientGone):
                self._emit("error", {"error": f"{type(exc).__name__}: {exc}"})

    def _new_session(self) -> sessions.Session:
        """A conversation started from the panel. Saved once it has a turn."""
        return sessions.Session.new(
            provider=self.settings.provider,
            model=self.settings.model,
            workspace=str(self.settings.workspace),
            source="web",
        )

    def _run_turn(self, prompt: str, session: sessions.Session | None) -> None:
        """Drive the existing agent, forwarding what it does to the browser.

        Two promises this keeps that a plain call to :meth:`Agent.run` does not:

        * **The conversation is saved before the provider is called, and again
          on the way out whatever happened.** A prompt that reached the panel is
          a conversation, and a turn that fails, is stopped, or loses its tab
          used to take the whole exchange with it, which reads as a panel that
          loses your work.
        * **Every tool call is one event pair with an id.** The panel updates the
          line it already drew instead of appending a second one, so a call
          never appears twice.
        """
        if session is not None:
            self._emit("session", {"id": session.id, "title": session.title})

        # One id per call, and a queue per tool name so that two calls to the
        # same tool in a row are matched to their own endings in order.
        counter = itertools.count(1)
        open_calls: dict[str, deque[str]] = {}

        def on_text(chunk: str) -> None:
            self._emit("token", {"text": chunk})

        def on_tool_start(name: str, arguments: dict[str, Any]) -> None:
            call = f"c{next(counter)}"
            open_calls.setdefault(name, deque()).append(call)
            self._emit(
                "tool",
                {
                    "state": "start",
                    "tool": name,
                    "call": call,
                    "arguments": _safe_arguments(arguments),
                },
            )

        def on_tool_end(name: str, arguments: dict[str, Any], output: str) -> None:
            waiting = open_calls.get(name)
            call = waiting.popleft() if waiting else ""
            # The first line only: a tool's whole output can be a whole file,
            # and the panel is a status surface, not a second transcript.
            text = output or ""
            first = text.splitlines()[0] if text else ""
            self._emit(
                "tool",
                {
                    "state": "end",
                    "tool": name,
                    "call": call,
                    "ok": not text.startswith("ERROR"),
                    "detail": first[:200],
                },
            )

        agent = self.panel.agent_factory(self.settings)
        agent.on_text = on_text
        agent.on_tool_start = on_tool_start
        agent.on_tool_call = on_tool_end
        if session is not None and session.messages:
            agent.load_history(session.messages)

        if self.settings.approval == Mode.ASK.value and getattr(agent, "approver", None):
            # Same policy, different surface: the diff goes to the browser as one
            # event and the turn waits for the answer to come back as a POST.
            agent.approver = PanelApprover(
                Mode.ASK,
                desk=self.panel.approvals,
                emit=self._emit,
                session_id=session.id if session is not None else "",
                workspace=self.settings.workspace,
                always=self.panel.always_for(session.id if session is not None else ""),
            )

        # What the conversation will look like the moment the provider is asked:
        # the history so far plus this prompt. Saved first, so that is the least
        # that is on disk however the turn ends.
        promised: list[dict[str, Any]] = [*agent.history, {"role": "user", "content": prompt}]
        # A copy of the history as it stood before the run, to tell "the agent
        # recorded nothing" from "the agent recorded something shorter", which a
        # turn that compacted a long conversation legitimately does.
        before = list(agent.history)
        if session is not None:
            session.set_title_from(prompt)
            session.touch(promised)
            with contextlib.suppress(OSError):
                session.save()
            with contextlib.suppress(_ClientGone, OSError):
                self._emit("saved", saved_payload(session))

        result: AgentResult | None = None
        settled = False

        def settle_session() -> None:
            """Put the conversation as it now stands on disk, and say so.

            Called once on the way through and once on the way out, whichever
            comes first: a turn that finishes reports its save before it reports
            that it is done, and a turn that raised, was stopped, or lost its
            tab still reports the one it managed.
            """
            nonlocal settled
            if settled or session is None:
                return
            settled = True
            # The history the agent ended with, including a partial answer and
            # the calls that produced it, unless it is exactly what it was
            # before the run, which means nothing was recorded at all and the
            # prompt as promised is the more honest thing to keep. Length is not
            # the test: a turn that compacted a long conversation has a shorter
            # history than it started with, and "longest wins" would put the
            # un-compacted one back on disk.
            history = promised if list(agent.history) == before else agent.history
            session.touch(history, result.usage if result is not None else None)
            with contextlib.suppress(OSError):
                session.save()
            with contextlib.suppress(_ClientGone, OSError):
                self._emit("saved", saved_payload(session))

        try:
            result = agent.run(prompt)
            settle_session()
            self._emit(
                "done",
                {
                    "output": result.output,
                    "session": session.id if session is not None else "",
                    "steps": result.tool_calls,
                    "usage": result.usage,
                    "tokens": result.cost.total_tokens if result.cost else 0,
                    "usd": result.cost.usd if result.cost else None,
                    "elapsed": round(result.elapsed, 3),
                    "stopped_early": result.stopped_early,
                },
            )
        finally:
            settle_session()


def build_server(
    settings: Settings,
    config: WebConfig,
    agent_factory: AgentFactory | None = None,
) -> PanelServer:
    """A server bound to loopback, wired to ``settings``.

    Args:
        settings: The configuration every turn and every listing is read from.
        config: Host, port, token and whether chat is allowed.
        agent_factory: Builds the agent for one turn; defaults to
            :func:`build_agent`. Tests hand in a scripted one, which is the only
            way to exercise a turn without a provider.

    Raises:
        ConfigurationError: if the host is not loopback, or chat is asked for
            under an approval policy nobody could answer.
    """
    # Everything a request needs hangs off the server, so there is no handler
    # subclass to build and nothing to leak between two panels in one process.
    # A browser that closes a tab must not leave a thread behind, and a turn
    # that outlives Ctrl-C must not keep the process alive: both are what
    # ``daemon_threads``, set on PanelServer, buys.
    return PanelServer(
        (config.host, config.port),
        _Handler,
        settings=settings,
        config=config,
        agent_factory=agent_factory or build_agent,
    )
