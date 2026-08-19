"""OpenAI-compatible chat completions provider.

Because the ``/chat/completions`` shape is a de-facto standard, this provider
also works against OpenRouter, Groq, Together, Fireworks, LM Studio, Ollama and
vLLM — just point ``JAIGENT_BASE_URL`` at them.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from jaigent.errors import ProviderError
from jaigent.llm.base import AssistantMessage, LLMProvider, TextStream, ToolCall
from jaigent.tools import ToolRegistry


class _StreamOptionsRejected(Exception):
    """The provider rejected ``stream_options`` in the streaming payload.

    Some OpenAI-compatible endpoints (Ollama, older vLLM) do not support the
    ``stream_options`` parameter and answer HTTP 400. This exception signals
    ``_stream`` to retry once without it.
    """


class _MaxTokensRejected(Exception):
    """The provider wants ``max_completion_tokens`` instead of ``max_tokens``."""


#: Newer OpenAI reasoning models reject the legacy ``max_tokens`` field.
_MAX_COMPLETION_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def _needs_max_completion_tokens(model: str) -> bool:
    """Whether this model id is known to reject ``max_tokens``."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    return name.startswith(_MAX_COMPLETION_PREFIXES)


def _swap_max_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace ``max_tokens`` with ``max_completion_tokens``."""
    swapped = dict(payload)
    if "max_tokens" in swapped:
        swapped["max_completion_tokens"] = swapped.pop("max_tokens")
    return swapped


def _wants_max_completion_tokens(error: Exception, payload: dict[str, Any]) -> bool:
    """Whether a 400 is the model asking us to rename ``max_tokens``."""
    if "max_tokens" not in payload:
        return False
    text = str(error).lower()
    return "max_completion_tokens" in text or (
        "max_tokens" in text
        and any(token in text for token in ("unsupported", "not supported", "unknown parameter"))
    )


class OpenAIProvider(LLMProvider):
    """Talks to any ``POST {base_url}/chat/completions`` endpoint."""

    name = "openai"
    supports_streaming = True

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: ToolRegistry | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        on_text: TextStream | None = None,
    ) -> AssistantMessage:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if _needs_max_completion_tokens(self.model):
            payload = _swap_max_tokens(payload)
        if tools is not None and len(tools) > 0:
            payload["tools"] = tools.to_openai_schema()
            payload["tool_choice"] = "auto"

        if on_text is not None:
            return self._stream(payload, on_text)

        try:
            data = self._post("/chat/completions", payload)
        except ProviderError as exc:
            if _wants_max_completion_tokens(exc, payload):
                payload = _swap_max_tokens(payload)
                data = self._post("/chat/completions", payload)
            else:
                raise

        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Unexpected response shape from {self.base_url}: {data}") from exc

        calls: list[ToolCall] = []
        for item in choice.get("tool_calls") or []:
            function = item.get("function", {})
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                arguments = {"__raw__": raw_args}
            calls.append(
                ToolCall(
                    id=item.get("id", f"call_{len(calls)}"),
                    name=function.get("name", ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )

        return AssistantMessage(
            content=choice.get("content") or "",
            tool_calls=calls,
            raw=choice,
            usage=data.get("usage") or {},
        )

    def format_assistant_message(self, message: AssistantMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": "assistant", "content": message.content or None}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        return payload

    def format_tool_result(self, call: ToolCall, output: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output}

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _stream(self, payload: dict[str, Any], on_text: TextStream) -> AssistantMessage:
        """Consume a server-sent-event stream, reassembling text and tool calls.

        Tool call arguments arrive as JSON fragments spread across chunks and
        are indexed rather than named, so they are accumulated per index and
        parsed once the stream closes.

        The first attempt includes ``stream_options`` for usage tracking, but
        some providers (Ollama, older vLLM, assorted OpenAI-compatible gateways)
        reject this parameter with HTTP 400. When that happens, a single retry
        without ``stream_options`` is made.
        """
        payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        try:
            return self._stream_raw(payload, on_text)
        except _StreamOptionsRejected:
            payload.pop("stream_options", None)
            try:
                return self._stream_raw(payload, on_text)
            except _MaxTokensRejected:
                return self._stream_raw(_swap_max_tokens(payload), on_text)
        except _MaxTokensRejected:
            payload = _swap_max_tokens(payload)
            try:
                return self._stream_raw(payload, on_text)
            except _StreamOptionsRejected:
                payload.pop("stream_options", None)
                return self._stream_raw(payload, on_text)

    def _stream_raw(self, payload: dict[str, Any], on_text: TextStream) -> AssistantMessage:
        """Execute the SSE loop with an already-finalised payload.

        Raises:
            _StreamOptionsRejected: when the provider rejects ``stream_options``
                (HTTP 400 with a payload that carried it). The caller retries
                without the parameter.
        """
        headers = self._headers()

        content: list[str] = []
        partial: dict[int, dict[str, str]] = {}
        usage: dict[str, int] = {}

        try:
            with (
                httpx.Client(timeout=self.timeout) as client,
                client.stream(
                    "POST", f"{self.base_url}/chat/completions", json=payload, headers=headers
                ) as response,
            ):
                if response.status_code >= 400:
                    response.read()
                    error = ProviderError(
                        _explain_status(
                            httpx.HTTPStatusError(
                                "error", request=response.request, response=response
                            )
                        )
                    )
                    if response.status_code == 400 and _wants_max_completion_tokens(error, payload):
                        raise _MaxTokensRejected("the provider rejected max_tokens")
                    if (
                        response.status_code == 400
                        and "stream_options" in payload
                        and payload.get("stream_options") is not None
                    ):
                        raise _StreamOptionsRejected("the provider rejected stream_options")
                    raise error

                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if event.get("usage"):
                        usage = event["usage"]

                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        chunk = delta.get("content")
                        if chunk:
                            content.append(chunk)
                            on_text(chunk)
                        for item in delta.get("tool_calls") or []:
                            index = int(item.get("index", 0))
                            slot = partial.setdefault(index, {"id": "", "name": "", "args": ""})
                            if item.get("id"):
                                slot["id"] = item["id"]
                            function = item.get("function") or {}
                            if function.get("name"):
                                slot["name"] = function["name"]
                            if function.get("arguments"):
                                slot["args"] += function["arguments"]
        except (_StreamOptionsRejected, _MaxTokensRejected):
            raise  # let _stream handle the retry
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach {self.base_url}: {exc}") from exc

        calls: list[ToolCall] = []
        for index in sorted(partial):
            slot = partial[index]
            try:
                arguments = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                arguments = {"__raw__": slot["args"]}
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )

        return AssistantMessage(content="".join(content), tool_calls=calls, usage=usage)

    def _headers(self) -> dict[str, str]:
        """Auth and content-type, plus the headers OpenRouter asks for."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if "openrouter.ai" in self.base_url:
            # OpenRouter rate-limits unidentified traffic more aggressively.
            headers["HTTP-Referer"] = "https://github.com/jaime-gaming/jaigent"
            headers["X-Title"] = "jaigent"
        return headers

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = self._headers()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(f"{self.base_url}{path}", json=payload, headers=headers)
                response.raise_for_status()
                return dict(response.json())
        except httpx.HTTPStatusError as exc:
            raise ProviderError(_explain_status(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise ProviderError(f"{self.base_url} returned invalid JSON") from exc


def _explain_status(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    try:
        detail = exc.response.json().get("error", {}).get("message", exc.response.text[:400])
    except Exception:  # noqa: BLE001
        detail = exc.response.text[:400]

    hints = {
        401: "Your API key was rejected. Check OPENAI_API_KEY / JAIGENT_API_KEY.",
        403: "Your key is valid but not allowed to use this model.",
        404: "Model or endpoint not found. Check JAIGENT_MODEL and JAIGENT_BASE_URL.",
        429: "Rate limit or quota exceeded. Wait and retry, or check your billing.",
    }
    hint = hints.get(status, "")
    return f"HTTP {status} from the provider: {detail}" + (f"\nHint: {hint}" if hint else "")
