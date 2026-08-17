"""Provider-agnostic chat interface.

Providers translate the neutral message/tool shapes defined here into whatever
their HTTP API expects, and translate the response back into an
:class:`AssistantMessage`. The agent loop only ever sees these types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from jaigent.tools import ToolRegistry


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


class LLMProvider(ABC):
    """Minimal contract every backend must satisfy."""

    name: str = "base"

    def __init__(self, *, api_key: str, model: str, base_url: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: ToolRegistry | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> AssistantMessage:
        """Send the conversation and return the next assistant message."""

    @abstractmethod
    def format_tool_result(self, call: ToolCall, output: str) -> dict[str, Any]:
        """Wrap a tool's output as a message the provider will accept."""

    @abstractmethod
    def format_assistant_message(self, message: AssistantMessage) -> dict[str, Any]:
        """Serialise an assistant turn back into conversation history."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} model={self.model!r}>"
