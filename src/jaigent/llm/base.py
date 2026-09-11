"""Provider-agnostic chat interface.

Providers translate the neutral message/tool shapes defined here into whatever
their HTTP API expects, and translate the response back into an
:class:`AssistantMessage`. The agent loop only ever sees these types.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jaigent.tools import ToolRegistry

#: Called with each chunk of assistant text as it arrives from the provider.
TextStream = Callable[[str], None]


def stream_index(event: dict[str, Any]) -> int:
    """Which tool-call slot a streaming event belongs to.

    The wire index is untrusted input — proxies and OpenAI-compatible
    gateways send ``null``, strings, or nothing at all. Garbage maps to
    slot 0 instead of crashing the turn with ``TypeError``.
    """
    try:
        return int(event.get("index") or 0)
    except (TypeError, ValueError):
        return 0


def text_content(value: Any) -> str:
    """Best-effort plain text from a message ``content`` field.

    Providers and clients send content as a string, ``null``, or a list of
    blocks (``{"type": "text", "text": ...}``, images, tool payloads). Only
    the text survives: anything else stringifies to garbage like ``"None"``
    or ``"[{'type': ...}]"``, which used to be sent to the model as the
    prompt.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(text, (int, float, bool)):
                    parts.append(str(text))
        return "".join(parts)
    if isinstance(value, dict):
        # A lone block where a string belongs (hand-built history, a client
        # that sends one object instead of a list): unwrap it.
        for key in ("text", "content"):
            found = value.get(key)
            if isinstance(found, str):
                return found
            if isinstance(found, (int, float, bool)):
                return str(found)
            if isinstance(found, (list, dict)):
                return text_content(found)
    return str(value)


@dataclass(slots=True, frozen=True)
class ToolCall:
    """A request from the model to run one tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class AssistantMessage:
    """One assistant turn: free text, tool calls, or both."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Tool arguments as ``(dict, raw_string_or_None)``.

    OpenAI sends arguments as a JSON string, Anthropic and Gemini as an
    object. The raw string is kept so the OpenAI renderer can resend it
    byte-identical; anything unparsable becomes ``{}`` rather than a crash.
    """
    if isinstance(raw, dict):
        return dict(raw), None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}, raw
        if isinstance(parsed, dict):
            return parsed, raw
        return {}, raw
    return {}, None


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite a conversation into the canonical shape every provider renders.

    Each provider stores history in its own wire format — OpenAI uses
    ``role: tool`` messages, Anthropic nests everything in content blocks,
    Gemini pairs calls with responses by position. That meant failover (or a
    resumed session) across providers sent malformed history and the fallback
    always 400'd: "falls through to the next provider" only worked within one
    API family. Normalizing on entry fixes every crossing at once.

    Canonical messages are fresh dicts (the input is never mutated):

    * ``{"role": "system" | "user", "content": str}``
    * ``{"role": "assistant", "content": str, "tool_calls": [...]}``
    * ``{"role": "tool", "tool_call_id": str, "name": str, "content": str}``

    with calls as ``{"id", "name", "arguments", "raw"}``. Gemini tool results
    carry no ids, so those are matched positionally to the preceding turn's
    calls; anything still orphaned gets a synthesised id rather than being
    dropped.
    """
    normalised: list[dict[str, Any]] = []
    id_to_name: dict[str, str] = {}
    counter = 0

    def fresh_id() -> str:
        nonlocal counter
        counter += 1
        return f"call_{counter}"

    pending: list[str] = []

    def take_id(explicit: Any) -> str:
        if isinstance(explicit, str) and explicit:
            return explicit
        if pending:
            return pending.pop(0)
        return fresh_id()

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")

        if role == "system":
            normalised.append({"role": "system", "content": text_content(message.get("content"))})
            continue

        if role == "assistant":
            calls: list[dict[str, Any]] = []
            raw_calls = message.get("tool_calls") or []
            if not isinstance(raw_calls, list):
                raw_calls = []
            for item in raw_calls:
                if not isinstance(item, dict):
                    continue
                function = item.get("function")
                if not isinstance(function, dict):
                    function = item
                arguments, raw = _parse_arguments(function.get("arguments"))
                call_id = item.get("id") if isinstance(item.get("id"), str) else ""
                name = function.get("name") or item.get("name") or ""
                call_id = call_id or fresh_id()
                id_to_name[call_id] = str(name)
                calls.append({"id": call_id, "name": str(name), "arguments": arguments, "raw": raw})
            content = message.get("content")
            if isinstance(content, list):
                # Anthropic history: tool_use blocks live in the content.
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    call_id = block.get("id")
                    call_id = call_id if isinstance(call_id, str) and call_id else fresh_id()
                    name = str(block.get("name") or "")
                    arguments, _ = _parse_arguments(block.get("input"))
                    id_to_name[call_id] = name
                    calls.append({"id": call_id, "name": name, "arguments": arguments, "raw": None})
            pending = [call["id"] for call in calls]
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": text_content(message.get("content")),
            }
            if calls:
                entry["tool_calls"] = calls
            normalised.append(entry)
            continue

        if role == "tool":
            call_id = take_id(message.get("tool_call_id") or message.get("id"))
            name = message.get("name") or id_to_name.get(call_id, "")
            normalised.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": str(name),
                    "content": text_content(message.get("content")),
                }
            )
            continue

        if role == "user" and isinstance(message.get("content"), list):
            # Anthropic history: text and tool_result blocks share one message.
            text = text_content(message.get("content"))
            if text:
                normalised.append({"role": "user", "content": text})
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = take_id(block.get("tool_use_id"))
                normalised.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": str(id_to_name.get(call_id, "")),
                        "content": text_content(block.get("content")),
                    }
                )
            continue

        # Plain user turns and anything else (e.g. OpenAI's `developer` role,
        # which each renderer maps as its API requires).
        normalised.append(
            {"role": str(role or "user"), "content": text_content(message.get("content"))}
        )

    return normalised


class LLMProvider(ABC):
    """Minimal contract every backend must satisfy."""

    name: str = "base"

    def __init__(self, *, api_key: str, model: str, base_url: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    #: Whether this provider implements incremental streaming.
    supports_streaming: bool = False

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: ToolRegistry | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        on_text: TextStream | None = None,
    ) -> AssistantMessage:
        """Send the conversation and return the next assistant message.

        Args:
            messages: Conversation so far, in this provider's wire format.
            tools: Tools to advertise. ``None`` forces a text-only reply.
            temperature: Sampling temperature.
            max_tokens: Cap on generated tokens.
            on_text: When given and the provider supports streaming, called with
                each chunk of assistant text as it arrives. The complete message
                is still returned at the end.
        """

    @abstractmethod
    def format_tool_result(self, call: ToolCall, output: str) -> dict[str, Any]:
        """Wrap a tool's output as a message the provider will accept."""

    @abstractmethod
    def format_assistant_message(self, message: AssistantMessage) -> dict[str, Any]:
        """Serialise an assistant turn back into conversation history."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} model={self.model!r}>"
