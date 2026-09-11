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

        def post(  # noqa: A002
            self,
            url: str,
            json: dict | None = None,
            headers: dict | None = None,
            **kwargs: Any,
        ):
            sent.append({"url": url, "json": json, "headers": headers, **kwargs})
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
        assert headers["X-Title"] == "jAIgent"


def _patch_stream(
    monkeypatch: pytest.MonkeyPatch, module: Any, lines: list[str], status: int = 200
) -> list[dict]:
    """Serve canned SSE lines to a provider's streaming loop."""
    sent: list[dict] = []

    class FakeStream:
        def __init__(self, **kwargs: Any) -> None:
            self._kwargs = kwargs

        def __enter__(self) -> FakeStream:
            sent.append(self._kwargs)
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        status_code = status
        request = httpx.Request("POST", "https://api.test/x")

        def read(self) -> bytes:
            return b""

        def iter_lines(self):  # noqa: ANN201, ANN202
            return iter(lines)

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def stream(self, *args: Any, **kwargs: Any) -> FakeStream:
            return FakeStream(**kwargs)

    monkeypatch.setattr(module.httpx, "Client", FakeClient)
    return sent


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


class TestStreamIndex:
    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ({"index": 2}, 2),
            ({"index": "3"}, 3),
            ({}, 0),
            ({"index": None}, 0),
            ({"index": "bogus"}, 0),
            ({"index": [1]}, 0),
        ],
    )
    def test_garbage_maps_to_slot_zero(self, event: dict, expected: int) -> None:
        from jaigent.llm.base import stream_index

        assert stream_index(event) == expected


class TestOpenAIStream:
    def test_text_chunks_accumulate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        _patch_stream(
            monkeypatch,
            mod,
            [
                _sse({"choices": [{"delta": {"content": "hel"}}]}),
                _sse({"choices": [{"delta": {"content": "lo"}}]}),
                "data: [DONE]",
            ],
        )
        seen: list[str] = []
        reply = OpenAIProvider(api_key="k", model="m", base_url="https://api.test/v1").complete(
            [{"role": "user", "content": "hi"}], on_text=seen.append
        )

        assert reply.content == "hello"
        assert seen == ["hel", "lo"]

    def test_a_null_index_does_not_crash_the_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        _patch_stream(
            monkeypatch,
            mod,
            [
                _sse(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": None,
                                            "id": "c1",
                                            "function": {"name": "read_file", "arguments": ""},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ),
                _sse(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {"index": None, "function": {"arguments": '{"path": "x"}'}}
                                    ]
                                }
                            }
                        ]
                    }
                ),
                "data: [DONE]",
            ],
        )
        reply = OpenAIProvider(api_key="k", model="m", base_url="https://api.test/v1").complete(
            [{"role": "user", "content": "hi"}], on_text=lambda chunk: None
        )

        assert [(c.name, c.arguments) for c in reply.tool_calls] == [("read_file", {"path": "x"})]

    def test_malformed_stream_items_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        _patch_stream(
            monkeypatch,
            mod,
            [
                _sse({"choices": ["not-a-dict", 42, {"delta": {"content": "ok"}}]}),
                _sse({"choices": [{"delta": {"tool_calls": ["x", {"index": 0}]}}]}),
                _sse({"choices": "nope"}),
                "data: [DONE]",
            ],
        )
        reply = OpenAIProvider(api_key="k", model="m", base_url="https://api.test/v1").complete(
            [{"role": "user", "content": "hi"}], on_text=lambda chunk: None
        )

        assert reply.content == "ok"

    def test_non_stream_malformed_tool_calls_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.openai as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    "junk",
                                    {"id": "c", "function": "also-junk"},
                                    {
                                        "id": "c2",
                                        "function": {"name": "read_file", "arguments": "{}"},
                                    },
                                ],
                            }
                        }
                    ]
                }
            ),
        )
        reply = OpenAIProvider(api_key="k", model="m", base_url="https://api.test/v1").complete(
            [{"role": "user", "content": "hi"}]
        )

        assert [(c.name, c.id) for c in reply.tool_calls] == [("read_file", "c2")]


class TestAnthropicStream:
    def test_tool_use_survives_a_null_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        _patch_stream(
            monkeypatch,
            mod,
            [
                _sse(
                    {
                        "type": "content_block_start",
                        "index": None,
                        "content_block": {"type": "tool_use", "id": "t1", "name": "read_file"},
                    }
                ),
                _sse(
                    {
                        "type": "content_block_delta",
                        "index": None,
                        "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
                    }
                ),
                _sse(
                    {
                        "type": "content_block_delta",
                        "index": None,
                        "delta": {"type": "input_json_delta", "partial_json": ' "x"}'},
                    }
                ),
                _sse({"type": "message_delta", "usage": {"output_tokens": 5}}),
            ],
        )
        seen: list[str] = []
        reply = AnthropicProvider(api_key="k", model="m", base_url="https://api.test").complete(
            [{"role": "user", "content": "hi"}], on_text=seen.append
        )

        assert [(c.name, c.arguments) for c in reply.tool_calls] == [("read_file", {"path": "x"})]
        assert reply.usage == {"output_tokens": 5}

    def test_malformed_content_blocks_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response({"content": ["junk", 42, {"type": "text", "text": "ok"}]}),
        )
        reply = AnthropicProvider(api_key="k", model="m", base_url="https://api.test").complete(
            [{"role": "user", "content": "hi"}]
        )

        assert reply.content == "ok"

    def test_a_non_list_content_is_treated_as_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        _patch_post(monkeypatch, mod, lambda *a: _response({"content": {"type": "text"}}))
        reply = AnthropicProvider(api_key="k", model="m", base_url="https://api.test").complete(
            [{"role": "user", "content": "hi"}]
        )

        assert reply.content == ""
        assert reply.tool_calls == []

    def test_a_non_dict_tool_input_is_emptied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response(
                {"content": [{"type": "tool_use", "id": "t", "name": "read_file", "input": ["x"]}]}
            ),
        )
        reply = AnthropicProvider(api_key="k", model="m", base_url="https://api.test").complete(
            [{"role": "user", "content": "hi"}]
        )

        assert [c.arguments for c in reply.tool_calls] == [{}]

    def test_a_contentless_system_message_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.anthropic as mod

        sent = _patch_post(
            monkeypatch, mod, lambda *a: _response({"content": [{"type": "text", "text": "ok"}]})
        )
        reply = AnthropicProvider(api_key="k", model="m", base_url="https://api.test").complete(
            [{"role": "system"}, {"role": "user", "content": "hi"}]
        )

        assert reply.content == "ok"
        assert sent[0]["json"]["messages"] == [{"role": "user", "content": "hi"}]

    def test_non_dict_history_is_dropped_not_crashed_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.anthropic as mod

        sent = _patch_post(
            monkeypatch, mod, lambda *a: _response({"content": [{"type": "text", "text": "ok"}]})
        )
        AnthropicProvider(api_key="k", model="m", base_url="https://api.test").complete(
            [{"role": "user", "content": "hi"}, "junk"]  # type: ignore[list-item]
        )

        # "junk" used to be forwarded to the API, which rejected the request;
        # it is dropped so the surviving history still sends.
        assert sent[0]["json"]["messages"] == [{"role": "user", "content": "hi"}]


class TestGeminiProvider:
    def _provider(self) -> Any:
        from jaigent.llm.gemini import GeminiProvider

        return GeminiProvider(api_key="k", model="gemini-2.0-flash", base_url="https://api.test")

    def test_parses_a_text_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.gemini as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response(
                {
                    "candidates": [
                        {"content": {"parts": [{"text": "hello there"}]}},
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 3,
                        "totalTokenCount": 13,
                    },
                }
            ),
        )
        reply = self._provider().complete([{"role": "user", "content": "hi"}])

        assert reply.content == "hello there"
        assert reply.usage == {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "total_tokens": 13,
        }

    def test_parses_a_function_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.gemini as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "read_file",
                                            "args": {"path": "x"},
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
        )
        reply = self._provider().complete([{"role": "user", "content": "hi"}])

        assert [(c.name, c.arguments) for c in reply.tool_calls] == [("read_file", {"path": "x"})]

    def test_malformed_responses_parse_to_nothing_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.gemini as mod

        for payload in (
            {"candidates": "nope"},
            {"candidates": ["not-a-dict"]},
            {"candidates": [{"content": {"parts": ["x", 42, {"text": 7}]}}]},
            {"candidates": [{"content": {"parts": [{"functionCall": "junk"}]}}]},
            {
                "candidates": [
                    {"content": {"parts": [{"functionCall": {"name": "t", "args": [1]}}]}}
                ]
            },
            {"usageMetadata": ["junk"]},
            {"usageMetadata": {"promptTokenCount": "many"}},
            {},
        ):
            _patch_post(monkeypatch, mod, lambda *a, p=payload: _response(p))
            reply = self._provider().complete([{"role": "user", "content": "hi"}])
            assert reply.content == ""
            assert all(isinstance(c.arguments, dict) for c in reply.tool_calls)

    def test_garbage_history_is_skipped_not_crashed_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.gemini as mod

        sent = _patch_post(monkeypatch, mod, lambda *a: _response({"candidates": []}))
        self._provider().complete(
            [
                {"role": "user", "content": "hi"},
                "junk",  # type: ignore[list-item]
                {"role": "assistant", "content": "", "tool_calls": "junk"},
                {"role": "assistant", "content": "", "tool_calls": ["junk", {"function": "x"}]},
            ]
        )

        assert sent[0]["json"]["contents"][0] == {"role": "user", "parts": [{"text": "hi"}]}

    def test_tool_results_merge_into_one_user_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.gemini as mod

        sent = _patch_post(monkeypatch, mod, lambda *a: _response({"candidates": []}))
        self._provider().complete(
            [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "a", "arguments": {}}},
                        {"function": {"name": "b", "arguments": {}}},
                    ],
                },
                {"role": "tool", "name": "a", "content": "1"},
                {"role": "tool", "name": "b", "content": "2"},
            ]
        )

        contents = sent[0]["json"]["contents"]
        assert contents[-1]["role"] == "user"
        assert len(contents[-1]["parts"]) == 2

    def test_error_statuses_become_provider_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.gemini as mod

        _patch_post(
            monkeypatch,
            mod,
            lambda *a: _response({"error": {"message": "bad key"}}, status=400),
        )
        with pytest.raises(ProviderError, match="bad key"):
            self._provider().complete([{"role": "user", "content": "hi"}])

    def test_streaming_accumulates_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.gemini as mod

        _patch_stream(
            monkeypatch,
            mod,
            [
                _sse({"candidates": [{"content": {"parts": [{"text": "hel"}]}}]}),
                _sse({"candidates": [{"content": {"parts": [{"text": "lo"}]}}]}),
            ],
        )
        seen: list[str] = []
        reply = self._provider().complete([{"role": "user", "content": "hi"}], on_text=seen.append)

        assert reply.content == "hello"
        assert seen == ["hel", "lo"]
