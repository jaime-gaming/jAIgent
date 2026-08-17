"""The agent loop: prompt → model → tools → model → … → answer."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jaigent.config import Settings
from jaigent.llm import LLMProvider, ToolCall, get_provider
from jaigent.prompts import build_system_prompt
from jaigent.tools import ToolRegistry, build_default_registry

#: Called with (tool_name, arguments, output) after each tool execution.
ToolObserver = Callable[[str, dict[str, Any], str], None]


@dataclass(slots=True)
class StepRecord:
    """What happened during one tool call, for tracing and tests."""

    step: int
    tool: str
    arguments: dict[str, Any]
    output: str
    duration: float


@dataclass(slots=True)
class AgentResult:
    """The outcome of :meth:`Agent.run`."""

    output: str
    steps: list[StepRecord] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stopped_early: bool = False

    @property
    def tool_calls(self) -> int:
        return len(self.steps)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.output


class Agent:
    """An LLM wired to a set of tools, run in a loop until it answers.

    Example::

        from jaigent import Agent, Settings

        agent = Agent(settings=Settings.from_env())
        result = agent.run("What changed in Python 3.13? Save a summary to notes.md")
        print(result.output)

    Args:
        settings: Configuration; defaults to :meth:`Settings.from_env`.
        tools: Custom registry; defaults to :func:`build_default_registry`.
        provider: Pre-built provider, mostly useful for tests.
        system_prompt: Overrides the generated system prompt entirely.
        instructions: Extra guidance appended to the generated system prompt.
        on_tool_call: Callback invoked after every tool execution.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        tools: ToolRegistry | None = None,
        provider: LLMProvider | None = None,
        system_prompt: str | None = None,
        instructions: str | None = None,
        on_tool_call: ToolObserver | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.tools = tools if tools is not None else build_default_registry(self.settings)
        self.provider = provider or get_provider(self.settings)
        self.system_prompt = system_prompt or build_system_prompt(
            workspace=str(self.settings.workspace),
            tool_names=self.tools.names(),
            extra_instructions=instructions,
        )
        self.on_tool_call = on_tool_call
        self.history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Forget the conversation so far."""
        self.history = []

    def run(self, prompt: str, *, max_steps: int | None = None) -> AgentResult:
        """Run the agent until it produces a final answer or runs out of steps.

        Args:
            prompt: The user's request.
            max_steps: Overrides ``settings.max_steps`` for this call.

        Returns:
            An :class:`AgentResult` with the answer and a trace of tool calls.
        """
        budget = max_steps or self.settings.max_steps
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *self.history,
            {"role": "user", "content": prompt},
        ]
        steps: list[StepRecord] = []
        usage: dict[str, int] = {}

        for step in range(1, budget + 1):
            reply = self.provider.complete(
                messages,
                self.tools,
                temperature=self.settings.temperature,
                max_tokens=self.settings.max_tokens,
            )
            _accumulate_usage(usage, reply.usage)
            messages.append(self.provider.format_assistant_message(reply))

            if not reply.wants_tools:
                self.history = messages[1:]
                return AgentResult(
                    output=reply.content.strip(),
                    steps=steps,
                    messages=messages,
                    usage=usage,
                )

            for call in reply.tool_calls:
                output = self._execute(call, step, steps)
                messages.append(self.provider.format_tool_result(call, output))

        # Budget exhausted: ask for a final answer with tools switched off.
        messages.append(
            {
                "role": "user",
                "content": (
                    f"You have used all {budget} tool steps. Stop calling tools and answer now "
                    "with what you already know, noting anything you could not verify."
                ),
            }
        )
        final = self.provider.complete(
            messages,
            None,
            temperature=self.settings.temperature,
            max_tokens=self.settings.max_tokens,
        )
        _accumulate_usage(usage, final.usage)
        messages.append(self.provider.format_assistant_message(final))
        self.history = messages[1:]
        return AgentResult(
            output=final.content.strip(),
            steps=steps,
            messages=messages,
            usage=usage,
            stopped_early=True,
        )

    def chat(self, prompt: str, **kwargs: Any) -> str:
        """Convenience wrapper around :meth:`run` that keeps history and returns text."""
        return self.run(prompt, **kwargs).output

    # ------------------------------------------------------------------
    def _execute(self, call: ToolCall, step: int, steps: list[StepRecord]) -> str:
        started = time.perf_counter()
        if self.settings.verbose:
            print(f"  → {call.name}({_preview(call.arguments)})", file=sys.stderr, flush=True)

        output = self.tools.call(call.name, call.arguments)
        duration = time.perf_counter() - started
        steps.append(
            StepRecord(
                step=step,
                tool=call.name,
                arguments=call.arguments,
                output=output,
                duration=duration,
            )
        )
        if self.settings.verbose:
            first_line = output.splitlines()[0] if output else ""
            print(f"    {first_line[:160]} [{duration:.2f}s]", file=sys.stderr, flush=True)
        if self.on_tool_call is not None:
            self.on_tool_call(call.name, call.arguments, output)
        return output


def _accumulate_usage(total: dict[str, int], new: dict[str, Any]) -> None:
    for key, value in (new or {}).items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def _preview(arguments: dict[str, Any], limit: int = 100) -> str:
    parts = []
    for key, value in arguments.items():
        text = str(value).replace("\n", "\\n")
        if len(text) > 40:
            text = text[:40] + "…"
        parts.append(f"{key}={text!r}")
    joined = ", ".join(parts)
    return joined if len(joined) <= limit else joined[:limit] + "…"
