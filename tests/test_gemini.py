"""The Gemini adapter, and that DeepSeek/Grok need no adapter of their own."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jaigent.config import DEFAULT_BASE_URLS, DEFAULT_MODELS, Settings
from jaigent.errors import ProviderError
from jaigent.llm import get_provider
from jaigent.llm.base import AssistantMessage, ToolCall
from jaigent.llm.gemini import GeminiProvider, _clean_schema
from jaigent.llm.openai import OpenAIProvider
from jaigent.tools import build_default_registry


def provider() -> GeminiProvider:
    return GeminiProvider(api_key="k", model="gemini-2.5-flash", base_url="https://gen.test/v1beta")


def patch_http(monkeypatch: pytest.MonkeyPatch, payload: dict, status: int = 200) -> dict:
    """Capture the outgoing request and return a canned response."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        return httpx.Response(status, json=payload)

    class FakeClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(handler))

    import jaigent.llm.gemini as mod

    monkeypatch.setattr(mod.httpx, "Client", FakeClient)
    return captured


class TestSchemaCleaning:
    def test_strips_unsupported_keywords(self) -> None:
        cleaned = _clean_schema(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"type": "string"}},
            }
        )
        assert "additionalProperties" not in cleaned
        assert cleaned["properties"]["a"]["type"] == "string"

    def test_recurses_into_nested_objects(self) -> None:
        cleaned = _clean_schema({"a": {"b": {"$schema": "x", "type": "string"}}})
        assert "$schema" not in cleaned["a"]["b"]

    def test_handles_lists(self) -> None:
        assert _clean_schema([{"default": 1, "type": "string"}]) == [{"type": "string"}]

    def test_every_real_tool_schema_survives(self) -> None:
        registry = build_default_registry(Settings(api_key="k", workspace="/tmp"))
        for tool in registry:
            cleaned = _clean_schema(tool.parameters)
            assert cleaned["type"] == "object"
            assert "additionalProperties" not in cleaned


class TestMessageTranslation:
    def test_system_becomes_system_instruction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = patch_http(monkeypatch, {"candidates": []})
        provider().complete(
            [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
        )

        assert b"systemInstruction" in captured["body"]
        assert b"Be brief." in captured["body"]

    def test_assistant_role_is_renamed_to_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = patch_http(monkeypatch, {"candidates": []})
        provider().complete(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        )

        assert b'"model"' in captured["body"]

    def test_tool_results_become_function_responses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = patch_http(monkeypatch, {"candidates": []})
        provider().complete([{"role": "tool", "name": "read_file", "content": "file text"}])

        assert b"functionResponse" in captured["body"]
        assert b"read_file" in captured["body"]

    def test_parallel_tool_results_share_one_user_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        captured = patch_http(monkeypatch, {"candidates": []})
        provider().complete(
            [
                {"role": "tool", "name": "read_file", "content": "a"},
                {"role": "tool", "name": "list_files", "content": "b"},
            ]
        )

        body = json.loads(captured["body"])
        users = [item for item in body["contents"] if item.get("role") == "user"]
        assert len(users) == 1
        names = [part["functionResponse"]["name"] for part in users[0]["parts"]]
        assert names == ["read_file", "list_files"]

    def test_key_travels_as_a_query_parameter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = patch_http(monkeypatch, {"candidates": []})
        provider().complete([{"role": "user", "content": "hi"}])

        assert "key=k" in captured["url"]

    def test_model_is_in_the_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = patch_http(monkeypatch, {"candidates": []})
        provider().complete([{"role": "user", "content": "hi"}])

        assert "gemini-2.5-flash:generateContent" in captured["url"]


class TestResponseParsing:
    def test_plain_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_http(
            monkeypatch,
            {"candidates": [{"content": {"parts": [{"text": "Hello there"}]}}]},
        )
        reply = provider().complete([{"role": "user", "content": "hi"}])

        assert reply.content == "Hello there"
        assert reply.wants_tools is False

    def test_function_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_http(
            monkeypatch,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Let me look. "},
                                {
                                    "functionCall": {
                                        "name": "web_search",
                                        "args": {"query": "python"},
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
        )
        reply = provider().complete([{"role": "user", "content": "search"}])

        assert reply.content == "Let me look. "
        assert reply.tool_calls[0].name == "web_search"
        assert reply.tool_calls[0].arguments == {"query": "python"}

    def test_usage_is_normalised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_http(
            monkeypatch,
            {
                "candidates": [{"content": {"parts": [{"text": "x"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 40,
                    "candidatesTokenCount": 12,
                    "totalTokenCount": 52,
                },
            },
        )
        usage = provider().complete([{"role": "user", "content": "hi"}]).usage

        # Reported in the same vocabulary as every other provider.
        assert usage["prompt_tokens"] == 40
        assert usage["completion_tokens"] == 12

    def test_empty_candidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_http(monkeypatch, {"candidates": []})
        assert provider().complete([{"role": "user", "content": "hi"}]).content == ""


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "hint"), [(401, "GEMINI_API_KEY"), (404, "not found"), (429, "Rate limit")]
    )
    def test_status_hints(self, monkeypatch: pytest.MonkeyPatch, status: int, hint: str) -> None:
        patch_http(monkeypatch, {"error": {"message": "nope"}}, status=status)
        with pytest.raises(ProviderError, match=hint):
            provider().complete([{"role": "user", "content": "hi"}])


class TestStreaming:
    def test_supports_streaming(self) -> None:
        assert GeminiProvider.supports_streaming is True

    def test_chunks_are_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events = (
            b'data: {"candidates":[{"content":{"parts":[{"text":"Hi "}]}}]}\n\n'
            b'data: {"candidates":[{"content":{"parts":[{"text":"there"}]}}]}\n\n'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert "streamGenerateContent" in str(request.url)
            return httpx.Response(200, content=events)

        class FakeClient(httpx.Client):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(transport=httpx.MockTransport(handler))

        import jaigent.llm.gemini as mod

        monkeypatch.setattr(mod.httpx, "Client", FakeClient)

        seen: list[str] = []
        reply = provider().complete([{"role": "user", "content": "hi"}], on_text=seen.append)

        assert seen == ["Hi ", "there"]
        assert reply.content == "Hi there"


class TestRoundTrip:
    def test_assistant_message_shape(self) -> None:
        message = AssistantMessage(content="x", tool_calls=[ToolCall("i", "n", {"a": 1})])
        serialised = provider().format_assistant_message(message)

        assert serialised["role"] == "assistant"
        assert serialised["tool_calls"][0]["function"]["name"] == "n"

    def test_tool_result_shape(self) -> None:
        result = provider().format_tool_result(ToolCall("i", "read_file", {}), "out")
        assert result == {"role": "tool", "name": "read_file", "content": "out"}


class TestRegistration:
    def test_gemini_is_wired_up(self) -> None:
        built = get_provider(Settings(provider="gemini", api_key="k", workspace="/tmp"))

        assert isinstance(built, GeminiProvider)
        assert built.model == DEFAULT_MODELS["gemini"]
        assert built.base_url == DEFAULT_BASE_URLS["gemini"]

    @pytest.mark.parametrize("name", ["deepseek", "xai"])
    def test_deepseek_and_grok_reuse_the_openai_adapter(self, name: str) -> None:
        # Both ship OpenAI-compatible endpoints; a separate class would be dead weight.
        built = get_provider(Settings(provider=name, api_key="k", workspace="/tmp"))
        assert isinstance(built, OpenAIProvider)

    def test_grok_default_is_current(self) -> None:
        assert DEFAULT_MODELS["xai"].startswith("grok-")

    def test_deepseek_endpoint(self) -> None:
        assert "deepseek.com" in DEFAULT_BASE_URLS["deepseek"]
