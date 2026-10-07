"""OAuth-protected ChatGPT MCP integration tests."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import generate_key

from jaigent.chatgpt import (
    RESOURCE_METADATA_PATH,
    ChatGPTConfig,
    GatewayClient,
    OAuthTokenVerifier,
    TokenValidation,
    build_server,
)
from jaigent.checkpoint import CheckpointStore
from jaigent.config import Settings
from jaigent.errors import ConfigurationError, ToolError


@pytest.fixture
def config() -> ChatGPTConfig:
    return ChatGPTConfig(
        host="127.0.0.1",
        port=0,
        public_url="https://mcp.example.test",
        oauth_issuer="https://identity.example.test/tenant",
        oauth_audience="https://mcp.example.test",
        gateway_base_url="http://127.0.0.1:8787/v1",
        gateway_key="jgt-" + "x" * 40,
    )


class FakeGateway:
    def __init__(
        self,
        config: ChatGPTConfig,
        *,
        read_only: bool = True,
        shell_enabled: bool = False,
    ) -> None:
        self.config = config
        self.read_only = read_only
        self.shell_enabled = shell_enabled
        self.calls: list[tuple[list[dict[str, str]], str]] = []
        self.closed = False

    def check_policy(self) -> dict[str, bool]:
        if self.shell_enabled:
            raise ConfigurationError("shell must be disabled")
        if not self.read_only and not self.config.allow_write:
            raise ConfigurationError("write opt-in required")
        return {"read_only": self.read_only, "shell_enabled": self.shell_enabled}

    def capabilities(self) -> tuple[dict[str, bool], list[str]]:
        return (
            {"read_only": self.read_only, "shell_enabled": self.shell_enabled},
            ["auto", "gpt-4o-mini"],
        )

    def status(self) -> str:
        return json.dumps({"status": "ok", "models": ["auto", "gpt-4o-mini"]})

    def chat(self, messages: Any, model: Any = "auto") -> str:
        self.calls.append((messages, model))
        return "jAIgent response"

    def close(self) -> None:
        self.closed = True


class FakeVerifier:
    def __init__(self) -> None:
        self.closed = False

    def verify(self, token: str | None) -> TokenValidation:
        if token == "valid-token":
            return TokenValidation(True)
        if token is None:
            return TokenValidation(False, "insufficient_scope")
        return TokenValidation(False, "invalid_token")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def served(config: ChatGPTConfig) -> Iterator[tuple[str, Any, FakeGateway, FakeVerifier]]:
    gateway = FakeGateway(config)
    verifier = FakeVerifier()
    server = build_server(config, gateway=gateway, verifier=verifier)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, server, gateway, verifier
    finally:
        server.shutdown()
        server.server_close()
        server.plugin_service.close()
        thread.join(timeout=5)


def _rpc(
    method: str, params: dict[str, Any] | None = None, *, request_id: int = 1
) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _post(
    base: str,
    message: dict[str, Any],
    *,
    token: str | None = None,
    origin: str | None = None,
    accept: str = "application/json, text/event-stream",
) -> httpx.Response:
    headers = {"Accept": accept, "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    return httpx.post(f"{base}/mcp", json=message, headers=headers, timeout=5)


class TestConfig:
    def test_gateway_key_is_never_in_repr(self, config: ChatGPTConfig) -> None:
        assert config.gateway_key not in repr(config)

    def test_default_resource_url_matches_ipv6_loopback_listener(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JAIGENT_PLUGIN_PUBLIC_URL", raising=False)
        monkeypatch.setenv("JAIGENT_PLUGIN_PORT", "8788")
        config = ChatGPTConfig.from_env(host="::1")
        assert config.public_url == "http://[::1]:8788"

    def test_provider_key_is_not_a_gateway_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_API_KEY", "provider-secret")
        monkeypatch.setenv("JAIGENT_PLUGIN_PUBLIC_URL", "https://mcp.example.test")
        monkeypatch.setenv("JAIGENT_PLUGIN_OAUTH_ISSUER", "https://identity.example.test")
        monkeypatch.setenv("JAIGENT_PLUGIN_GATEWAY_URL", "http://127.0.0.1:8787/v1")
        monkeypatch.delenv("JAIGENT_PLUGIN_GATEWAY_KEY", raising=False)
        with pytest.raises(ConfigurationError, match="JAIGENT_PLUGIN_GATEWAY_KEY"):
            ChatGPTConfig.from_env().validate()

    def test_listener_must_remain_on_loopback(self, config: ChatGPTConfig) -> None:
        config.host = "0.0.0.0"
        with pytest.raises(ConfigurationError, match="loopback"):
            config.validate()

    def test_remote_public_url_requires_https(self, config: ChatGPTConfig) -> None:
        config.public_url = "http://mcp.example.test"
        with pytest.raises(ConfigurationError, match="HTTPS"):
            config.validate()

    def test_public_url_rejects_header_injection_characters(self, config: ChatGPTConfig) -> None:
        config.public_url = 'https://mcp.example.test", error="invalid_token'
        with pytest.raises(ConfigurationError, match="quotes"):
            config.validate()

    def test_gateway_url_must_be_local_http_or_remote_https(self, config: ChatGPTConfig) -> None:
        config.gateway_base_url = "http://gateway.example.test/v1"
        with pytest.raises(ConfigurationError, match="HTTPS"):
            config.validate()

    def test_gateway_url_must_target_existing_v1_routes(self, config: ChatGPTConfig) -> None:
        config.gateway_base_url = "http://127.0.0.1:8787/v2"
        with pytest.raises(ConfigurationError, match="end in `/v1`"):
            config.validate()

    def test_remote_tools_use_the_configured_workspace(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plugin_workspace = tmp_path / "plugin"
        monkeypatch.setenv("JAIGENT_PLUGIN_WORKSPACE", str(plugin_workspace))
        config = ChatGPTConfig.from_env()
        assert config.workspace == plugin_workspace.resolve()

        cli_workspace = tmp_path / "cli"
        config = ChatGPTConfig.from_env(workspace=cli_workspace)
        assert config.workspace == cli_workspace.resolve()

        config.gateway_key = "jgt-" + "x" * 40
        config.workspace = tmp_path / "missing"
        with pytest.raises(ConfigurationError, match="existing directory"):
            config.validate()

    def test_remote_resource_requires_https_issuer_and_audience(
        self, config: ChatGPTConfig
    ) -> None:
        config.oauth_issuer = "http://127.0.0.1:9000"
        with pytest.raises(ConfigurationError, match="HTTPS OAuth issuer"):
            config.validate()

        config.oauth_issuer = "https://identity.example.test/tenant"
        config.oauth_audience = "http://127.0.0.1/resource"
        with pytest.raises(ConfigurationError, match="Remote service URLs must use HTTPS"):
            config.validate()


class TestOAuthTokenVerifier:
    @pytest.fixture
    def issuer(self, config: ChatGPTConfig):  # noqa: ANN201
        private_key = generate_key("RSA", 2048, auto_kid=True)
        jwks = {"keys": [private_key.as_dict(private=False)]}
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if str(request.url) == (
                "https://identity.example.test/tenant/.well-known/openid-configuration"
            ):
                return httpx.Response(
                    200,
                    json={
                        "issuer": config.oauth_issuer,
                        "jwks_uri": "https://identity.example.test/keys",
                    },
                )
            if str(request.url) == "https://identity.example.test/keys":
                return httpx.Response(200, json=jwks)
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        verifier = OAuthTokenVerifier(config, client=client)
        yield private_key, verifier, calls
        client.close()

    def _token(self, config: ChatGPTConfig, key: Any, **overrides: Any) -> str:
        claims: dict[str, Any] = {
            "iss": config.oauth_issuer,
            "aud": config.oauth_audience,
            "sub": "user-123",
            "exp": int(time.time()) + 120,
            "scope": f"openid {config.oauth_scope}",
        }
        claims.update(overrides)
        return jwt.encode(
            {"alg": "RS256", "kid": key.kid},
            claims,
            key,
            algorithms=["RS256"],
        )

    def test_accepts_a_signed_jwt_with_issuer_audience_expiry_and_scope(
        self, config: ChatGPTConfig, issuer: Any
    ) -> None:
        key, verifier, calls = issuer
        result = verifier.verify(self._token(config, key))
        assert result == TokenValidation(True)
        assert calls.count("https://identity.example.test/keys") == 1

    @pytest.mark.parametrize(
        ("claims", "expected_error"),
        [
            ({"iss": "https://attacker.example"}, "invalid_token"),
            ({"aud": "https://other.example"}, "invalid_token"),
            ({"exp": int(time.time()) - 1}, "invalid_token"),
            ({"scope": "openid profile"}, "insufficient_scope"),
        ],
    )
    def test_rejects_tokens_with_invalid_claims(
        self,
        config: ChatGPTConfig,
        issuer: Any,
        claims: dict[str, Any],
        expected_error: str,
    ) -> None:
        key, verifier, _ = issuer
        token = self._token(config, key, **claims)
        result = verifier.verify(token)
        assert not result.valid
        assert result.error == expected_error

    def test_rejects_missing_malformed_and_oversized_tokens(
        self, config: ChatGPTConfig, issuer: Any
    ) -> None:
        _, verifier, _ = issuer
        assert verifier.verify(None) == TokenValidation(False, "insufficient_scope")
        assert not verifier.verify("not-a-jwt").valid
        assert not verifier.verify("x" * 9_000).valid

    def test_rejects_a_token_signed_by_an_untrusted_key(
        self, config: ChatGPTConfig, issuer: Any
    ) -> None:
        _, verifier, _ = issuer
        attacker_key = generate_key("RSA", 2048, auto_kid=True)
        token = self._token(config, attacker_key)
        assert not verifier.verify(token).valid


class TestMCPHTTP:
    def test_protected_resource_metadata_is_public_and_points_to_the_issuer(
        self, served: tuple[str, Any, FakeGateway, FakeVerifier], config: ChatGPTConfig
    ) -> None:
        base, _, _, _ = served
        response = httpx.get(base + RESOURCE_METADATA_PATH, timeout=5)
        assert response.status_code == 200
        metadata = response.json()
        assert metadata["resource"] == config.public_url
        assert metadata["authorization_servers"] == [config.oauth_issuer]
        assert metadata["scopes_supported"] == [config.oauth_scope]

    def test_tool_list_is_anonymous_but_every_tool_declares_oauth(
        self, served: tuple[str, Any, FakeGateway, FakeVerifier]
    ) -> None:
        base, _, _, _ = served
        response = _post(base, _rpc("tools/list"))
        assert response.status_code == 200
        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        assert {"jaigent_chat", "jaigent_status", "jaigent_sessions_list"} <= set(tools)
        assert {"list_files", "read_file", "search_files", "web_search", "fetch_page"} <= set(tools)
        assert "run_command" not in tools
        assert "ask_user" not in tools
        assert "write_file" not in tools
        assert "jaigent_session_start" not in tools
        expected_security = [{"type": "oauth2", "scopes": ["jaigent:agent"]}]
        assert all(tool["securitySchemes"] == expected_security for tool in tools.values())
        assert all(tool["_meta"]["securitySchemes"] == expected_security for tool in tools.values())
        assert tools["jaigent_chat"]["annotations"]["readOnlyHint"] is True
        assert tools["jaigent_chat"]["annotations"]["destructiveHint"] is False

    def test_write_tools_require_bridge_opt_in_and_a_writable_gateway(
        self, config: ChatGPTConfig, tmp_path: Any
    ) -> None:
        config.workspace = tmp_path
        config.allow_write = True
        gateway = FakeGateway(config, read_only=False)
        verifier = FakeVerifier()
        server = build_server(config, gateway=gateway, verifier=verifier)  # type: ignore[arg-type]
        try:
            raw = server.plugin_service.mcp.handle_jsonrpc(_rpc("tools/list"))
            tools = {tool["name"]: tool for tool in json.loads(raw)["result"]["tools"]}
            assert {"write_file", "edit_file", "delete_file"} <= set(tools)
            assert {
                "jaigent_session_start",
                "jaigent_session_chat",
                "jaigent_session_delete",
            } <= set(tools)
            assert "run_command" not in tools
            assert tools["write_file"]["annotations"]["readOnlyHint"] is False
            assert tools["jaigent_session_delete"]["annotations"]["destructiveHint"] is True
        finally:
            server.server_close()
            server.plugin_service.close()

    def test_direct_file_write_uses_the_existing_checkpoint_store(
        self, config: ChatGPTConfig, tmp_path: Any
    ) -> None:
        config.workspace = tmp_path
        config.allow_write = True
        gateway = FakeGateway(config, read_only=False)
        verifier = FakeVerifier()
        server = build_server(config, gateway=gateway, verifier=verifier)  # type: ignore[arg-type]
        try:
            request = _rpc(
                "tools/call",
                {
                    "name": "write_file",
                    "arguments": {"path": "remote.txt", "content": "remote write"},
                },
            )
            result = json.loads(server.plugin_service.mcp.handle_jsonrpc(request))["result"]
            assert "Wrote remote.txt" in result["content"][0]["text"]
            assert (tmp_path / "remote.txt").read_text(encoding="utf-8") == "remote write"

            store = CheckpointStore(tmp_path)
            checkpoint = store.latest()
            assert checkpoint is not None
            assert checkpoint.tool == "write_file"
            assert checkpoint.files[0].path == "remote.txt"
            assert not checkpoint.files[0].existed
            assert store.restore(checkpoint) == ["remote.txt"]
            assert not (tmp_path / "remote.txt").exists()
        finally:
            server.server_close()
            server.plugin_service.close()

    def test_project_memory_write_uses_the_existing_checkpoint_store(
        self, config: ChatGPTConfig, tmp_path: Any
    ) -> None:
        config.workspace = tmp_path
        config.allow_write = True
        gateway = FakeGateway(config, read_only=False)
        server = build_server(
            config,
            gateway=gateway,  # type: ignore[arg-type]
            verifier=FakeVerifier(),  # type: ignore[arg-type]
            settings=Settings(
                workspace=tmp_path,
                memory=True,
                checkpoints=True,
                skills_enabled=False,
                plugins_enabled=False,
            ),
        )
        try:
            request = _rpc(
                "tools/call",
                {"name": "remember", "arguments": {"note": "Keep tests offline."}},
            )
            result = json.loads(server.plugin_service.mcp.handle_jsonrpc(request))["result"]
            assert "Remembered" in result["content"][0]["text"]
            memory = tmp_path / ".jaigent" / "memory.md"
            assert memory.read_text(encoding="utf-8") == "Keep tests offline.\n"

            checkpoint = CheckpointStore(tmp_path).latest()
            assert checkpoint is not None
            assert checkpoint.tool == "remember"
            assert checkpoint.files[0].path == ".jaigent/memory.md"
            assert not checkpoint.files[0].existed
            assert CheckpointStore(tmp_path).restore(checkpoint) == [".jaigent/memory.md"]
            assert not memory.exists()
        finally:
            server.server_close()
            server.plugin_service.close()

    def test_refused_secret_write_is_not_checkpointed(
        self, config: ChatGPTConfig, tmp_path: Any
    ) -> None:
        config.workspace = tmp_path
        config.allow_write = True
        gateway = FakeGateway(config, read_only=False)
        server = build_server(
            config,
            gateway=gateway,  # type: ignore[arg-type]
            verifier=FakeVerifier(),  # type: ignore[arg-type]
        )
        try:
            (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
            request = _rpc(
                "tools/call",
                {
                    "name": "write_file",
                    "arguments": {"path": ".env", "content": "overwritten"},
                },
            )
            result = json.loads(server.plugin_service.mcp.handle_jsonrpc(request))["result"]
            assert result["isError"] is True
            assert ".env" in result["content"][0]["text"]
            assert CheckpointStore(tmp_path).history() == []
            assert (tmp_path / ".env").read_text(encoding="utf-8") == "SECRET=value"
        finally:
            server.server_close()
            server.plugin_service.close()

    def test_direct_write_rechecks_gateway_policy_on_each_call(
        self, config: ChatGPTConfig, tmp_path: Any
    ) -> None:
        config.workspace = tmp_path
        config.allow_write = True
        gateway = FakeGateway(config, read_only=False)
        verifier = FakeVerifier()
        server = build_server(config, gateway=gateway, verifier=verifier)  # type: ignore[arg-type]
        try:
            gateway.read_only = True
            request = _rpc(
                "tools/call",
                {
                    "name": "write_file",
                    "arguments": {"path": "blocked.txt", "content": "no"},
                },
            )
            result = json.loads(server.plugin_service.mcp.handle_jsonrpc(request))["result"]
            assert result["isError"] is True
            assert "not writable" in result["content"][0]["text"]
            assert not (tmp_path / "blocked.txt").exists()
        finally:
            server.server_close()
            server.plugin_service.close()

    def test_bridge_write_flag_cannot_override_a_read_only_gateway(
        self, config: ChatGPTConfig
    ) -> None:
        config.allow_write = True
        gateway = FakeGateway(config, read_only=True)
        verifier = FakeVerifier()
        server = build_server(config, gateway=gateway, verifier=verifier)  # type: ignore[arg-type]
        try:
            raw = server.plugin_service.mcp.handle_jsonrpc(_rpc("tools/list"))
            tools = {tool["name"] for tool in json.loads(raw)["result"]["tools"]}
            assert "write_file" not in tools
            assert "jaigent_session_start" not in tools
            assert "jaigent_sessions_list" in tools
        finally:
            server.server_close()
            server.plugin_service.close()

    def test_initialize_advertises_only_tools(
        self, served: tuple[str, Any, FakeGateway, FakeVerifier]
    ) -> None:
        base, _, _, _ = served
        response = _post(base, _rpc("initialize", {"protocolVersion": "2025-11-25"}))
        capabilities = response.json()["result"]["capabilities"]
        assert capabilities == {"tools": {"listChanged": False}}

    def test_missing_token_returns_openai_oauth_challenge(
        self, served: tuple[str, Any, FakeGateway, FakeVerifier]
    ) -> None:
        base, _, _, _ = served
        response = _post(
            base,
            _rpc(
                "tools/call",
                {
                    "name": "jaigent_chat",
                    "arguments": {"messages": [{"role": "user", "content": "hello"}]},
                },
            ),
        )
        result = response.json()["result"]
        assert response.status_code == 200
        assert result["isError"] is True
        challenge = result["_meta"]["mcp/www_authenticate"][0]
        assert 'error="insufficient_scope"' in challenge
        assert "oauth-protected-resource" in challenge
        assert response.headers["WWW-Authenticate"] == challenge

    def test_valid_token_reaches_the_existing_gateway_tool(
        self, served: tuple[str, Any, FakeGateway, FakeVerifier]
    ) -> None:
        base, _, gateway, _ = served
        response = _post(
            base,
            _rpc(
                "tools/call",
                {
                    "name": "jaigent_chat",
                    "arguments": {
                        "messages": [{"role": "user", "content": "hello"}],
                        "model": "auto",
                    },
                },
            ),
            token="valid-token",
        )
        result = response.json()["result"]
        assert result["content"][0]["text"] == "jAIgent response"
        assert "isError" not in result
        assert gateway.calls == [([{"role": "user", "content": "hello"}], "auto")]

    def test_status_tool_uses_the_existing_gateway(
        self, served: tuple[str, Any, FakeGateway, FakeVerifier]
    ) -> None:
        base, _, _, _ = served
        response = _post(
            base,
            _rpc("tools/call", {"name": "jaigent_status", "arguments": {}}),
            token="valid-token",
        )
        result = response.json()["result"]
        assert json.loads(result["content"][0]["text"])["status"] == "ok"

    def test_invalid_token_challenge_never_echoes_the_token(
        self, served: tuple[str, Any, FakeGateway, FakeVerifier]
    ) -> None:
        base, _, _, _ = served
        response = _post(
            base,
            _rpc("tools/call", {"name": "jaigent_status", "arguments": {}}),
            token="secret-token",
        )
        assert response.status_code == 200
        assert "secret-token" not in response.text
        assert (
            'error="invalid_token"' in response.json()["result"]["_meta"]["mcp/www_authenticate"][0]
        )

    def test_rejects_unknown_origin_and_unsupported_transport_requests(
        self, served: tuple[str, Any, FakeGateway, FakeVerifier]
    ) -> None:
        base, _, _, _ = served
        denied = _post(base, _rpc("tools/list"), origin="https://attacker.example")
        assert denied.status_code == 403

        no_sse = httpx.get(base + "/mcp", timeout=5)
        assert no_sse.status_code == 405
        assert no_sse.headers["allow"] == "POST"

        missing_accept = _post(base, _rpc("ping"), accept="application/json")
        assert missing_accept.status_code == 406

    def test_notifications_return_accepted_without_a_body(
        self, served: tuple[str, Any, FakeGateway, FakeVerifier]
    ) -> None:
        base, _, _, _ = served
        response = httpx.post(
            base + "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=5,
        )
        assert response.status_code == 202
        assert response.content == b""


class TestGatewayClient:
    def test_passes_the_internal_key_only_to_the_existing_gateway(
        self, config: ChatGPTConfig
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/models":
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "auto"}],
                        "jaigent": {"read_only": True, "shell_enabled": False},
                    },
                )
            if request.url.path == "/v1/chat/completions":
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "answer"}}]},
                )
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        gateway = GatewayClient(config, client=client)
        assert gateway.chat([{"role": "user", "content": "hello"}]) == "answer"
        assert seen[0].headers["authorization"] == f"Bearer {config.gateway_key}"
        assert seen[1].headers["authorization"] == f"Bearer {config.gateway_key}"
        assert all(config.gateway_key not in request.content.decode() for request in seen)
        client.close()

    def test_explicit_write_opt_in_accepts_a_non_shell_gateway(self, config: ChatGPTConfig) -> None:
        config.allow_write = True

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "auto"}],
                    "jaigent": {"read_only": False, "shell_enabled": False},
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        gateway = GatewayClient(config, client=client)
        gateway.check_policy()
        client.close()

    @pytest.mark.parametrize(
        ("capabilities", "allow_write", "message"),
        [
            (
                {"read_only": True, "shell_enabled": True},
                False,
                "shell access enabled",
            ),
            (
                {"read_only": False, "shell_enabled": False},
                False,
                "permits file changes",
            ),
        ],
    )
    def test_refuses_shell_and_unapproved_write_access(
        self,
        config: ChatGPTConfig,
        capabilities: dict[str, bool],
        allow_write: bool,
        message: str,
    ) -> None:
        config.allow_write = allow_write

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": [{"id": "auto"}], "jaigent": capabilities},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        gateway = GatewayClient(config, client=client)
        with pytest.raises(ConfigurationError, match=message):
            gateway.check_policy()
        client.close()

    def test_502_details_and_gateway_key_are_not_forwarded(self, config: ChatGPTConfig) -> None:
        secret = config.gateway_key

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "auto"}],
                        "jaigent": {"read_only": True, "shell_enabled": False},
                    },
                )
            return httpx.Response(502, json={"error": {"message": f"provider-key {secret}"}})

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        gateway = GatewayClient(config, client=client)
        with pytest.raises(ToolError) as error:
            gateway.chat([{"role": "user", "content": "hello"}])
        assert secret not in str(error.value)
        assert "provider-key" not in str(error.value)
        client.close()
