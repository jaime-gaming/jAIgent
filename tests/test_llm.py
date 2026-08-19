"""Provider request building and response parsing, over mocked HTTP."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jaigent.config import Settings
from jaigent.errors import ConfigurationError, ProviderError
from jaigent.llm import get_provider
from jaigent.llm.anthropic import AnthropicProvider
from jaigent.llm.base import AssistantMessage, ToolCall
from jaigent.llm.openai import OpenAIProvider
from jaigent.tools import build_default_registry


def _patch_post(monkeypatch: pytest.MonkeyPatch, module: Any, handler) -> list[dict]:  # noqa: ANN001
    """Capture outgoing payloads and return canned responses."""
    sent: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, url: str, json: dict | None = None, headers: dict | None = None):  # noqa: A002
            sent.append({"url": url, "json": json, "headers": headers})
            return handler(url, json, headers)

    monkeypatch.setattr(module.httpx, "Client", FakeClient)
    return sent


def _response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "https://api.test/x"))


class TestOpenAIProvider:
    def _provider(self) -> OpenAIProvider:
        return OpenAIProvider(api_key="k", model="gpt-4o-mini", base_url="https://api.test/v1")

    def test_parses_plain_text_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response(
                {
                    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"total_tokens": 7},
                }
            ),
        )
        reply = self._provider().complete([{"role": "user", "content": "yo"}])

        assert reply.content == "hi"
        assert reply.wants_tools is False
        assert reply.usage["total_tokens"] == 7

    def test_parses_tool_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path": "a.txt"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
        )
        reply = self._provider().complete([{"role": "user", "content": "read"}])

        assert reply.wants_tools is True
        assert reply.tool_calls[0].name == "read_file"
        assert reply.tool_calls[0].arguments == {"path": "a.txt"}

    def test_malformed_tool_arguments_do_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"id": "c", "function": {"name": "x", "arguments": "{oops"}}
                                ]
                            }
                        }
                    ]
                }
            ),
        )
        reply = self._provider().complete([])
        assert reply.tool_calls[0].arguments == {"__raw__": "{oops"}

    def test_sends_tool_schema_and_auth_header(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        import jaigent.llm.openai as mod

        sent = _patch_post(
            monkeypatch, mod, lambda *a: _response({"choices": [{"message": {"content": "ok"}}]})
        )
        tools = build_default_registry(settings)
        self._provider().complete([{"role": "user", "content": "x"}], tools)

        payload = sent[0]["json"]
        assert payload["tool_choice"] == "auto"
        assert {t["function"]["name"] for t in payload["tools"]} >= {"read_file", "web_search"}
        assert sent[0]["headers"]["Authorization"] == "Bearer k"

    @pytest.mark.parametrize(
        ("status", "hint"),
        [(401, "API key was rejected"), (404, "not found"), (429, "Rate limit")],
    )
    def test_http_errors_get_hints(
        self, monkeypatch: pytest.MonkeyPatch, status: int, hint: str
    ) -> None:
        import jaigent.llm.openai as mod

        _patch_post(
            monkeypatch, mod, lambda *a: _response({"error": {"message": "boom"}}, status=status)
        )
        with pytest.raises(ProviderError, match=hint):
            self._provider().complete([])

    def test_unexpected_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        _patch_post(monkeypatch, mod, lambda *a: _response({"nope": True}))
        with pytest.raises(ProviderError, match="Unexpected response shape"):
            self._provider().complete([])

    def test_message_round_trip(self) -> None:
        provider = self._provider()
        message = AssistantMessage(
            content="", tool_calls=[ToolCall("c1", "read_file", {"path": "a"})]
        )
        serialised = provider.format_assistant_message(message)

        assert serialised["role"] == "assistant"
        assert json.loads(serialised["tool_calls"][0]["function"]["arguments"]) == {"path": "a"}

        result = provider.format_tool_result(message.tool_calls[0], "content")
        assert result == {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "read_file",
            "content": "content",
        }


class TestAnthropicProvider:
    def _provider(self) -> AnthropicProvider:
        return AnthropicProvider(api_key="k", model="claude", base_url="https://api.test/v1")

    def test_parses_text_and_tool_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response(
                {
                    "content": [
                        {"type": "text", "text": "Let me look. "},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "web_search",
                            "input": {"query": "python"},
                        },
                    ],
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                }
            ),
        )
        reply = self._provider().complete([{"role": "user", "content": "search"}])

        assert reply.content == "Let me look. "
        assert reply.tool_calls[0].name == "web_search"
        assert reply.tool_calls[0].arguments == {"query": "python"}

    def test_system_messages_are_hoisted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        sent = _patch_post(
            monkeypatch, mod, lambda *a: _response({"content": [{"type": "text", "text": "ok"}]})
        )
        self._provider().complete(
            [{"role": "system", "content": "be nice"}, {"role": "user", "content": "hi"}]
        )
        payload = sent[0]["json"]

        assert payload["system"] == "be nice"
        assert all(m["role"] != "system" for m in payload["messages"])

    def test_sends_version_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        sent = _patch_post(
            monkeypatch, mod, lambda *a: _response({"content": [{"type": "text", "text": "ok"}]})
        )
        self._provider().complete([{"role": "user", "content": "hi"}])

        assert sent[0]["headers"]["x-api-key"] == "k"
        assert "anthropic-version" in sent[0]["headers"]

    def test_tool_result_shape(self) -> None:
        provider = self._provider()
        result = provider.format_tool_result(ToolCall("tu_1", "read_file", {}), "output")

        assert result["role"] == "user"
        assert result["content"][0]["type"] == "tool_result"
        assert result["content"][0]["tool_use_id"] == "tu_1"

    def test_parallel_tool_results_are_one_user_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        sent = _patch_post(
            monkeypatch, mod, lambda *a: _response({"content": [{"type": "text", "text": "ok"}]})
        )
        first = self._provider().format_tool_result(ToolCall("a", "one", {}), "A")
        second = self._provider().format_tool_result(ToolCall("b", "two", {}), "B")
        self._provider().complete(
            [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": "…"},
                first,
                second,
            ]
        )

        convo = sent[0]["json"]["messages"]
        tool_turns = [
            m for m in convo if m.get("role") == "user" and isinstance(m.get("content"), list)
        ]
        assert len(tool_turns) == 1
        assert [block["tool_use_id"] for block in tool_turns[0]["content"]] == ["a", "b"]

    def test_assistant_message_blocks(self) -> None:
        provider = self._provider()
        message = AssistantMessage(content="text", tool_calls=[ToolCall("t", "n", {"a": 1})])
        blocks = provider.format_assistant_message(message)["content"]

        assert blocks[0] == {"type": "text", "text": "text"}
        assert blocks[1]["type"] == "tool_use"


class TestGetProvider:
    def test_returns_openai(self) -> None:
        provider = get_provider(Settings(provider="openai", api_key="k"))
        assert isinstance(provider, OpenAIProvider)

    def test_returns_anthropic(self) -> None:
        provider = get_provider(Settings(provider="anthropic", api_key="k"))
        assert isinstance(provider, AnthropicProvider)

    def test_missing_key_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="No API key"):
            get_provider(Settings(provider="openai", api_key=None))

    def test_unknown_provider_raises(self) -> None:
        settings = Settings(api_key="k")
        object.__setattr__(settings, "provider", "skynet")
        with pytest.raises(ConfigurationError, match="Unknown provider"):
            get_provider(settings)

    def test_base_url_override(self) -> None:
        provider = get_provider(
            Settings(provider="openai", api_key="k", base_url="https://openrouter.ai/api/v1/")
        )
        assert provider.base_url == "https://openrouter.ai/api/v1"


class TestOpenAICompatFixes:
    def test_reasoning_models_send_max_completion_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.openai as mod

        sent = _patch_post(
            monkeypatch, mod, lambda *a: _response({"choices": [{"message": {"content": "ok"}}]})
        )
        OpenAIProvider(api_key="k", model="o3-mini", base_url="https://api.test/v1").complete(
            [{"role": "user", "content": "hi"}]
        )

        payload = sent[0]["json"]
        assert "max_tokens" not in payload
        assert payload["max_completion_tokens"] == 2048

    def test_max_tokens_rejection_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        attempts: list[dict] = []

        def handler(url, json, headers):  # noqa: A002, ANN001
            attempts.append(json)
            if "max_tokens" in json:
                return _response(
                    {"error": {"message": "Unsupported parameter: 'max_tokens'"}}, status=400
                )
            return _response({"choices": [{"message": {"content": "ok"}}]})

        _patch_post(monkeypatch, mod, handler)
        reply = OpenAIProvider(
            api_key="k", model="mystery-model", base_url="https://api.test/v1"
        ).complete([{"role": "user", "content": "hi"}])

        assert reply.content == "ok"
        assert len(attempts) == 2
        assert "max_completion_tokens" in attempts[1]
        assert "max_tokens" not in attempts[1]

    def test_openrouter_sends_attribution_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        sent = _patch_post(
            monkeypatch, mod, lambda *a: _response({"choices": [{"message": {"content": "ok"}}]})
        )
        OpenAIProvider(
            api_key="k", model="openai/gpt-4o-mini", base_url="https://openrouter.ai/api/v1"
        ).complete([{"role": "user", "content": "hi"}])

        headers = sent[0]["headers"]
        assert headers["HTTP-Referer"] == "https://github.com/jaime-gaming/jaigent"
        assert headers["X-Title"] == "jaigent"
