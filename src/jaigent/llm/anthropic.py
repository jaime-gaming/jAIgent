"""Anthropic Messages API provider."""

from __future__ import annotations

import json
from typing import Any

import httpx

from jaigent.errors import ProviderError
from jaigent.llm.base import AssistantMessage, LLMProvider, ToolCall
from jaigent.tools import ToolRegistry

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    """Talks to ``POST {base_url}/messages`` with native tool use."""

    name = "anthropic"

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: ToolRegistry | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> AssistantMessage:
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        convo = [m for m in messages if m.get("role") != "system"]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": convo,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(str(part) for part in system_parts)
        if tools is not None and len(tools) > 0:
            payload["tools"] = tools.to_anthropic_schema()

        data = self._post("/messages", payload)

        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content", []):
            kind = block.get("type")
            if kind == "text":
                text_chunks.append(block.get("text", ""))
            elif kind == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.get("id", f"call_{len(calls)}"),
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                    )
                )

        return AssistantMessage(
            content="".join(text_chunks),
            tool_calls=calls,
            raw=data,
            usage=data.get("usage") or {},
        )

    def format_assistant_message(self, message: AssistantMessage) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if message.content:
            content.append({"type": "text", "text": message.content})
        content.extend(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            for call in message.tool_calls
        )
        return {"role": "assistant", "content": content or [{"type": "text", "text": ""}]}

    def format_tool_result(self, call: ToolCall, output: str) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call.id, "content": output}],
        }

    # ------------------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
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
        401: "Your API key was rejected. Check ANTHROPIC_API_KEY / JAIGENT_API_KEY.",
        404: "Model not found. Check JAIGENT_MODEL.",
        429: "Rate limit exceeded. Wait and retry.",
        529: "Anthropic is overloaded. Retry in a moment.",
    }
    hint = hints.get(status, "")
    return f"HTTP {status} from the provider: {detail}" + (f"\nHint: {hint}" if hint else "")
