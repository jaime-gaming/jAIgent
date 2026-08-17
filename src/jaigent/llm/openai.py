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
from jaigent.llm.base import AssistantMessage, LLMProvider, ToolCall
from jaigent.tools import ToolRegistry


class OpenAIProvider(LLMProvider):
    """Talks to any ``POST {base_url}/chat/completions`` endpoint."""

    name = "openai"

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: ToolRegistry | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> AssistantMessage:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools is not None and len(tools) > 0:
            payload["tools"] = tools.to_openai_schema()
            payload["tool_choice"] = "auto"

        data = self._post("/chat/completions", payload)

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
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
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
        401: "Your API key was rejected. Check OPENAI_API_KEY / JAIGENT_API_KEY.",
        403: "Your key is valid but not allowed to use this model.",
        404: "Model or endpoint not found. Check JAIGENT_MODEL and JAIGENT_BASE_URL.",
        429: "Rate limit or quota exceeded. Wait and retry, or check your billing.",
    }
    hint = hints.get(status, "")
    return f"HTTP {status} from the provider: {detail}" + (f"\nHint: {hint}" if hint else "")
