"""Persistent ChatGPT sessions reuse jAIgent's existing session store and gateway."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from jaigent.chatgpt import ChatGPTConfig, TokenValidation, build_server
from jaigent.config import Settings
from jaigent.session import Session, load


class FakeGateway:
    def __init__(self, config: ChatGPTConfig) -> None:
        self.config = config
        self.calls: list[tuple[list[dict[str, str]], str]] = []
        self._calls_guard = threading.Lock()
        self.first_call_entered = threading.Event()
        self.release_first_call = threading.Event()
        self.block_first_call = False
        self.closed = False

    def check_policy(self) -> dict[str, bool]:
        return {"read_only": False, "shell_enabled": False}

    def capabilities(self) -> tuple[dict[str, bool], list[str]]:
        return {"read_only": False, "shell_enabled": False}, ["auto"]

    def status(self) -> str:
        return json.dumps({"status": "ok", "models": ["auto"]})

    def chat(self, messages: list[dict[str, str]], model: str = "auto") -> str:
        with self._calls_guard:
            self.calls.append(([dict(message) for message in messages], model))
            call_number = len(self.calls)
        if self.block_first_call and call_number == 1:
            self.first_call_entered.set()
            if not self.release_first_call.wait(timeout=5):
                raise TimeoutError("test did not release the first gateway call")
        return f"answer {call_number}"

    def close(self) -> None:
        self.closed = True


class FakeVerifier:
    def verify(self, token: str | None) -> TokenValidation:
        return TokenValidation(token == "test-token")

    def close(self) -> None:
        pass


@pytest.fixture
def remote_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, FakeGateway, Path]]:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("JAIGENT_SESSION_DIR", str(session_dir))
    config = ChatGPTConfig(
        host="127.0.0.1",
        port=0,
        public_url="https://mcp.example.test",
        oauth_issuer="https://identity.example.test",
        oauth_audience="https://mcp.example.test",
        gateway_key="jgt-" + "k" * 40,
        workspace=tmp_path,
        allow_write=True,
    )
    gateway = FakeGateway(config)
    server = build_server(
        config,
        gateway=gateway,  # type: ignore[arg-type]
        verifier=FakeVerifier(),  # type: ignore[arg-type]
        settings=Settings(workspace=tmp_path, skills_enabled=False, plugins_enabled=False),
    )
    try:
        yield server, gateway, session_dir
    finally:
        server.server_close()
        server.plugin_service.close()


def _call(server: Any, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    response = json.loads(server.plugin_service.mcp.handle_jsonrpc(request))
    return response["result"]


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def test_sessions_are_isolated_listed_and_saved_in_the_shared_store(
    remote_server: tuple[Any, FakeGateway, Path],
) -> None:
    server, gateway, _ = remote_server
    first = json.loads(_text(_call(server, "jaigent_session_start", {"title": "Research"})))
    second = json.loads(_text(_call(server, "jaigent_session_start", {"title": "Draft"})))
    first_id = first["session_id"]
    second_id = second["session_id"]
    assert first_id != second_id

    _call(server, "jaigent_session_chat", {"session_id": first_id, "message": "first topic"})
    _call(server, "jaigent_session_chat", {"session_id": second_id, "message": "second topic"})
    _call(server, "jaigent_session_chat", {"session_id": first_id, "message": "follow up"})

    assert gateway.calls == [
        ([{"role": "user", "content": "first topic"}], "auto"),
        ([{"role": "user", "content": "second topic"}], "auto"),
        (
            [
                {"role": "user", "content": "first topic"},
                {"role": "assistant", "content": "answer 1"},
                {"role": "user", "content": "follow up"},
            ],
            "auto",
        ),
    ]
    assert load(first_id).source == "chatgpt"  # type: ignore[union-attr]
    assert load(first_id).turns == 2  # type: ignore[union-attr]

    cli_session = Session.new(model="gpt-4o-mini", workspace=str(Path.cwd()))
    cli_session.title = "Private CLI chat"
    cli_session.save()
    listed = json.loads(_text(_call(server, "jaigent_sessions_list")))
    listed_ids = {item["session_id"] for item in listed["sessions"]}
    assert {first_id, second_id} <= listed_ids
    assert cli_session.id not in listed_ids
    assert all("messages" not in item for item in listed["sessions"])


def test_remote_session_ids_are_validated_and_cannot_open_cli_sessions(
    remote_server: tuple[Any, FakeGateway, Path],
) -> None:
    server, _, _ = remote_server
    cli_session = Session.new(model="gpt-4o-mini", workspace=str(Path.cwd()))
    cli_session.save()

    invalid = _call(
        server,
        "jaigent_session_chat",
        {"session_id": "../../outside", "message": "do not load this"},
    )
    assert invalid["isError"] is True

    cross_origin = _call(
        server,
        "jaigent_session_chat",
        {"session_id": cli_session.id, "message": "do not read this"},
    )
    assert cross_origin["isError"] is True
    denied_delete = _call(server, "jaigent_session_delete", {"session_id": cli_session.id})
    assert denied_delete["isError"] is True
    assert load(cli_session.id) is not None


def test_concurrent_calls_to_the_same_session_are_serialized(
    remote_server: tuple[Any, FakeGateway, Path],
) -> None:
    server, gateway, _ = remote_server
    gateway.block_first_call = True
    started = json.loads(_text(_call(server, "jaigent_session_start")))
    session_id = started["session_id"]
    first_request = {
        "session_id": session_id,
        "message": "first concurrent message",
    }
    second_request = {
        "session_id": session_id,
        "message": "second concurrent message",
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_call, server, "jaigent_session_chat", first_request)
        assert gateway.first_call_entered.wait(timeout=2)
        second_started = threading.Event()

        def call_second() -> dict[str, Any]:
            second_started.set()
            return _call(server, "jaigent_session_chat", second_request)

        second = pool.submit(call_second)
        assert second_started.wait(timeout=2)
        gateway.release_first_call.set()
        first.result(timeout=3)
        second.result(timeout=3)

    assert gateway.calls[1][0] == [
        {"role": "user", "content": "first concurrent message"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "second concurrent message"},
    ]
    assert load(session_id).turns == 2  # type: ignore[union-attr]
