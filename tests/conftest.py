"""Shared fixtures and a scripted fake LLM provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jaigent.config import Settings
from jaigent.llm.base import AssistantMessage, LLMProvider, ToolCall
from jaigent.tools import ToolRegistry


class FakeProvider(LLMProvider):
    """Replays a scripted list of :class:`AssistantMessage` objects.

    Lets the agent loop be tested end to end without a network or an API key.
    """

    name = "fake"

    supports_streaming = True

    def __init__(
        self, script: list[AssistantMessage] | None = None, *, chunk_size: int = 4
    ) -> None:
        super().__init__(api_key="test-key", model="fake-model", base_url="http://localhost")
        self.script = list(script or [])
        self.calls: list[list[dict[str, Any]]] = []
        self.streamed: list[str] = []
        self.chunk_size = chunk_size

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: ToolRegistry | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        on_text: Any = None,
    ) -> AssistantMessage:
        self.calls.append([dict(m) for m in messages])
        reply = self.script.pop(0) if self.script else AssistantMessage(content="done")

        # Emit the text in chunks the way a real streaming provider would.
        if on_text is not None and reply.content:
            for start in range(0, len(reply.content), self.chunk_size):
                chunk = reply.content[start : start + self.chunk_size]
                self.streamed.append(chunk)
                on_text(chunk)
        return reply

    def format_assistant_message(self, message: AssistantMessage) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls
            ],
        }

    def format_tool_result(self, call: ToolCall, output: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": call.id, "content": output}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An isolated workspace pre-populated with a couple of files."""
    (tmp_path / "notes.md").write_text("# Notes\nhello world\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def settings(workspace: Path) -> Settings:
    return Settings(
        provider="openai",
        model="fake-model",
        api_key="test-key",
        workspace=workspace,
        max_steps=5,
    )


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every jaigent-related variable so tests see a pristine environment."""
    for var in (
        "JAIGENT_PROVIDER",
        "JAIGENT_MODEL",
        "JAIGENT_API_KEY",
        "JAIGENT_BASE_URL",
        "JAIGENT_WORKSPACE",
        "JAIGENT_MAX_STEPS",
        "JAIGENT_TEMPERATURE",
        "JAIGENT_MAX_TOKENS",
        "JAIGENT_TIMEOUT",
        "JAIGENT_SEARCH_BACKEND",
        "JAIGENT_ALLOW_SHELL",
        "JAIGENT_VERBOSE",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TAVILY_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
