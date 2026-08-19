"""An OpenAI-compatible HTTP endpoint backed by the agent.

``jaigent serve`` exposes your agent at ``http://localhost:8787/v1``. Point any
OpenAI SDK at it, authenticate with a jaigent key, and every request runs
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
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jaigent import paths
from jaigent.errors import ConfigurationError
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


def create_key(name: str = "default") -> APIKey:
    """Mint a key. The plain text is returned once and never stored."""
    secret = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
    key = APIKey(
        id=uuid.uuid4().hex,
        name=name.strip() or "default",
        hashed=hash_key(secret),
        secret=secret,
    )
    keys = load_keys()
    keys.append(key)
    save_keys(keys)
    return key


def revoke_key(identifier: str) -> APIKey | None:
    """Revoke by id or name. Returns the key that was revoked."""
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
@dataclass(slots=True)
class ServerConfig:
    """How :func:`serve` should behave."""

    host: str = "127.0.0.1"
    port: int = 8787
    require_key: bool = True
    verbose: bool = False


class _Handler(BaseHTTPRequestHandler):
    """Implements the slice of the OpenAI API that SDKs actually need."""

    server_version = "jaigent"
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
        self.end_headers()
        self.wfile.write(raw)

    def _cors_origin(self) -> str:
        """Allow browser clients only when bound to loopback."""
        host = (self.config.host or "").strip().lower()
        if host in {"127.0.0.1", "localhost", "::1"}:
            return "*"
        return ""

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
        self.send_response(204)
        origin = self._cors_origin()
        if origin:
            self.send_header("access-control-allow-origin", origin)
        self.send_header("access-control-allow-headers", "authorization, content-type")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")

        if path in {"/health", "/v1/health"}:
            self._send(200, {"status": "ok", "service": "jaigent"})
            return

        if path in {"/v1/models", "/models"}:
            if not self._authorised():
                self._error(401, "Invalid or missing API key.", "authentication_error")
                return
            from jaigent.models import CATALOGUE

            listed = [
                {"id": "auto", "object": "model", "owned_by": "jaigent"},
                *({"id": m.id, "object": "model", "owned_by": m.provider} for m in CATALOGUE),
            ]
            self._send(200, {"object": "list", "data": listed})
            return

        self._error(404, f"Unknown path {path}. Try /v1/chat/completions.")

    def do_POST(self) -> None:  # noqa: N802
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
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._error(400, "Request body must be JSON.")
            return

        messages = body.get("messages") or []
        prompt = next(
            (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        if not prompt.strip():
            self._error(400, "No user message found in `messages`.")
            return

        system = "\n\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "system"
        )
        prior = [
            {"role": str(m.get("role")), "content": str(m.get("content", ""))}
            for m in messages
            if m.get("role") in {"user", "assistant"} and str(m.get("content", "")).strip()
        ]
        if prior and prior[-1]["role"] == "user":
            prior = prior[:-1]

        try:
            agent = self.agent_factory(model=body.get("model"), instructions=system or None)
            if prior:
                agent.load_history(prior)
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

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.config.verbose:
            super().log_message(fmt, *args)


def build_server(agent_factory: Any, config: ServerConfig) -> ThreadingHTTPServer:
    """Create the HTTP server without starting it."""
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
    return ThreadingHTTPServer((config.host, config.port), handler)
