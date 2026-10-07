"""Inbound ChatGPT integration over OAuth-protected MCP Streamable HTTP.

ChatGPT connects to this server. The bridge publishes jAIgent's existing
MCP tool registry plus persistent session actions; full agent runs continue
through the authenticated OpenAI-compatible gateway. It does not create a
second agent or forward provider credentials.

The HTTP listener is deliberately loopback-only. Put a TLS reverse proxy in
front of it for remote ChatGPT access.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from jaigent.approval import MUTATING_TOOLS
from jaigent.chatgpt_sessions import build_session_tools
from jaigent.checkpoint import CheckpointStore, paths_for_tool
from jaigent.config import Settings
from jaigent.errors import ConfigurationError, ToolError
from jaigent.gateway import is_loopback_host
from jaigent.mcp import MCP_SUPPORTED_VERSIONS, MCPServer
from jaigent.tools import Tool, build_default_registry
from jaigent.tools.sandbox import refuse_if_blocked, resolve_in_workspace

LOGGER = logging.getLogger(__name__)

MCP_PATH = "/mcp"
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
OAUTH_SCOPE = "jaigent:agent"
CHAT_TOOL_NAME = "jaigent_chat"
STATUS_TOOL_NAME = "jaigent_status"
MAX_HTTP_BODY_BYTES = 1_000_000
MAX_TOKEN_CHARS = 8_192
MAX_MESSAGES = 64
MAX_MESSAGE_CHARS = 100_000
JWKS_CACHE_SECONDS = 300
JWKS_REFRESH_COOLDOWN_SECONDS = 15

#: OpenAI's web clients may send an Origin header. Local MCP inspectors can
#: also connect from loopback when the endpoint itself is local.
_OPENAI_ORIGINS = frozenset({"https://chatgpt.com", "https://chat.openai.com"})
_JWT_ALGORITHMS = ("RS256", "PS256", "ES256", "ES384", "EdDSA")


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_http_url(value: str, *, allow_loopback_http: bool) -> SplitResult:
    """Parse a URL and reject credentials, query strings and fragments."""
    if any(char.isspace() or ord(char) < 0x20 or char in {'"', "\\"} for char in value):
        raise ConfigurationError("URLs must not contain whitespace, quotes or backslashes.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("The URL is malformed or contains an invalid port.") from exc
    if parsed.scheme not in {"https", "http"} or not hostname:
        raise ConfigurationError("A complete HTTP(S) URL is required.")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("URLs must not contain embedded credentials.")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("URLs must not contain a query string or fragment.")
    if parsed.scheme == "http" and not (allow_loopback_http and is_loopback_host(hostname)):
        raise ConfigurationError("Remote service URLs must use HTTPS.")
    if port is not None and not 1 <= port <= 65_535:
        raise ConfigurationError("The URL port must be between 1 and 65535.")
    return parsed


@dataclass(slots=True)
class ChatGPTConfig:
    """Settings for the inbound ChatGPT MCP bridge.

    ``gateway_key`` is intentionally excluded from reprs and errors. It is a
    jAIgent gateway key, not a provider key such as ``JAIGENT_API_KEY``.
    """

    host: str = "127.0.0.1"
    port: int = 8788
    public_url: str = ""
    oauth_issuer: str = ""
    oauth_audience: str = ""
    gateway_base_url: str = "http://127.0.0.1:8787/v1"
    gateway_key: str = field(default="", repr=False)
    workspace: Path = field(default_factory=Path.cwd)
    allow_write: bool = False
    verbose: bool = False

    @property
    def oauth_scope(self) -> str:
        return OAUTH_SCOPE

    @classmethod
    def from_env(
        cls,
        *,
        host: str | None = None,
        port: int | None = None,
        workspace: str | Path | None = None,
        allow_write: bool | None = None,
        verbose: bool | None = None,
    ) -> ChatGPTConfig:
        """Load configuration without ever falling back to a provider key."""
        resolved_host = (
            host if host is not None else os.getenv("JAIGENT_PLUGIN_HOST", "127.0.0.1")
        ).strip()
        raw_port = os.getenv("JAIGENT_PLUGIN_PORT", "8788")
        if port is None:
            try:
                resolved_port = int(raw_port)
            except ValueError as exc:
                raise ConfigurationError("JAIGENT_PLUGIN_PORT must be an integer.") from exc
        else:
            resolved_port = port

        public_url = os.getenv("JAIGENT_PLUGIN_PUBLIC_URL", "").strip().rstrip("/")
        if not public_url and is_loopback_host(resolved_host):
            url_host = resolved_host.strip("[]")
            if ":" in url_host:
                url_host = f"[{url_host}]"
            public_url = f"http://{url_host}:{resolved_port}"

        return cls(
            host=resolved_host,
            port=resolved_port,
            public_url=public_url,
            oauth_issuer=os.getenv("JAIGENT_PLUGIN_OAUTH_ISSUER", "").strip(),
            oauth_audience=os.getenv("JAIGENT_PLUGIN_OAUTH_AUDIENCE", "").strip() or public_url,
            gateway_base_url=os.getenv(
                "JAIGENT_PLUGIN_GATEWAY_URL", "http://127.0.0.1:8787/v1"
            ).strip(),
            gateway_key=os.getenv("JAIGENT_PLUGIN_GATEWAY_KEY", "").strip(),
            workspace=Path(
                workspace
                if workspace is not None
                else os.getenv(
                    "JAIGENT_PLUGIN_WORKSPACE",
                    os.getenv("JAIGENT_WORKSPACE", str(Path.cwd())),
                )
            )
            .expanduser()
            .resolve(),
            allow_write=(
                allow_write if allow_write is not None else _env_flag("JAIGENT_PLUGIN_ALLOW_WRITE")
            ),
            verbose=verbose if verbose is not None else _env_flag("JAIGENT_PLUGIN_VERBOSE"),
        )

    def validate(self) -> None:
        """Validate transport, OAuth and private gateway settings."""
        if not self.host or not is_loopback_host(self.host):
            raise ConfigurationError(
                "The ChatGPT MCP listener serves plain HTTP and must bind to loopback "
                "(127.0.0.1). Put a TLS reverse proxy in front for remote access."
            )
        if not 0 <= self.port <= 65_535:
            raise ConfigurationError("The ChatGPT MCP port must be between 0 and 65535.")
        if not self.gateway_key.startswith("jgt-") or len(self.gateway_key) < 20:
            raise ConfigurationError(
                "Set JAIGENT_PLUGIN_GATEWAY_KEY to a private key created with `jaigent keys new`. "
                "Do not use JAIGENT_API_KEY here."
            )
        self.workspace = Path(self.workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ConfigurationError(
                "The ChatGPT tool workspace must be an existing directory. "
                "Set JAIGENT_PLUGIN_WORKSPACE or use `jaigent chatgpt --workspace`."
            )

        gateway = _safe_http_url(self.gateway_base_url, allow_loopback_http=True)
        if gateway.path.rstrip("/") != "/v1":
            raise ConfigurationError("JAIGENT_PLUGIN_GATEWAY_URL must end in `/v1`.")

        public = _safe_http_url(self.public_url, allow_loopback_http=True)
        self.public_url = self.public_url.rstrip("/")
        if not self.oauth_audience:
            self.oauth_audience = self.public_url
        if public.path not in {"", "/"}:
            raise ConfigurationError(
                "JAIGENT_PLUGIN_PUBLIC_URL must be the public HTTPS origin, without `/mcp`."
            )
        if public.scheme != "https" and not is_loopback_host(public.hostname or ""):
            raise ConfigurationError("The public ChatGPT MCP URL must use HTTPS.")

        if not self.oauth_issuer:
            raise ConfigurationError(
                "Set JAIGENT_PLUGIN_OAUTH_ISSUER to the exact OAuth/OIDC issuer URL."
            )
        issuer = _safe_http_url(self.oauth_issuer, allow_loopback_http=True)
        if public.scheme == "https" and issuer.scheme != "https":
            raise ConfigurationError("A remote ChatGPT MCP server requires an HTTPS OAuth issuer.")
        if not self.oauth_audience or any(ch.isspace() for ch in self.oauth_audience):
            raise ConfigurationError("JAIGENT_PLUGIN_OAUTH_AUDIENCE must be a non-empty URI.")
        local_resource = public.scheme == "http" and is_loopback_host(public.hostname or "")
        audience = _safe_http_url(
            self.oauth_audience,
            allow_loopback_http=local_resource,
        )
        if public.scheme == "https" and audience.scheme != "https":
            raise ConfigurationError("JAIGENT_PLUGIN_OAUTH_AUDIENCE must use HTTPS remotely.")


class GatewayClient:
    """Authenticated client for the existing jAIgent gateway."""

    def __init__(
        self,
        config: ChatGPTConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.Client(
                timeout=httpx.Timeout(180.0, connect=5.0),
                follow_redirects=False,
            )
        )
        self._base = config.gateway_base_url.rstrip("/")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str) -> dict[str, Any]:
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {self.config.gateway_key}",
        }
        try:
            response = self._client.request(method, f"{self._base}/{path}", headers=headers)
        except httpx.HTTPError as exc:
            raise ToolError(
                "Could not reach the configured jAIgent gateway. Check that `jaigent serve` "
                "is running and JAIGENT_PLUGIN_GATEWAY_URL is correct."
            ) from exc

        if response.status_code in {401, 403}:
            raise ToolError(
                "The jAIgent gateway rejected its private key. Create or rotate a key with "
                "`jaigent keys new`, then update JAIGENT_PLUGIN_GATEWAY_KEY."
            )
        if response.status_code >= 500:
            raise ToolError(
                "The jAIgent gateway could not complete the request. Check its local logs; "
                "the upstream error details were not forwarded."
            )
        if response.status_code >= 400:
            raise ToolError(
                "The jAIgent gateway rejected the request. Check its URL and configuration."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolError("The jAIgent gateway returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise ToolError("The jAIgent gateway returned an invalid response.")
        return payload

    def capabilities(self) -> tuple[dict[str, bool], list[str]]:
        """Fetch the existing authenticated model endpoint and safety flags."""
        payload = self._request("GET", "models")
        reported = payload.get("jaigent")
        if not isinstance(reported, dict):
            raise ToolError(
                "This jAIgent gateway does not advertise its safety capabilities. "
                "Update jAIgent on both processes before connecting ChatGPT."
            )
        read_only = reported.get("read_only")
        shell_enabled = reported.get("shell_enabled")
        if not isinstance(read_only, bool) or not isinstance(shell_enabled, bool):
            raise ToolError(
                "This jAIgent gateway returned invalid safety capabilities. "
                "Update jAIgent on both processes before connecting ChatGPT."
            )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ToolError("The jAIgent gateway returned an invalid model catalogue.")
        model_ids = [str(item["id"]) for item in rows if isinstance(item, dict) and item.get("id")]
        return {"read_only": read_only, "shell_enabled": shell_enabled}, model_ids

    def check_policy(self) -> dict[str, bool]:
        """Fail closed before the public MCP endpoint starts serving requests."""
        try:
            capabilities, _ = self.capabilities()
        except ToolError as exc:
            raise ConfigurationError(str(exc)) from exc
        if capabilities["shell_enabled"]:
            raise ConfigurationError(
                "The configured jAIgent gateway has shell access enabled. The ChatGPT bridge "
                "never connects to a shell-capable gateway. Restart it without `--allow-shell` "
                "or use `jaigent serve --read-only`."
            )
        if not capabilities["read_only"] and not self.config.allow_write:
            raise ConfigurationError(
                "The configured jAIgent gateway permits file changes. For the safe default, "
                "restart it with `jaigent serve --read-only`. To explicitly allow ChatGPT "
                "to expose ChatGPT write tools, start this bridge with `--allow-write`."
            )
        return capabilities

    def status(self) -> str:
        capabilities, model_ids = self.capabilities()
        return json.dumps(
            {
                "status": "ok",
                "models": model_ids,
                "model_catalogue_is_advisory": True,
                "read_only": capabilities["read_only"],
                "shell_enabled": capabilities["shell_enabled"],
                "write_access_enabled": (
                    not capabilities["read_only"]
                    and not capabilities["shell_enabled"]
                    and self.config.allow_write
                ),
            },
            ensure_ascii=False,
        )

    def chat(self, messages: Any, model: Any = "auto") -> str:
        """Run a request through the same Chat Completions gateway as other clients."""
        normalized = _normalize_messages(messages)
        if not isinstance(model, str) or len(model) > 200:
            raise ToolError("`model` must be a string of at most 200 characters.")
        model_name = model.strip() or "auto"

        capabilities, _ = self.capabilities()
        if capabilities["shell_enabled"]:
            raise ToolError(
                "Remote ChatGPT requests are disabled because the jAIgent gateway has shell "
                "access enabled. Restart the gateway without `--allow-shell` or with `--read-only`."
            )
        if not capabilities["read_only"] and not self.config.allow_write:
            raise ToolError(
                "Remote file changes are blocked by default. Restart the gateway with "
                "`--read-only`, or explicitly enable them with `jaigent chatgpt --allow-write`."
            )

        body = {"model": model_name, "messages": normalized}
        headers = {
            "authorization": f"Bearer {self.config.gateway_key}",
            "content-type": "application/json",
            "accept": "application/json",
        }
        try:
            response = self._client.post(
                f"{self._base}/chat/completions",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ToolError(
                "Could not reach the configured jAIgent gateway. Check that `jaigent serve` "
                "is running."
            ) from exc

        if response.status_code in {401, 403}:
            raise ToolError(
                "The jAIgent gateway rejected its private key. Check JAIGENT_PLUGIN_GATEWAY_KEY."
            )
        if response.status_code >= 500:
            raise ToolError(
                "jAIgent could not complete the agent run. Check the gateway's local logs; "
                "provider or exception details were not forwarded."
            )
        if response.status_code >= 400:
            raise ToolError(
                "The jAIgent gateway rejected the chat request. Check the model and messages."
            )
        try:
            payload = response.json()
            answer = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ToolError("The jAIgent gateway returned an invalid chat response.") from exc
        if not isinstance(answer, str):
            raise ToolError("The jAIgent gateway returned a non-text chat response.")
        return answer


def _normalize_messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > MAX_MESSAGES:
        raise ToolError(f"`messages` must be a non-empty list of at most {MAX_MESSAGES} messages.")
    result: list[dict[str, str]] = []
    total = 0
    has_user = False
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ToolError("Each message must contain only `role` and `content`.")
        role, content = item.get("role"), item.get("content")
        if not isinstance(role, str) or role not in {"system", "user", "assistant"}:
            raise ToolError("Message roles must be `system`, `user` or `assistant`.")
        if not isinstance(content, str):
            raise ToolError("Every message `content` value must be text.")
        total += len(content)
        if total > MAX_MESSAGE_CHARS:
            raise ToolError(
                f"The combined message text must be at most {MAX_MESSAGE_CHARS:,} characters."
            )
        has_user = has_user or role == "user"
        result.append({"role": role, "content": content})
    if not has_user:
        raise ToolError("At least one `user` message is required.")
    return result


_CHATGPT_TOOL_NAMES = frozenset(
    {
        STATUS_TOOL_NAME,
        CHAT_TOOL_NAME,
        "jaigent_sessions_list",
        "jaigent_session_start",
        "jaigent_session_chat",
        "jaigent_session_delete",
    }
)


def _tool_settings(settings: Settings | None, workspace: Path) -> Settings:
    """Copy only local-tool configuration; never pass provider credentials on."""
    source = settings or Settings(workspace=workspace)
    return Settings(
        workspace=workspace,
        timeout=source.timeout,
        search_backend=source.search_backend,
        search_api_key=source.search_api_key,
        skills_enabled=source.skills_enabled,
        plugins_enabled=source.plugins_enabled,
        allow_shell=False,
        memory=source.memory,
        checkpoints=source.checkpoints,
    )


def _guard_remote_tool(
    tool: Tool,
    gateway: GatewayClient,
    checkpoint_store: CheckpointStore | None,
    write_lock: threading.Lock,
) -> Tool:
    """Recheck write policy and checkpoint direct file mutations before calling."""
    func = tool.func

    def run(**arguments: Any) -> str:
        if not tool.read_only or tool.dangerous:
            try:
                capabilities = gateway.check_policy()
            except ConfigurationError as exc:
                raise ToolError(str(exc)) from exc
            if (
                capabilities.get("read_only", True)
                or capabilities.get("shell_enabled", True)
                or not gateway.config.allow_write
            ):
                raise ToolError(
                    "This operation is blocked because the jAIgent gateway is not writable. "
                    "Use a writable gateway and explicitly enable bridge writes."
                )
        with write_lock:
            if checkpoint_store is not None:
                if tool.name in MUTATING_TOOLS:
                    targets = paths_for_tool(tool.name, arguments)
                    safe_targets = [
                        resolve_in_workspace(checkpoint_store.workspace, target)
                        for target in targets
                    ]
                elif tool.name == "remember":
                    # Project memory is a known persistent write even though it
                    # is not an ordinary file-tool operation in MUTATING_TOOLS.
                    safe_targets = [
                        resolve_in_workspace(
                            checkpoint_store.workspace,
                            Path(".jaigent") / "memory.md",
                        )
                    ]
                else:
                    safe_targets = []
                for target in safe_targets:
                    refuse_if_blocked(checkpoint_store.workspace, target)
                if safe_targets:
                    checkpoint_store.capture(
                        safe_targets,
                        label=f"{tool.name} {safe_targets[0]}",
                        tool=tool.name,
                    )
            return func(**arguments)

    return replace(tool, func=run)


def build_tools(
    gateway: GatewayClient,
    scope: str = OAUTH_SCOPE,
    *,
    settings: Settings | None = None,
    capabilities: dict[str, bool] | None = None,
) -> list[Tool]:
    """Compose existing jAIgent tools with OAuth-protected ChatGPT actions."""
    del scope  # Retained for compatibility with earlier callers; auth is per MCP tool.
    if capabilities is None:
        capabilities, _ = gateway.capabilities()
    read_only = capabilities.get("read_only")
    shell_enabled = capabilities.get("shell_enabled")
    if not isinstance(read_only, bool) or not isinstance(shell_enabled, bool):
        raise ToolError("The jAIgent gateway returned invalid safety capabilities.")
    can_write = bool(gateway.config.allow_write and not read_only and not shell_enabled)

    tool_settings = _tool_settings(settings, Path(gateway.config.workspace))
    registry = build_default_registry(tool_settings, interactive=False)
    collisions = _CHATGPT_TOOL_NAMES.intersection(registry.names())
    if collisions:
        raise ConfigurationError(
            "A local jAIgent tool uses a reserved ChatGPT MCP name: "
            f"{', '.join(sorted(collisions))}. Rename or disable that plugin, then restart."
        )

    direct_tools = [
        registry.get(name) for name in registry.names() if name not in {"run_command", "ask_user"}
    ]
    checkpoint_store = (
        CheckpointStore(tool_settings.workspace)
        if can_write and tool_settings.checkpoints
        else None
    )
    write_lock = threading.Lock()
    session_tools = build_session_tools(
        gateway,
        workspace=str(tool_settings.workspace),
        max_messages=MAX_MESSAGES,
        max_message_chars=MAX_MESSAGE_CHARS,
    )
    status_tool = Tool(
        name=STATUS_TOOL_NAME,
        description=(
            "Check the connected jAIgent gateway and list its model catalogue. "
            "The catalogue is advisory; actual model availability depends on the "
            "provider credentials configured on the jAIgent host."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        func=lambda: gateway.status(),
        read_only=True,
    )
    chat_tool = Tool(
        name=CHAT_TOOL_NAME,
        description=(
            "Send a conversation to the existing jAIgent agent through its local gateway. "
            "jAIgent uses its own configured provider and tools; it may change files only "
            "when the gateway permits writes and the owner explicitly enabled them for "
            "ChatGPT. Keep provider credentials on the jAIgent host."
        ),
        parameters={
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_MESSAGES,
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "enum": ["system", "user", "assistant"],
                            },
                            "content": {
                                "type": "string",
                                "maxLength": MAX_MESSAGE_CHARS,
                            },
                        },
                        "required": ["role", "content"],
                        "additionalProperties": False,
                    },
                },
                "model": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "jAIgent model id; use `auto` for jAIgent's normal routing.",
                    "default": "auto",
                },
            },
            "required": ["messages"],
            "additionalProperties": False,
        },
        func=lambda messages, model="auto": gateway.chat(messages, model),
        dangerous=not read_only,
        read_only=read_only,
    )
    candidates = [*direct_tools, *session_tools, status_tool, chat_tool]
    guarded = [
        _guard_remote_tool(tool, gateway, checkpoint_store, write_lock)
        if not tool.read_only or tool.dangerous or tool.name in MUTATING_TOOLS
        else tool
        for tool in candidates
    ]
    return [tool for tool in guarded if (tool.read_only and not tool.dangerous) or can_write]


@dataclass(slots=True, frozen=True)
class TokenValidation:
    """A minimal, non-sensitive OAuth validation result."""

    valid: bool
    error: str = "invalid_token"


class TokenVerificationError(Exception):
    """Internal token/discovery failure; never returned verbatim to ChatGPT."""


class OAuthTokenVerifier:
    """Validate JWT access tokens using the configured issuer's OIDC/JWKS data."""

    def __init__(
        self,
        config: ChatGPTConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=5.0, follow_redirects=False)
        self._lock = threading.Lock()
        self._key_set: Any = None
        self._expires_at = 0.0
        self._last_refresh = 0.0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def verify(self, token: str | None) -> TokenValidation:
        if not token:
            return TokenValidation(False, "insufficient_scope")
        if len(token) > MAX_TOKEN_CHARS:
            return TokenValidation(False, "invalid_token")
        try:
            return self._verify(token)
        except Exception:  # noqa: BLE001 - verification details must not reach the client
            return TokenValidation(False, "invalid_token")

    def _verify(self, token: str) -> TokenValidation:
        from joserfc import jwt
        from joserfc.errors import JoseError

        header = _jwt_header(token)
        algorithm = header.get("alg")
        if algorithm not in _JWT_ALGORITHMS:
            return TokenValidation(False, "invalid_token")

        key_set = self._load_key_set()
        kid = header.get("kid")
        if isinstance(kid, str):
            try:
                key_set.get_by_kid(kid)
            except JoseError:
                key_set = self._load_key_set(force=True)

        decoded = jwt.decode(token, key_set, algorithms=_JWT_ALGORITHMS)
        claims = decoded.claims
        registry = jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": self.config.oauth_issuer},
            aud={"essential": True, "value": self.config.oauth_audience},
            exp={"essential": True},
        )
        registry.validate(claims)
        if not _has_scope(claims, self.config.oauth_scope):
            return TokenValidation(False, "insufficient_scope")
        return TokenValidation(True)

    def _load_key_set(self, *, force: bool = False) -> Any:
        now = time.monotonic()
        with self._lock:
            if self._key_set is not None and now < self._expires_at and not force:
                return self._key_set
            if (
                force
                and self._key_set is not None
                and now - self._last_refresh < JWKS_REFRESH_COOLDOWN_SECONDS
            ):
                return self._key_set

            metadata = self._fetch_metadata()
            jwks_url = metadata.get("jwks_uri")
            if not isinstance(jwks_url, str):
                raise TokenVerificationError("The issuer metadata has no JWKS URI.")
            issuer_host = urlsplit(self.config.oauth_issuer).hostname or ""
            jwks_parsed = _safe_http_url(
                jwks_url,
                allow_loopback_http=is_loopback_host(issuer_host),
            )
            if jwks_parsed.scheme != "https" and not is_loopback_host(issuer_host):
                raise TokenVerificationError("The issuer JWKS URI is not HTTPS.")
            try:
                response = self._client.get(jwks_url)
                response.raise_for_status()
                jwks = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise TokenVerificationError("The issuer JWKS could not be loaded.") from exc
            if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
                raise TokenVerificationError("The issuer returned an invalid JWKS.")

            from joserfc.jwk import KeySet

            self._key_set = KeySet.import_key_set(cast(Any, jwks))
            self._expires_at = now + JWKS_CACHE_SECONDS
            self._last_refresh = now
            return self._key_set

    def _fetch_metadata(self) -> dict[str, Any]:
        issuer = urlsplit(self.config.oauth_issuer)
        issuer_path = issuer.path.rstrip("/")
        oidc_path = f"{issuer_path}/.well-known/openid-configuration"
        oauth_path = f"/.well-known/oauth-authorization-server{issuer_path}"
        candidates = (
            urlunsplit((issuer.scheme, issuer.netloc, oidc_path, "", "")),
            urlunsplit((issuer.scheme, issuer.netloc, oauth_path, "", "")),
        )
        for url in candidates:
            try:
                response = self._client.get(url)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                document = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            if (
                isinstance(document, dict)
                and document.get("issuer") == self.config.oauth_issuer
                and isinstance(document.get("jwks_uri"), str)
            ):
                return document
        raise TokenVerificationError("The configured OAuth issuer has no valid discovery metadata.")


def _jwt_header(token: str) -> dict[str, Any]:
    segments = token.split(".")
    if len(segments) != 3:
        raise TokenVerificationError("The access token is not a compact signed JWT.")
    segment = segments[0]
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    header = json.loads(raw)
    if not isinstance(header, dict):
        raise TokenVerificationError("The JWT header is invalid.")
    return header


def _has_scope(claims: dict[str, Any], required: str) -> bool:
    value = claims.get("scope", claims.get("scp", ""))
    if isinstance(value, str):
        scopes = set(value.split())
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        scopes = set(value)
    else:
        return False
    return required in scopes


class ChatGPTHTTPServer(ThreadingHTTPServer):
    """Loopback-only HTTP listener with its resources available for shutdown."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        config: ChatGPTConfig,
        plugin_service: PluginService,
    ) -> None:
        super().__init__(address, handler)
        self.config = config
        self.plugin_service = plugin_service


class PluginService:
    """Shared MCP dispatcher and service dependencies for HTTP requests."""

    def __init__(
        self,
        config: ChatGPTConfig,
        gateway: GatewayClient,
        verifier: OAuthTokenVerifier,
        *,
        settings: Settings | None = None,
        capabilities: dict[str, bool] | None = None,
    ) -> None:
        self.config = config
        self.gateway = gateway
        self.verifier = verifier
        if capabilities is None:
            capabilities, _ = gateway.capabilities()
        self.capabilities = capabilities
        self.can_write = bool(
            config.allow_write
            and not capabilities.get("read_only", True)
            and not capabilities.get("shell_enabled", True)
        )
        self.settings = _tool_settings(settings, config.workspace)
        tools = build_tools(
            gateway,
            config.oauth_scope,
            settings=self.settings,
            capabilities=capabilities,
        )
        security = {
            tool.name: [{"type": "oauth2", "scopes": [config.oauth_scope]}] for tool in tools
        }
        self.mcp = MCPServer(
            self.settings,
            allow_write=self.can_write,
            client="chatgpt",
            tools=tools,
            expose_resources=False,
            expose_prompts=False,
            instructions=(
                "You are connected to the user's existing jAIgent gateway and workspace. "
                "Use jAIgent's existing web, file and utility tools directly for individual "
                "actions; use jaigent_chat to delegate a complete task to the existing agent. "
                "Use jaigent_session_start and jaigent_session_chat for independent persistent "
                "conversations. These tools reuse the user's existing jAIgent logic; no second "
                "agent or provider credentials are exposed. File mutations and session changes "
                "are available only when the owner explicitly enabled writes and the gateway "
                "allows them. run_command is never exposed."
            ),
            security_schemes=security,
        )

    def close(self) -> None:
        self.gateway.close()
        self.verifier.close()

    def auth_challenge(self, error: str) -> str:
        if error not in {"invalid_token", "insufficient_scope"}:
            error = "invalid_token"
        if error == "insufficient_scope":
            description = "Connect or re-authorize with the required jAIgent permission."
        else:
            description = "The access token is invalid or expired; re-authorize and try again."
        return (
            f'Bearer resource_metadata="{self.config.public_url}{RESOURCE_METADATA_PATH}", '
            f'scope="{self.config.oauth_scope}", error="{error}", '
            f'error_description="{description}"'
        )


def build_server(
    config: ChatGPTConfig,
    *,
    gateway: GatewayClient | None = None,
    verifier: OAuthTokenVerifier | None = None,
    settings: Settings | None = None,
) -> ChatGPTHTTPServer:
    """Build, validate and return the inbound MCP server without starting it."""
    config.validate()
    try:
        import joserfc  # noqa: F401 - optional dependency, checked before listening
    except ImportError as exc:
        raise ConfigurationError(
            "The ChatGPT MCP endpoint needs the optional JWT verifier. Install it with "
            '`pip install "jaigent[chatgpt]"`. '
        ) from exc

    gateway_client = gateway if gateway is not None else GatewayClient(config)
    token_verifier = verifier if verifier is not None else OAuthTokenVerifier(config)
    try:
        capabilities = gateway_client.check_policy()
        service = PluginService(
            config,
            gateway_client,
            token_verifier,
            settings=settings,
            capabilities=capabilities,
        )
        server = ChatGPTHTTPServer(
            (config.host, config.port),
            _ChatGPTHandler,
            config=config,
            plugin_service=service,
        )
    except Exception:
        gateway_client.close()
        token_verifier.close()
        raise
    return server


class _ChatGPTHandler(BaseHTTPRequestHandler):
    """Stateless Streamable HTTP transport for jAIgent's MCP dispatcher."""

    server: ChatGPTHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "jAIgent MCP"
    sys_version = ""

    @property
    def config(self) -> ChatGPTConfig:
        return self.server.config

    @property
    def service(self) -> PluginService:
        return self.server.plugin_service

    def _headers(
        self,
        status: int,
        *,
        content_type: str | None = None,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors_headers()

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header(
                "Access-Control-Allow-Headers",
                "authorization, content-type, accept, mcp-protocol-version",
            )
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "WWW-Authenticate")
            self.send_header("Vary", "Origin")

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        extra_headers = headers or {}
        cache_control = extra_headers.get("Cache-Control", "no-store")
        self._headers(
            status,
            content_type="application/json; charset=utf-8",
            cache_control=cache_control,
        )
        for name, value in extra_headers.items():
            if name.lower() != "cache-control":
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_empty(self, status: int, *, headers: dict[str, str] | None = None) -> None:
        self._headers(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _origin_allowed(self, origin: str) -> bool:
        if origin in _OPENAI_ORIGINS:
            return True
        try:
            parsed = urlsplit(origin)
            _ = parsed.port
        except ValueError:
            return False
        public = urlsplit(self.config.public_url)
        if (
            parsed.scheme != "http"
            or parsed.path
            or parsed.query
            or parsed.fragment
            or public.scheme != "http"
            or not public.hostname
            or not is_loopback_host(public.hostname)
        ):
            return False
        return bool(parsed.hostname and is_loopback_host(parsed.hostname))

    def _reject_bad_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None or self._origin_allowed(origin):
            return False
        self._send_json(403, {"error": "Forbidden origin."})
        return True

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_bad_origin():
            return
        path = urlsplit(self.path).path
        if path == RESOURCE_METADATA_PATH:
            self._send_json(
                200,
                {
                    "resource": self.config.public_url,
                    "authorization_servers": [self.config.oauth_issuer],
                    "scopes_supported": [self.config.oauth_scope],
                    "bearer_methods_supported": ["header"],
                },
                headers={"Cache-Control": "public, max-age=300"},
            )
            return
        if path == MCP_PATH:
            self._send_empty(405, headers={"Allow": "POST"})
            return
        self._send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_bad_origin():
            return
        if urlsplit(self.path).path != MCP_PATH:
            self._send_json(404, {"error": "Not found."})
            return

        if not self._accepts_mcp_json():
            self._send_json(406, {"error": "Accept application/json and text/event-stream."})
            return
        if self.headers.get("Transfer-Encoding", "").lower() not in {"", "identity"}:
            self._send_json(400, {"error": "Chunked request bodies are not supported."})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "Content-Type must be application/json."})
            return
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(400, {"error": "A valid Content-Length is required."})
            return
        if length < 0:
            self._send_json(400, {"error": "A valid Content-Length is required."})
            return
        if length > MAX_HTTP_BODY_BYTES:
            self._send_json(413, {"error": "Request body is too large."})
            return
        try:
            raw = self.rfile.read(length)
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Request body must be valid UTF-8 JSON."})
            return
        if isinstance(message, list):
            self._send_json(400, {"error": "Send one MCP JSON-RPC message per POST."})
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._send_json(400, {"error": "Request body must be one JSON-RPC 2.0 message."})
            return

        protocol_version = self.headers.get("MCP-Protocol-Version")
        if protocol_version and protocol_version not in MCP_SUPPORTED_VERSIONS:
            self._send_json(400, {"error": "Unsupported MCP-Protocol-Version."})
            return

        method = message.get("method")
        if method is None and ("result" in message or "error" in message):
            # This server never opens SSE streams or sends server-initiated
            # requests, but accepts protocol responses as required by transport.
            self._send_empty(202)
            return
        if not isinstance(method, str):
            self._send_json(400, {"error": "A JSON-RPC method is required."})
            return

        if method == "tools/call":
            token = _bearer_token(self.headers.get("Authorization", ""))
            validation = self.service.verifier.verify(token)
            if not validation.valid:
                challenge = self.service.auth_challenge(validation.error)
                if message.get("id") is None:
                    self._send_empty(401, headers={"WWW-Authenticate": challenge})
                else:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Authentication is required to use this jAIgent tool. "
                                    "Connect or re-authorize, then try again.",
                                }
                            ],
                            "isError": True,
                            "_meta": {"mcp/www_authenticate": [challenge]},
                        },
                    }
                    self._send_json(
                        200,
                        payload,
                        headers={"WWW-Authenticate": challenge},
                    )
                return

        response = self.service.mcp.handle_jsonrpc(message)
        if response is None:
            self._send_empty(202)
            return
        try:
            payload = json.loads(response)
        except json.JSONDecodeError:
            self._send_json(500, {"error": "The MCP server returned an invalid response."})
            return
        self._send_json(200, payload)

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self._reject_bad_origin():
            return
        self._send_empty(204)

    def do_DELETE(self) -> None:  # noqa: N802
        if self._reject_bad_origin():
            return
        self._send_empty(405, headers={"Allow": "POST"})

    def _accepts_mcp_json(self) -> bool:
        media_types = {
            part.split(";", 1)[0].strip().lower()
            for part in self.headers.get("Accept", "").split(",")
        }
        return {"application/json", "text/event-stream"}.issubset(media_types)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.config.verbose:
            # Never log request bodies, Authorization headers, or query strings.
            LOGGER.info("ChatGPT MCP request %s %s", self.command, urlsplit(self.path).path)


def _bearer_token(header: str) -> str | None:
    scheme, separator, value = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token if token else None
