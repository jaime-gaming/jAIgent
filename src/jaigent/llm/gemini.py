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
from jaigent.llm.base import AssistantMessage, LLMProvider, TextStream, ToolCall
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

        for message in messages:
            role = message.get("role")

            if role == "system":
                system_parts.append(str(message.get("content", "")))
                continue

            if role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": message.get("name", "tool"),
                                    "response": {"result": message.get("content", "")},
                                }
                            }
                        ],
                    }
                )
                continue

            if role == "assistant":
                parts: list[dict[str, Any]] = []
                if message.get("content"):
                    parts.append({"text": message["content"]})
                for call in message.get("tool_calls") or []:
                    function = call.get("function", call)
                    raw = function.get("arguments", {})
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw or "{}")
                        except json.JSONDecodeError:
                            raw = {}
                    parts.append({"functionCall": {"name": function.get("name", ""), "args": raw}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
                continue

            contents.append({"role": "user", "parts": [{"text": str(message.get("content", ""))}]})

        return contents, "\n\n".join(system_parts)

    def _parse(self, data: dict[str, Any]) -> AssistantMessage:
        """Turn one ``generateContent`` response into an AssistantMessage."""
        candidates = data.get("candidates") or []
        text_chunks: list[str] = []
        calls: list[ToolCall] = []

        if candidates:
            for part in candidates[0].get("content", {}).get("parts", []) or []:
                if "text" in part:
                    text_chunks.append(part["text"])
                elif "functionCall" in part:
                    function = part["functionCall"]
                    calls.append(
                        ToolCall(
                            id=f"call_{len(calls)}",
                            name=function.get("name", ""),
                            arguments=function.get("args") or {},
                        )
                    )

        usage_raw = data.get("usageMetadata") or {}
        usage = {
            "prompt_tokens": int(usage_raw.get("promptTokenCount", 0)),
            "completion_tokens": int(usage_raw.get("candidatesTokenCount", 0)),
            "total_tokens": int(usage_raw.get("totalTokenCount", 0)),
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
        400: "Check the model id and that your key has the Generative Language API enabled.",
        401: "Your API key was rejected. Check GEMINI_API_KEY.",
        403: "Key valid but not permitted for this model, or the API is not enabled.",
        404: "Model not found. Try `jaigent models --only gemini`.",
        429: "Rate limit or quota exceeded. Wait and retry.",
    }
    hint = hints.get(status, "")
    return f"HTTP {status} from Gemini: {detail}" + (f"\nHint: {hint}" if hint else "")
