"""The OpenAI-compatible gateway: key management and HTTP behaviour."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import FakeProvider
from jaigent import gateway
from jaigent.agent import Agent
from jaigent.config import Settings
from jaigent.errors import ConfigurationError
from jaigent.gateway import (
    KEY_PREFIX,
    ServerConfig,
    build_server,
    create_key,
    hash_key,
    load_keys,
    revoke_key,
    verify_key,
)
from jaigent.llm.base import AssistantMessage, ToolCall


@pytest.fixture(autouse=True)
def isolated_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "keys.json"
    monkeypatch.setenv("JAIGENT_KEYS_FILE", str(path))
    return path


class TestKeys:
    def test_create_returns_the_secret_once(self) -> None:
        key = create_key("my-app")

        assert key.secret is not None
        assert key.secret.startswith(KEY_PREFIX)
        assert key.name == "my-app"

    def test_secret_is_never_written_to_disk(self, isolated_keys: Path) -> None:
        key = create_key("app")
        raw = isolated_keys.read_text(encoding="utf-8")

        assert key.secret not in raw
        assert hash_key(key.secret or "") in raw

    def test_secret_is_absent_after_reload(self) -> None:
        create_key("app")
        assert all(k.secret is None for k in load_keys())

    def test_keys_are_unique(self) -> None:
        assert create_key("a").secret != create_key("b").secret

    def test_verify_accepts_a_valid_key(self) -> None:
        key = create_key("app")
        assert verify_key(key.secret or "") is not None

    def test_verify_rejects_nonsense(self) -> None:
        create_key("app")
        assert verify_key("jgt-not-a-real-key") is None

    def test_verify_rejects_empty(self) -> None:
        assert verify_key("") is None

    def test_verify_records_usage(self) -> None:
        key = create_key("app")
        verify_key(key.secret or "")
        verify_key(key.secret or "")

        stored = load_keys()[0]
        assert stored.calls == 2
        assert stored.last_used > 0

    def test_revoked_keys_stop_working(self) -> None:
        key = create_key("app")
        revoke_key(key.id)

        assert verify_key(key.secret or "") is None

    def test_revoke_by_name(self) -> None:
        create_key("by-name")
        assert revoke_key("by-name") is not None

    def test_revoke_unknown(self) -> None:
        assert revoke_key("nope") is None

    def test_preview_hides_the_secret(self) -> None:
        key = create_key("app")
        assert key.secret not in key.preview

    def test_corrupt_store_is_survivable(self, isolated_keys: Path) -> None:
        isolated_keys.write_text("{broken", encoding="utf-8")
        assert load_keys() == []


class TestServer:
    """Exercised over real HTTP against a loopback socket."""

    @pytest.fixture
    def server(self, tmp_path: Path):  # noqa: ANN201
        def factory(model: str | None = None, instructions: str | None = None) -> Agent:
            settings = Settings(
                provider="openai",
                model=model or "gpt-4o-mini",
                api_key="k",
                workspace=tmp_path,
            )
            return Agent(
                settings,
                provider=FakeProvider(
                    [
                        AssistantMessage(tool_calls=[ToolCall("c", "list_files", {})]),
                        AssistantMessage(
                            content="all done",
                            usage={"prompt_tokens": 100, "completion_tokens": 20},
                        ),
                    ]
                ),
            )

        create_key("test")
        httpd = build_server(factory, ServerConfig(host="127.0.0.1", port=0))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()
        httpd.server_close()

    def _post(self, base: str, payload: dict, key: str | None = None):  # noqa: ANN202
        request = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )
        if key:
            request.add_header("authorization", f"Bearer {key}")
        return urllib.request.urlopen(request, timeout=10)

    def test_health_needs_no_auth(self, server) -> None:  # noqa: ANN001
        _, base = server
        with urllib.request.urlopen(f"{base}/health", timeout=10) as response:
            assert json.loads(response.read())["status"] == "ok"

    def test_missing_key_is_401(self, server) -> None:  # noqa: ANN001
        _, base = server
        with pytest.raises(urllib.error.HTTPError) as exc:
            self._post(base, {"messages": [{"role": "user", "content": "hi"}]})

        assert exc.value.code == 401

    def test_wrong_key_is_401(self, server) -> None:  # noqa: ANN001
        _, base = server
        with pytest.raises(urllib.error.HTTPError) as exc:
            self._post(base, {"messages": [{"role": "user", "content": "hi"}]}, key="jgt-wrong")

        assert exc.value.code == 401

    def test_valid_key_runs_the_agent(self, server) -> None:  # noqa: ANN001
        _, base = server
        key = create_key("caller").secret
        with self._post(base, {"messages": [{"role": "user", "content": "hi"}]}, key=key) as r:
            body = json.loads(r.read())

        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "all done"
        assert body["usage"]["total_tokens"] == 120

    def test_response_reports_tools_used(self, server) -> None:  # noqa: ANN001
        _, base = server
        key = create_key("caller").secret
        with self._post(base, {"messages": [{"role": "user", "content": "hi"}]}, key=key) as r:
            body = json.loads(r.read())

        assert body["jaigent"]["tool_calls"] == 1
        assert body["jaigent"]["tools_used"] == ["list_files"]

    def test_empty_messages_is_400(self, server) -> None:  # noqa: ANN001
        _, base = server
        key = create_key("caller").secret
        with pytest.raises(urllib.error.HTTPError) as exc:
            self._post(base, {"messages": []}, key=key)

        assert exc.value.code == 400

    def test_models_endpoint(self, server) -> None:  # noqa: ANN001
        _, base = server
        key = create_key("caller").secret
        request = urllib.request.Request(f"{base}/v1/models")
        request.add_header("authorization", f"Bearer {key}")
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read())

        ids = [m["id"] for m in body["data"]]
        assert "auto" in ids
        assert len(ids) > 5

    def test_unknown_path_is_404(self, server) -> None:  # noqa: ANN001
        _, base = server
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/v1/nope", timeout=10)

        assert exc.value.code == 404

    def test_system_message_is_forwarded(self, server, tmp_path: Path) -> None:  # noqa: ANN001
        _, base = server
        key = create_key("caller").secret
        payload = {
            "messages": [
                {"role": "system", "content": "Answer in Catalan."},
                {"role": "user", "content": "hi"},
            ]
        }
        with self._post(base, payload, key=key) as r:
            assert json.loads(r.read())["choices"][0]["message"]["content"] == "all done"


class TestServerConfig:
    def test_refuses_to_start_without_keys(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="No API keys"):
            build_server(lambda **kw: None, ServerConfig(port=0))

    def test_no_auth_mode_starts_without_keys(self, tmp_path: Path) -> None:
        httpd = build_server(lambda **kw: None, ServerConfig(port=0, require_key=False))
        httpd.server_close()


def test_keys_path_follows_jaigent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JAIGENT_KEYS_FILE", raising=False)
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "h"))
    assert gateway.keys_path() == tmp_path / "h" / "keys.json"
