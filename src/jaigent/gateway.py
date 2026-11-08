"""An OpenAI-compatible HTTP endpoint backed by the agent.

``jaigent serve`` exposes your agent at ``http://localhost:8787/v1``. Point any
OpenAI SDK at it, authenticate with a jAIgent key, and every request runs
through the full agent — auto model selection, web search, the workspace tools —
before the answer comes back in the shape the client expects.

::

    client = OpenAI(base_url="http://localhost:8787/v1", api_key="jgt-...")
    client.chat.completions.create(model="auto", messages=[...])

Keys are created with ``jaigent keys new`` and stored **hashed**, so the plain
text exists only at creation time. Your provider credentials stay in the server
process and are never exposed to callers.

The implementation uses only the standard library: adding a web framework to a
CLI tool for one endpoint is not a trade worth making.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jaigent import paths
from jaigent.errors import ConfigurationError
from jaigent.llm.base import text_content
from jaigent.paths import user_home

KEY_PREFIX = "jgt-"
KEYS_VERSION = 1


def keys_path() -> Path:
    """Where issued keys are recorded."""
    raw = os.getenv("JAIGENT_KEYS_FILE")
    if raw:
        return Path(raw).expanduser()
    return user_home() / "keys.json"


def hash_key(key: str) -> str:
    """SHA-256 of a key. Only the hash is ever written to disk."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class APIKey:
    """One issued key. ``secret`` is populated only when freshly minted."""

    id: str
    name: str
    hashed: str
    created: float = field(default_factory=time.time)
    last_used: float = 0.0
    calls: int = 0
    revoked: bool = False
    secret: str | None = None

    @property
    def preview(self) -> str:
        """Something safe to display: ``jgt-a1b2…``."""
        return f"{KEY_PREFIX}{self.id[:8]}…"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "hashed": self.hashed,
            "created": self.created,
            "last_used": self.last_used,
            "calls": self.calls,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> APIKey:
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            hashed=str(data.get("hashed", "")),
            created=float(data.get("created", 0.0)),
            last_used=float(data.get("last_used", 0.0)),
            calls=int(data.get("calls", 0)),
            revoked=bool(data.get("revoked", False)),
        )


def load_keys() -> list[APIKey]:
    """Every issued key. A corrupt file yields nothing rather than crashing."""
    path = keys_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    raw = data.get("keys", []) if isinstance(data, dict) else data
    keys: list[APIKey] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue  # a hand-edited file must not crash the server
        try:
            keys.append(APIKey.from_dict(item))
        except (TypeError, ValueError):
            continue
    return keys


def save_keys(keys: list[APIKey]) -> Path:
    """Persist the key list atomically, with owner-only permissions."""
    payload = {"version": KEYS_VERSION, "keys": [k.to_dict() for k in keys]}
    # write_private sets the mode before any bytes land, so there is no window
    # in which the file exists world-readable.
    return paths.write_private(keys_path(), json.dumps(payload, indent=2))


#: Key-store changes are read-modify-write cycles, and the gateway server is
#: threaded: two requests saving the key list they each loaded used to lose
#: one of the writes — a `verify_key` call saving its usage bump could erase
#: a key `keys new` had just appended, while the user was already holding the
#: only copy of its secret. One lock around each load→save cycle fixes it.
_KEYS_LOCK = threading.Lock()


def create_key(name: str = "default") -> APIKey:
    """Mint a key. The plain text is returned once and never stored."""
    secret = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
    key = APIKey(
        id=uuid.uuid4().hex,
        name=name.strip() or "default",
        hashed=hash_key(secret),
        secret=secret,
    )
    with _KEYS_LOCK:
        keys = load_keys()
        keys.append(key)
        save_keys(keys)
    return key


def revoke_key(identifier: str) -> APIKey | None:
    """Revoke by id or name. Returns the key that was revoked."""
    if not identifier:
        # Every id startswith(""), so without this `keys revoke ""` silently
        # revoked the first key in the file.
        return None
    with _KEYS_LOCK:
        keys = load_keys()
        for key in keys:
            if key.id == identifier or key.id.startswith(identifier) or key.name == identifier:
                key.revoked = True
                save_keys(keys)
                return key
    return None


def verify_key(candidate: str) -> APIKey | None:
    """Check a presented key, recording the use. ``None`` if it is not valid."""
    if not candidate:
        return None
    digest = hash_key(candidate.strip())
    with _KEYS_LOCK:
        keys = load_keys()
        for key in keys:
            # Constant-time compare so timing cannot reveal a valid prefix.
            if secrets.compare_digest(key.hashed, digest) and not key.revoked:
                key.last_used = time.time()
                key.calls += 1
                save_keys(keys)
                return key
    return None


# ----------------------------------------------------------------------
# Server
# ----------------------------------------------------------------------
#: Bind addresses that only this machine can reach. Note ``""`` is deliberately
#: absent: binding ``""`` listens on *every* interface, so treating it as
#: loopback used to let ``serve --host "" --no-auth`` expose the agent to the
#: network unauthenticated. ``0.0.0.0``/``::`` are also non-loopback for the same
#: reason — they mean "everywhere".
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def is_loopback_host(host: str) -> bool:
    """Whether ``host`` can only be reached from this machine.

    Anything else — ``0.0.0.0``, a LAN address, a hostname — is reachable by
    whoever else is on the network, and that is the whole difference between a
    local tool and an exposed service.
    """
    name = (host or "").strip().lower()
    if name in LOOPBACK_HOSTS:
        return True
    # ``0.0.0.0`` and ``::`` mean "all interfaces" — reachable remotely.
    if name in {"0.0.0.0", "::"} or name == "":  # nosec B104
        return False
    try:
        addr = ipaddress.ip_address(name.strip("[]"))
        # Unspecified addresses are not loopback; they bind everywhere.
        if addr.is_unspecified:
            return False
        if addr.is_loopback:
            return True
        # IPv4-mapped IPv6 like ::ffff:127.0.0.1 is loopback on stacks that
        # support it; treat it as loopback so --host ::ffff:127.0.0.1 --no-auth
        # does not expose the gateway.
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            return addr.ipv4_mapped.is_loopback
        return False
    except ValueError:
        return False


#: Ports that never count as "the same application". A page on a local dev
#: server (or anything else on this machine) is a different origin from the
#: gateway, so it gets no implicit trust.
_NOT_LOCAL_PORTS = frozenset({80, 443, 3000, 3001, 4200, 5000, 5173, 8000, 8080, 8081, 8888})


def _origin_is_local(origin: str) -> bool:
    """Whether a browser origin is a service on this machine.

    ``http://localhost:8787`` is the gateway's own page (for a small browser UI
    someone wrote); ``http://localhost:5173`` is somebody's dev server and is
    *not* trusted. Only the gateway's own port and the ephemeral range count.
    """
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if host not in _LOCAL_ORIGIN_HOSTS:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port in _NOT_LOCAL_PORTS:
        return False
    return port >= 1024


#: Hostnames a browser reaches through a literal loopback address, historically.
_LOCAL_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

#: A browser asking to run code from another origin must prove it can read the
#: answer first. That is the preflight, and it is what a forged form POST and a
#: ``no-cors`` request cannot produce.
_BROWSER_MARKERS = ("sec-fetch-mode", "sec-fetch-site", "origin", "access-control-request-method")


@dataclass(slots=True)
class ServerConfig:
    """How :func:`serve` should behave."""

    host: str = "127.0.0.1"
    port: int = 8787
    require_key: bool = True
    verbose: bool = False
    read_only: bool = False
    allow_shell: bool = False

    def validate(self) -> None:
        """Refuse the one configuration that hands the agent to the network.

        Requests are served with approval forced to ``auto`` — nobody is at a
        terminal to confirm anything — so an unauthenticated gateway on a
        non-loopback interface is remote control of this workspace, billed to
        your provider account. SECURITY.md warned about that combination; this
        is where it is actually refused.

        Raises:
            ConfigurationError: if the server would be reachable without a key, or
                if a read-only server is configured with shell access.
        """
        if self.read_only and self.allow_shell:
            raise ConfigurationError("A read-only gateway cannot enable shell access.")
        if self.require_key or is_loopback_host(self.host):
            return
        raise ConfigurationError(
            f"Refusing to serve {self.host or 'all interfaces'} without authentication: "
            "anyone who can reach this port would control the agent, with approvals "
            "forced to auto.\n"
            "  Create a key:  jaigent keys new my-app\n"
            "  Or stay local: jaigent serve --no-auth   (binds 127.0.0.1)"
        )


class _Handler(BaseHTTPRequestHandler):
    """Implements the slice of the OpenAI API that SDKs actually need."""

    server_version = "jAIgent"
    agent_factory: Any = None
    config: ServerConfig = ServerConfig()

    # -- helpers -------------------------------------------------------
    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        origin = self._cors_origin()
        if origin:
            self.send_header("access-control-allow-origin", origin)
        if "origin" in self.headers:
            self.send_header("vary", "origin")
        self.end_headers()
        self.wfile.write(raw)

    def _cors_origin(self) -> str:
        """The ``access-control-allow-origin`` value for this request, or ``""``.

        Never ``*``. A wildcard let any page on the internet send requests to a
        loopback gateway through the victim's browser: the page cannot read the
        reply, but it does not need to — the agent runs, the file is written,
        the command executes. Local only, so this is cross-site request forgery
        against a developer's machine.

        The allowed origins are:

        * nothing, when the gateway is bound to a non-loopback interface (a
          key is required there anyway, and a browser should not be a client);
        * a non-browser client, which sends no ``Origin`` at all — unaffected;
        * the gateway's own origin, or a loopback origin on an ephemeral port,
          for a local browser UI.

        That last one is safe against a rogue *local* page, which shares the
        loopback address and would otherwise be allowed: it must issue a real
        preflight (``do_OPTIONS``) and carry the key, and no page can add
        ``Authorization`` to a simple request without one.
        """
        if not is_loopback_host(self.config.host):
            return ""
        origin = (self.headers.get("origin") or "").strip().rstrip("/")
        if not origin:
            return ""  # curl, the SDKs, server-to-server: not a browser
        if origin in self._own_origins():
            return origin
        if _origin_is_local(origin):
            return origin
        return ""

    def _own_origins(self) -> set[str]:
        """The origins this gateway is actually served on."""
        server_port = getattr(self.server, "server_port", None) if self.server else None
        port = server_port or self.config.port
        return {
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
            f"http://[::1]:{port}",
        }

    def _from_a_browser(self) -> bool:
        """Whether this request carries the fingerprints of a browser page."""
        return any(self.headers.get(header) is not None for header in _BROWSER_MARKERS)

    def _origin_allowed(self) -> bool:
        """Reject a browser request from an origin that is not ours.

        This runs before the route is dispatched, so a cross-site page cannot
        reach *anything* — not even ``/health`` — and never learns whether the
        port is live.
        """
        if "origin" not in self.headers:
            return True
        return bool(self._cors_origin())

    def _host_allowed(self) -> bool:
        """Refuse a ``Host`` header that is not this machine.

        DNS rebinding defeats an address check: ``evil.example`` resolves to
        ``127.0.0.1`` and the browser then treats the gateway as same-origin
        with the attacker's page. The one thing the attacker cannot rewrite is
        the ``Host`` the browser sends, which still names their domain.
        """
        host = (self.headers.get("host") or "").strip().lower()
        if not host:
            return True  # HTTP/1.0 client with no host; not a browser
        name = host
        # Strip the port, honouring bracketed IPv6 literals.
        if name.startswith("["):
            name = name.split("]")[0].lstrip("[")
        elif ":" in name:
            name = name.rsplit(":", 1)[0]
        name = name.rstrip(".")
        if name in {
            "localhost",
            "localhost.localdomain",
            "127.0.0.1",
            "::1",
            "0.0.0.0",  # nosec B104 - a comparison, not a bind: "all interfaces"
        }:
            return True
        if is_loopback_host(self.config.host):
            # A loopback gateway is also reached as 127.x.y.z or via a
            # hostname in /etc/hosts; anything else is a rebinding attempt.
            try:
                return ipaddress.ip_address(name).is_loopback
            except ValueError:
                return name in {"localhost"}
        return True

    def _error(self, status: int, message: str, kind: str = "invalid_request_error") -> None:
        self._send(status, {"error": {"message": message, "type": kind}})

    def _authorised(self) -> bool:
        if not self.config.require_key:
            return True
        header = self.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return verify_key(token) is not None

    # -- routes --------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802 - required by the base class
        """Answer a CORS preflight — the one thing a hostile page cannot fake.

        A preflight is what tells the browser whether to send the real request.
        Answering it only for an allowed origin, and only for the headers the
        gateway actually needs, is what keeps ``Authorization`` out of reach of
        a page that has no business calling this port.
        """
        if not self._host_allowed():
            self._error(403, "Host header does not name this machine.")
            return
        origin = self._cors_origin()
        self.send_response(204)
        if origin:
            self.send_header("access-control-allow-origin", origin)
            self.send_header("vary", "origin")
            self.send_header("access-control-allow-headers", "authorization, content-type")
            self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
            self.send_header("access-control-max-age", "600")
        self.send_header("content-length", "0")
        self.end_headers()

    def _guarded(self) -> bool:
        """Run the request-level checks every route shares.

        Returns False when a response has already been sent. The gateway is
        reachable by whatever runs on this machine, so the checks that protect
        it — ``Host``, ``Origin``, and a browser's requirement to preflight —
        must happen exactly once, here, instead of in each of the three verbs.
        """
        if not self._host_allowed():
            self._error(403, "Host header does not name this machine.", "invalid_request_error")
            return False
        if not self._origin_allowed():
            self._error(403, "Cross-origin requests are not allowed.", "invalid_request_error")
            return False
        # A browser that reached a real route without a preflight used a
        # "simple" request: a forged form POST or a no-cors fetch. Both are
        # hostile by construction; a same-origin page is the only browser
        # caller allowed through, and it got here via do_OPTIONS.
        if self._from_a_browser() and self.headers.get("sec-fetch-mode") not in {None, "cors"}:
            self._error(403, "Preflight required for browser requests.", "invalid_request_error")
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._guarded():
            return
        path = self.path.split("?")[0].rstrip("/")

        if path in {"/health", "/v1/health"}:
            self._send(200, {"status": "ok", "service": "jAIgent"})
            return

        if path in {"/v1/models", "/models"}:
            if not self._authorised():
                self._error(401, "Invalid or missing API key.", "authentication_error")
                return
            from jaigent.models import CATALOGUE

            listed = [
                {"id": "auto", "object": "model", "owned_by": "jAIgent"},
                *({"id": m.id, "object": "model", "owned_by": m.provider} for m in CATALOGUE),
            ]
            self._send(
                200,
                {
                    "object": "list",
                    "data": listed,
                    # A jAIgent extension used by trusted local adapters to make
                    # capability decisions without probing tools or adding a
                    # parallel endpoint. OpenAI-compatible clients can ignore it.
                    "jaigent": {
                        "read_only": self.config.read_only,
                        "shell_enabled": self.config.allow_shell,
                    },
                },
            )
            return

        self._error(404, f"Unknown path {path}. Try /v1/chat/completions.")

    def do_POST(self) -> None:  # noqa: N802
        if not self._guarded():
            return
        path = self.path.split("?")[0].rstrip("/")
        if path not in {"/v1/chat/completions", "/chat/completions"}:
            self._error(404, f"Unknown path {path}. Try /v1/chat/completions.")
            return

        if not self._authorised():
            self._error(
                401,
                "Invalid or missing API key. Create one with `jaigent keys new`.",
                "authentication_error",
            )
            return

        try:
            length = int(self.headers.get("content-length", 0))
        except ValueError:
            self._error(400, "Request body must be JSON.")
            return
        if length < 0:
            self._error(400, "Request body must be JSON.")
            return
        # A lying Content-Length otherwise parks this thread in read()
        # until the client goes away; eight megabytes is plenty for a prompt.
        if length > 8_000_000:
            self._error(413, "Request body is too large (over 8 MB).")
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._error(400, "Request body must be JSON.")
            return
        if not isinstance(body, dict):
            self._error(400, "Request body must be a JSON object.")
            return

        messages = body.get("messages") or []
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
            # Used to crash the handler (`m.get` on a string) and drop the
            # connection with an empty reply; now it is a plain 400.
            self._error(400, "`messages` must be a list of {role, content} objects.")
            return
        # Content arrives as a string, null, or a list of blocks — never assume.
        # str() on those shapes used to feed the model garbage like "None" or
        # "[{'type': 'text', ...}]" as the prompt.
        prompt = next(
            (text_content(m.get("content")) for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        if not prompt.strip():
            self._error(400, "No user message found in `messages`.")
            return

        # `stream` arrived from JSON, so it can be anything: a string "false"
        # must not mean "on" and a client that omits it must get the old JSON.
        wants_stream = body.get("stream") is True

        system = "\n\n".join(
            text_content(m.get("content")) for m in messages if m.get("role") == "system"
        )
        prior = [
            {"role": str(m.get("role")), "content": text_content(m.get("content"))}
            for m in messages
            if m.get("role") in {"user", "assistant"} and text_content(m.get("content")).strip()
        ]
        if prior and prior[-1]["role"] == "user":
            prior = prior[:-1]

        try:
            requested_model = body.get("model")
            agent = self.agent_factory(
                model=requested_model if isinstance(requested_model, str) else None,
                instructions=system or None,
            )
            if prior:
                agent.load_history(prior)
        except Exception as exc:  # noqa: BLE001 - must become a clean HTTP error
            self._error(502, f"Agent run failed: {exc}", "api_error")
            return

        if wants_stream:
            options = body.get("stream_options")
            include_usage = isinstance(options, dict) and bool(options.get("include_usage"))
            self._stream_completion(agent, prompt, include_usage=include_usage)
            return

        try:
            result = agent.run(prompt)
        except Exception as exc:  # noqa: BLE001 - must become a clean HTTP error
            self._error(502, f"Agent run failed: {exc}", "api_error")
            return

        self._send(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": agent.settings.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.output},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": result.cost.input_tokens,
                    "completion_tokens": result.cost.output_tokens,
                    "total_tokens": result.cost.total_tokens,
                },
                "jaigent": {
                    "tool_calls": result.tool_calls,
                    "tools_used": [step.tool for step in result.steps],
                    "estimated_usd": result.cost.usd,
                },
            },
        )

    # -- streaming -----------------------------------------------------
    def _stream_completion(self, agent: Any, prompt: str, *, include_usage: bool) -> None:
        """Answer as Server-Sent Events, the way OpenAI clients expect.

        ``stream: true`` used to be parsed and then ignored: every client — the
        OpenAI SDK, Open WebUI, a LangChain callback — got one JSON body after a
        long silence and reported a broken stream. Text now arrives as the model
        produces it.

        The agent loop is blocking, so it runs on a worker thread and the
        response thread drains a queue. That keeps this module free of async:
        the same :class:`Agent` drives a terminal turn and an HTTP turn.

        Byte-identical deltas are not guaranteed: providers chunk differently
        and this server does not re-split text. The sequence, roles, finish
        reason and ``[DONE]`` sentinel are the parts clients rely on.

        A failure after the headers are sent cannot become an HTTP error code,
        so it is reported as a final ``error`` event — exactly the shape the
        non-streaming path uses — followed by ``[DONE]``.
        """
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        model = agent.settings.model or ""
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        agent.on_text = lambda text: events.put(("text", text))

        def worker() -> None:
            try:
                result = agent.run(prompt)
            except BaseException as exc:  # noqa: BLE001 - reported, then stream ends
                events.put(("error", str(exc) or type(exc).__name__))
                return
            events.put(("done", result))

        def send(payload: dict[str, Any]) -> None:
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        def delta(content: str | None = None, **extra: Any) -> dict[str, Any]:
            body: dict[str, Any] = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content} if content is not None else {},
                        "finish_reason": None,
                    }
                ],
            }
            body.update(extra)
            return body

        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("x-accel-buffering", "no")
        # The reply ends when this handler returns and the connection closes,
        # so a client that reads until EOF never hangs.
        self.send_header("connection", "close")
        origin = self._cors_origin()
        if origin:
            self.send_header("access-control-allow-origin", origin)
        if "origin" in self.headers:
            self.send_header("vary", "origin")
        self.end_headers()

        thread = threading.Thread(target=worker, name="jaigent-stream", daemon=True)
        thread.start()

        try:
            # The opening chunk carries the role, as OpenAI's does; clients
            # that assemble messages need it before any content arrives.
            opening = delta()
            opening["choices"][0]["delta"] = {"role": "assistant", "content": ""}
            send(opening)

            while True:
                kind, payload = events.get()
                if kind == "text":
                    send(delta(payload))
                    continue
                if kind == "error":
                    send({"error": {"message": payload, "type": "api_error"}})
                    break
                result = payload
                final = delta()
                final["choices"][0]["finish_reason"] = "length" if result.stopped_early else "stop"
                final["jaigent"] = {
                    "tool_calls": result.tool_calls,
                    "tools_used": [step.tool for step in result.steps],
                    "estimated_usd": result.cost.usd,
                }
                send(final)
                if include_usage:
                    send(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [],
                            "usage": {
                                "prompt_tokens": result.cost.input_tokens,
                                "completion_tokens": result.cost.output_tokens,
                                "total_tokens": result.cost.total_tokens,
                            },
                        }
                    )
                break
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The caller hung up. Its provider tokens are already spent, but
            # writing into a closed socket must not raise out of the handler.
            self.close_connection = True

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.config.verbose:
            super().log_message(fmt, *args)


def build_server(agent_factory: Any, config: ServerConfig) -> ThreadingHTTPServer:
    """Create the HTTP server without starting it.

    Raises:
        ConfigurationError: if the configuration would expose the agent, or if
            authentication is required and no key exists to authenticate with.
    """
    config.validate()
    if config.require_key and not [k for k in load_keys() if not k.revoked]:
        raise ConfigurationError(
            "No API keys exist yet, so nothing could authenticate.\n"
            "  Create one:  jaigent keys new my-app\n"
            "  Or run without auth (local only):  jaigent serve --no-auth"
        )

    handler = type(
        "BoundHandler",
        (_Handler,),
        {"agent_factory": staticmethod(agent_factory), "config": config},
    )
    server = ThreadingHTTPServer((config.host, config.port), handler)
    # Threads are non-daemon by default, which parked the process until every
    # in-flight request finished: Ctrl-C during a long agent run printed
    # "stopped" and then hung for minutes.
    server.daemon_threads = True
    return server
