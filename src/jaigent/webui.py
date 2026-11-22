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

Read-only is the default. ``POST /api/chat`` exists only behind ``--chat``,
and refuses to run when the approval policy is ``ask``, because a browser
cannot answer a prompt that would be printed on a terminal nobody is watching.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from jaigent import __version__
from jaigent import session as sessions
from jaigent.agent import Agent
from jaigent.approval import Approver, Mode
from jaigent.checkpoint import Checkpoint, CheckpointStore
from jaigent.config import Settings
from jaigent.errors import ConfigurationError
from jaigent.gateway import is_loopback_host
from jaigent.tools import build_default_registry

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
        chat: Whether ``POST /api/chat`` may run a turn at all.
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
    chat: bool = False
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


def check_chat_allowed(settings: Settings) -> None:
    """Refuse ``--chat`` under an approval policy a browser cannot answer.

    ``ask`` prints a diff on a terminal and waits for a key. A turn started
    from the panel has nobody at that terminal, so the choice is between
    blocking forever and answering itself. Neither is acceptable, so the
    combination is refused before it can happen.

    Raises:
        ConfigurationError: when chat is on and the policy is ``ask``.
    """
    if settings.approval == Mode.ASK.value:
        raise ConfigurationError(
            "The web panel cannot answer an approval prompt: it would be printed on a "
            "terminal nobody is watching.\n"
            "  Run it read-only:  jaigent web\n"
            "  Or allow the work:  jaigent web --chat --yes\n"
            "  Or refuse writes:   jaigent web --chat --dry-run"
        )


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

    ``interactive=False`` because ``ask_user`` has nobody to ask. A question
    would block on a terminal the reader is not looking at, so the model is told
    up front to use its own judgement instead. The approval policy comes from
    settings, which is why ``--chat`` refuses to run under ``ask``.
    """
    return Agent(
        settings,
        tools=build_default_registry(settings, interactive=False),
        approver=Approver(Mode(settings.approval), workspace=settings.workspace),
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
        # Binding happens in the base constructor, so the attributes above have
        # to exist first: a request can arrive during it.
        super().__init__(address, handler)


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
        if path != "/api/chat":
            if not self._guarded(needs_token=False):
                return
            self._error(404, f"No API route named {path}.")
            return
        if not self._guarded():
            return
        self._chat()

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

    # -- the one turn --------------------------------------------------
    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
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
                "This panel is read-only. Start it with `jaigent web --chat` to run turns "
                "from the browser.",
            )
            return
        try:
            check_chat_allowed(self.settings)
        except ConfigurationError as exc:
            self._error(400, str(exc))
            return

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
        """Drive the existing agent, forwarding what it does to the browser."""
        if session is not None:
            self._emit("session", {"id": session.id, "title": session.title})

        def on_text(chunk: str) -> None:
            self._emit("token", {"text": chunk})

        def on_tool_start(name: str, arguments: dict[str, Any]) -> None:
            self._emit("tool", {"state": "start", "tool": name})

        def on_tool_end(name: str, arguments: dict[str, Any], output: str) -> None:
            # The first line only: a tool's whole output can be a whole file,
            # and the panel is a status surface, not a second transcript.
            first = (output or "").splitlines()[0] if output else ""
            self._emit(
                "tool",
                {
                    "state": "end",
                    "tool": name,
                    "ok": not output.startswith("ERROR"),
                    "detail": first[:200],
                },
            )

        agent = self.panel.agent_factory(self.settings)
        agent.on_text = on_text
        agent.on_tool_start = on_tool_start
        agent.on_tool_call = on_tool_end
        if session is not None and session.messages:
            agent.load_history(session.messages)

        result = agent.run(prompt)

        if session is not None:
            session.set_title_from(prompt)
            session.touch(agent.history, result.usage)
            session.save()

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
    if config.chat:
        check_chat_allowed(settings)

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
