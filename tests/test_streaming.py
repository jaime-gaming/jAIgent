"""Incremental streaming: SSE parsing for both providers, and the agent wiring."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import FakeProvider
from jaigent.agent import Agent
from jaigent.config import Settings
from jaigent.errors import ProviderError
from jaigent.llm.anthropic import AnthropicProvider
from jaigent.llm.base import AssistantMessage, ToolCall
from jaigent.llm.openai import OpenAIProvider


def sse(*events: str) -> bytes:
    return "".join(f"data: {event}\n\n" for event in events).encode()


def patch_stream(monkeypatch: pytest.MonkeyPatch, module: Any, body: bytes, status: int = 200):
    """Point the provider's httpx.Client at a canned SSE response."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = request.read()
        captured["headers"] = dict(request.headers)
        return httpx.Response(status, content=body)

    class FakeClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(module.httpx, "Client", FakeClient)
    return captured


class TestOpenAIStreaming:
    def provider(self) -> OpenAIProvider:
        return OpenAIProvider(api_key="k", model="gpt-4o-mini", base_url="https://api.test/v1")

    def test_text_arrives_in_chunks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        patch_stream(
            monkeypatch,
            mod,
            sse(
                '{"choices":[{"delta":{"content":"Hel"}}]}',
                '{"choices":[{"delta":{"content":"lo"}}]}',
                "[DONE]",
            ),
        )
        seen: list[str] = []
        reply = self.provider().complete([], on_text=seen.append)

        assert seen == ["Hel", "lo"]
        assert reply.content == "Hello"

    def test_stream_flag_is_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        captured = patch_stream(monkeypatch, mod, sse("[DONE]"))
        self.provider().complete([], on_text=lambda _: None)

        assert b'"stream":true' in captured["payload"]

    def test_tool_calls_are_reassembled_from_fragments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.openai as mod

        patch_stream(
            monkeypatch,
            mod,
            sse(
                '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
                '"function":{"name":"read_file","arguments":""}}]}}]}',
                '{"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"function":{"arguments":"{\\"path\\":"}}]}}]}',
                '{"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"function":{"arguments":" \\"a.txt\\"}"}}]}}]}',
                "[DONE]",
            ),
        )
        reply = self.provider().complete([], on_text=lambda _: None)

        assert len(reply.tool_calls) == 1
        assert reply.tool_calls[0].name == "read_file"
        assert reply.tool_calls[0].arguments == {"path": "a.txt"}

    def test_parallel_tool_calls_stay_separate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        patch_stream(
            monkeypatch,
            mod,
            sse(
                '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a",'
                '"function":{"name":"one","arguments":"{}"}}]}}]}',
                '{"choices":[{"delta":{"tool_calls":[{"index":1,"id":"b",'
                '"function":{"name":"two","arguments":"{}"}}]}}]}',
                "[DONE]",
            ),
        )
        reply = self.provider().complete([], on_text=lambda _: None)

        assert [c.name for c in reply.tool_calls] == ["one", "two"]

    def test_usage_is_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        patch_stream(
            monkeypatch,
            mod,
            sse(
                '{"choices":[{"delta":{"content":"x"}}]}',
                '{"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,'
                '"total_tokens":12}}',
                "[DONE]",
            ),
        )
        reply = self.provider().complete([], on_text=lambda _: None)
        assert reply.usage["total_tokens"] == 12

    def test_malformed_events_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        patch_stream(
            monkeypatch,
            mod,
            sse("{not json", '{"choices":[{"delta":{"content":"ok"}}]}', "[DONE]"),
        )
        assert self.provider().complete([], on_text=lambda _: None).content == "ok"

    def test_http_error_during_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        patch_stream(monkeypatch, mod, b'{"error":{"message":"nope"}}', status=401)
        with pytest.raises(ProviderError, match="API key was rejected"):
            self.provider().complete([], on_text=lambda _: None)

    def test_non_streaming_path_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        def handler(request: httpx.Request) -> httpx.Response:
            assert b'"stream"' not in request.read()
            return httpx.Response(200, json={"choices": [{"message": {"content": "plain"}}]})

        class FakeClient(httpx.Client):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(transport=httpx.MockTransport(handler))

        monkeypatch.setattr(mod.httpx, "Client", FakeClient)
        assert self.provider().complete([]).content == "plain"


class TestAnthropicStreaming:
    def provider(self) -> AnthropicProvider:
        return AnthropicProvider(api_key="k", model="claude", base_url="https://api.test/v1")

    def test_text_deltas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        patch_stream(
            monkeypatch,
            mod,
            sse(
                '{"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"Hi "}}',
                '{"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"there"}}',
                '{"type":"message_stop"}',
            ),
        )
        seen: list[str] = []
        reply = self.provider().complete([], on_text=seen.append)

        assert seen == ["Hi ", "there"]
        assert reply.content == "Hi there"

    def test_tool_use_json_is_accumulated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        patch_stream(
            monkeypatch,
            mod,
            sse(
                '{"type":"content_block_start","index":0,'
                '"content_block":{"type":"tool_use","id":"tu1","name":"web_search"}}',
                '{"type":"content_block_delta","index":0,'
                '"delta":{"type":"input_json_delta","partial_json":"{\\"query\\":"}}',
                '{"type":"content_block_delta","index":0,'
                '"delta":{"type":"input_json_delta","partial_json":" \\"cats\\"}"}}',
                '{"type":"message_stop"}',
            ),
        )
        reply = self.provider().complete([], on_text=lambda _: None)

        assert reply.tool_calls[0].name == "web_search"
        assert reply.tool_calls[0].arguments == {"query": "cats"}

    def test_usage_from_message_delta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        patch_stream(
            monkeypatch,
            mod,
            sse('{"type":"message_delta","usage":{"output_tokens":25}}'),
        )
        reply = self.provider().complete([], on_text=lambda _: None)
        assert reply.usage["output_tokens"] == 25

    def test_stream_flag_and_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        captured = patch_stream(monkeypatch, mod, sse('{"type":"message_stop"}'))
        self.provider().complete([], on_text=lambda _: None)

        assert b'"stream":true' in captured["payload"]
        assert captured["headers"]["x-api-key"] == "k"


class TestAgentStreaming:
    def test_agent_forwards_chunks_to_the_sink(self, settings: Settings) -> None:
        chunks: list[str] = []
        provider = FakeProvider([AssistantMessage(content="hello world")], chunk_size=3)
        agent = Agent(settings, provider=provider, on_text=chunks.append)

        result = agent.run("hi")

        assert "".join(chunks) == "hello world"
        assert result.output == "hello world"

    def test_no_sink_means_no_streaming(self, settings: Settings) -> None:
        provider = FakeProvider([AssistantMessage(content="quiet")])
        Agent(settings, provider=provider).run("hi")
        assert provider.streamed == []

    def test_provider_without_support_is_not_streamed(self, settings: Settings) -> None:
        provider = FakeProvider([AssistantMessage(content="text")])
        provider.supports_streaming = False
        chunks: list[str] = []
        Agent(settings, provider=provider, on_text=chunks.append).run("hi")

        assert chunks == []

    def test_streaming_works_across_tool_steps(self, settings: Settings) -> None:
        chunks: list[str] = []
        provider = FakeProvider(
            [
                AssistantMessage(tool_calls=[ToolCall("c1", "list_files", {})]),
                AssistantMessage(content="all done"),
            ]
        )
        agent = Agent(settings, provider=provider, on_text=chunks.append)
        result = agent.run("look")

        assert "".join(chunks) == "all done"
        assert result.tool_calls == 1
