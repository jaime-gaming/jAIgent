"""Google Gemini provider.

Gemini's ``generateContent`` API differs from OpenAI's in almost every detail:
messages are ``contents`` with ``parts``, the assistant role is ``model``, the
system prompt is a separate ``systemInstruction``, tool schemas drop the
``additionalProperties`` keyword, and the key travels as a query parameter.
All of that translation lives here.

DeepSeek and Grok are *not* here — both ship OpenAI-compatible endpoints, so
they use :class:`~jaigent.llm.openai.OpenAIProvider` with a different base URL.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from jaigent.errors import ProviderError
from jaigent.llm.base import (
    AssistantMessage,
    LLMProvider,
    TextStream,
    ToolCall,
    normalize_messages,
    text_content,
)
from jaigent.tools import ToolRegistry

#: JSON Schema keywords Gemini rejects outright.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({"additionalProperties", "$schema", "examples", "default"})


def _clean_schema(schema: Any) -> Any:
    """Strip keywords Gemini's dialect of JSON Schema does not accept."""
    if isinstance(schema, dict):
        return {
            key: _clean_schema(value)
            for key, value in schema.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [_clean_schema(item) for item in schema]
    return schema


class GeminiProvider(LLMProvider):
    """Talks to ``POST {base_url}/models/{model}:generateContent``."""

    name = "gemini"
    supports_streaming = True

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------
    def _to_contents(self, messages: list[dict[str, Any]]) -> tuple[list[dict], str]:
        """Convert neutral messages into Gemini ``contents`` plus a system string."""
        contents: list[dict[str, Any]] = []
        system_parts: list[str] = []

        # Canonical first: OpenAI- or Anthropic-shaped history (failover, a
        # resumed session) converts to neutral tool messages, which render as
        # function calls and responses below.
        for message in normalize_messages(messages):
            role = message.get("role")

            if role == "system":
                system_parts.append(str(message.get("content", "")))
                continue

            if role == "tool":
                part = {
                    "functionResponse": {
                        "name": message.get("name") or "tool",
                        "response": {"result": message.get("content", "")},
                    }
                }
                # Gemini wants every functionResponse for one model turn in a
                # single user content. Consecutive tool messages are merged.
                previous = contents[-1] if contents else None
                if (
                    previous is not None
                    and previous.get("role") == "user"
                    and any("functionResponse" in item for item in previous.get("parts") or [])
                ):
                    previous["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue

            if role == "assistant":
                parts: list[dict[str, Any]] = []
                if message.get("content"):
                    parts.append({"text": text_content(message["content"])})
                tool_calls = message.get("tool_calls") or []
                if not isinstance(tool_calls, list):
                    tool_calls = []
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function", call)
                    if not isinstance(function, dict):
                        continue
                    raw = function.get("arguments", {})
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw or "{}")
                        except json.JSONDecodeError:
                            raw = {}
                    parts.append(
                        {"functionCall": {"name": function.get("name") or "", "args": raw}}
                    )
                if not parts:
                    # An empty text part is rejected, which used to poison the
                    # session: one empty reply broke every later turn.
                    parts.append({"text": "(empty reply)"})
                contents.append({"role": "model", "parts": parts})
                continue

            contents.append({"role": "user", "parts": [{"text": str(message.get("content", ""))}]})

        return contents, "\n\n".join(system_parts)

    def _parse(self, data: dict[str, Any]) -> AssistantMessage:
        """Turn one ``generateContent`` response into an AssistantMessage."""
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            candidates = []
        text_chunks: list[str] = []
        calls: list[ToolCall] = []

        first = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        content = first.get("content")
        parts = content.get("parts") if isinstance(content, dict) else []
        if not isinstance(parts, list):
            parts = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                text_chunks.append(part["text"])
            elif isinstance(part.get("functionCall"), dict):
                function = part["functionCall"]
                raw_args = function.get("args")
                calls.append(
                    ToolCall(
                        id=f"call_{len(calls)}",
                        name=function.get("name") or "",
                        arguments=raw_args if isinstance(raw_args, dict) else {},
                    )
                )

        usage_raw = data.get("usageMetadata")
        if not isinstance(usage_raw, dict):
            usage_raw = {}
        usage = {
            "prompt_tokens": _safe_int(usage_raw.get("promptTokenCount")),
            "completion_tokens": _safe_int(usage_raw.get("candidatesTokenCount")),
            "total_tokens": _safe_int(usage_raw.get("totalTokenCount")),
        }
        return AssistantMessage(
            content="".join(text_chunks),
            tool_calls=calls,
            raw=data,
            usage={k: v for k, v in usage.items() if v},
        )

    # ------------------------------------------------------------------
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: ToolRegistry | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        on_text: TextStream | None = None,
    ) -> AssistantMessage:
        contents, system = self._to_contents(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools is not None and len(tools) > 0:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": _clean_schema(tool.parameters),
                        }
                        for tool in tools
                    ]
                }
            ]

        if on_text is not None:
            return self._stream(payload, on_text)

        data = self._post(f"/models/{self.model}:generateContent", payload)
        return self._parse(data)

    def format_assistant_message(self, message: AssistantMessage) -> dict[str, Any]:
        # Stored in the neutral shape; _to_contents converts on the way out.
        payload: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if message.tool_calls:
            payload["tool_calls"] = [
                {"function": {"name": call.name, "arguments": call.arguments}}
                for call in message.tool_calls
            ]
        return payload

    def format_tool_result(self, call: ToolCall, output: str) -> dict[str, Any]:
        return {"role": "tool", "name": call.name, "content": output}

    # ------------------------------------------------------------------
    def _stream(self, payload: dict[str, Any], on_text: TextStream) -> AssistantMessage:
        """Consume ``streamGenerateContent`` server-sent events."""
        url = f"{self.base_url}/models/{self.model}:streamGenerateContent"
        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        usage: dict[str, int] = {}

        try:
            with (
                httpx.Client(timeout=self.timeout) as client,
                client.stream(
                    "POST",
                    url,
                    json=payload,
                    params={"key": self.api_key, "alt": "sse"},
                    headers={"Content-Type": "application/json"},
                ) as response,
            ):
                if response.status_code >= 400:
                    response.read()
                    raise ProviderError(
                        _explain_status(
                            httpx.HTTPStatusError(
                                "error", request=response.request, response=response
                            )
                        )
                    )

                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue

                    partial = self._parse(event)
                    if partial.content:
                        text_chunks.append(partial.content)
                        on_text(partial.content)
                    calls.extend(partial.tool_calls)
                    if partial.usage:
                        usage = partial.usage
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach {self.base_url}: {exc}") from exc

        return AssistantMessage(content="".join(text_chunks), tool_calls=calls, usage=usage)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}{path}",
                    json=payload,
                    params={"key": self.api_key},
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(_explain_status(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise ProviderError(f"{self.base_url} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError(f"Unexpected response shape from {self.base_url}: {data!r:.200}")
        return data


def _safe_int(value: Any) -> int:
    """A usage count that survived the wire: garbage becomes 0, not a crash."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _explain_status(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    try:
        err = exc.response.json().get("error")
        if isinstance(err, dict):
            detail = err.get("message") or str(err)
        elif err is not None:
            detail = str(err)
        else:
            detail = exc.response.text[:400]
    except Exception:  # noqa: BLE001
        detail = exc.response.text[:400]

    hints = {
        400: "Check the model id and that your key has the Generative Language API enabled.",
        401: "Your API key was rejected. Check GEMINI_API_KEY.",
        403: "Key valid but not permitted for this model, or the API is not enabled.",
        404: "Model not found. Try `jaigent models --only gemini`.",
        429: "Rate limit or quota exceeded. Wait and retry.",
    }
    hint = hints.get(status, "")
    return f"HTTP {status} from Gemini: {detail}" + (f"\nHint: {hint}" if hint else "")
