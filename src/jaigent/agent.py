"""The agent loop: prompt → model → tools → model → … → answer."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jaigent.approval import Approver, Mode
from jaigent.checkpoint import CheckpointStore, paths_for_tool
from jaigent.config import DEFAULT_MODELS, Settings
from jaigent.llm import LLMProvider, ToolCall, get_provider
from jaigent.pricing import Cost, estimate, load_price_overrides
from jaigent.prompts import build_system_prompt
from jaigent.router import Routing, choose_model
from jaigent.tools import ToolRegistry, build_default_registry

#: Called with (tool_name, arguments, output) after each tool execution.
ToolObserver = Callable[[str, dict[str, Any], str], None]
#: Called before a tool runs, with its name and arguments.
ToolStartObserver = Callable[[str, dict[str, Any]], None]
#: Called with each chunk of assistant text as it streams in.
TextObserver = Callable[[str], None]


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
    cost: Cost = field(default_factory=Cost)
    #: Checkpoint ids captured during the run, oldest first.
    checkpoints: list[str] = field(default_factory=list)
    #: Set when ``model="auto"`` picked the model for this run.
    routing: Routing | None = None

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
        on_tool_start: Callback invoked just before every tool execution.
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
        on_tool_start: ToolStartObserver | None = None,
        on_failover: Callable[[Any], None] | None = None,
        on_text: TextObserver | None = None,
        approver: Approver | None = None,
        on_route: Callable[[Routing], None] | None = None,
        checkpoints: bool | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.tools = tools if tools is not None else build_default_registry(self.settings)
        #: An injected provider is honoured as-is; only an auto-built one is
        #: rebuilt when the router changes the model.
        self._owns_provider = provider is None
        self.on_failover = on_failover
        self.provider = provider or get_provider(self.settings)
        if self._owns_provider:
            self.provider = self._wrap_failover(self.provider)

        # Advertise skills in the prompt only when the tool to load them exists.
        catalogue = ""
        if "load_skill" in self.tools:
            from jaigent.skills import catalogue as render_catalogue
            from jaigent.skills import discover

            self.skills = discover()
            catalogue = render_catalogue(self.skills)
        else:
            self.skills = {}

        self.system_prompt = system_prompt or build_system_prompt(
            workspace=str(self.settings.workspace),
            tool_names=self.tools.names(),
            extra_instructions=instructions,
            skills_catalogue=catalogue,
        )
        self.on_tool_call = on_tool_call
        self.on_tool_start = on_tool_start
        self.on_text = on_text
        self.on_route = on_route

        enabled = self.settings.checkpoints if checkpoints is None else checkpoints
        #: Snapshots taken before mutating tools, so a run can be undone.
        self.checkpoints = CheckpointStore(self.settings.workspace) if enabled else None
        self._run_checkpoints: list[str] = []
        self.approver = approver or Approver(Mode.AUTO, workspace=self.settings.workspace)
        self.history: list[dict[str, Any]] = []
        self._prices = load_price_overrides()

    # ------------------------------------------------------------------
    @property
    def _stream_callback(self) -> TextObserver | None:
        """Stream only when a sink is set and the provider can do it."""
        if self.on_text is None or not getattr(self.provider, "supports_streaming", False):
            return None
        return self.on_text

    # ------------------------------------------------------------------
    def _wrap_failover(self, provider: LLMProvider) -> LLMProvider:
        """Wrap ``provider`` in failover when the setting is on."""
        if not self.settings.failover:
            return provider
        from jaigent.failover import FailoverPolicy, FailoverProvider

        if isinstance(provider, FailoverProvider):
            return provider
        return FailoverProvider(
            provider,
            self.settings,
            policy=FailoverPolicy(attempts=self.settings.retries),
            on_failover=lambda attempt: self.on_failover(attempt) if self.on_failover else None,
        )

    def _rebuild_owned_provider(self) -> None:
        """Rebuild the auto-constructed provider (and re-apply failover)."""
        self.provider = self._wrap_failover(get_provider(self.settings))

    def set_model(self, model: str) -> None:
        """Switch model mid-session, keeping failover if it was enabled."""
        self.settings = self.settings.merged_with(model=model)
        if self._owns_provider:
            self._rebuild_owned_provider()
        else:
            self.provider.model = model

    def reset(self) -> None:
        """Forget the conversation so far."""
        self.history = []

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        """Replace the conversation with a previously saved one.

        The system prompt is dropped if present: it is regenerated for the
        current workspace and toolset rather than restored from the session.
        """
        self.history = [m for m in messages if m.get("role") != "system"]

    def run(self, prompt: str, *, max_steps: int | None = None) -> AgentResult:
        """Run the agent until it produces a final answer or runs out of steps.

        Args:
            prompt: The user's request.
            max_steps: Overrides ``settings.max_steps`` for this call.

        Returns:
            An :class:`AgentResult` with the answer and a trace of tool calls.
        """
        budget = max_steps or self.settings.max_steps
        self._run_checkpoints = []
        routing = self._route(prompt)
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
                on_text=self._stream_callback,
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
                    cost=self._cost(usage),
                    routing=routing,
                    checkpoints=list(self._run_checkpoints),
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
            on_text=self._stream_callback,
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
            cost=self._cost(usage),
            routing=routing,
            checkpoints=list(self._run_checkpoints),
        )

    def _route(self, prompt: str) -> Routing | None:
        """Resolve ``model="auto"`` to a concrete model for this prompt.

        The provider is rebuilt with the chosen model. OmniRoute is left alone:
        ``auto`` is a real model id there, and the gateway does its own routing
        with live quota information we do not have.
        """
        if self.settings.model.strip().lower() != "auto":
            return None
        if self.settings.provider == "omniroute":
            return None

        routing = choose_model(
            prompt, self.settings.provider, fallback=DEFAULT_MODELS.get(self.settings.provider, "")
        )
        if not routing.model:
            return None

        self.set_model(routing.model)
        if self.on_route is not None:
            self.on_route(routing)
        return routing

    def _cost(self, usage: dict[str, int]) -> Cost:
        return estimate(self.settings.model, usage, overrides=self._prices)

    def chat(self, prompt: str, **kwargs: Any) -> str:
        """Convenience wrapper around :meth:`run` that keeps history and returns text."""
        return self.run(prompt, **kwargs).output

    # ------------------------------------------------------------------
    def _execute(self, call: ToolCall, step: int, steps: list[StepRecord]) -> str:
        started = time.perf_counter()
        if self.settings.verbose:
            print(f"  → {call.name}({_preview(call.arguments)})", file=sys.stderr, flush=True)

        # Announce the tool *before* running it, so a status line can name what
        # is happening now rather than what has already finished.
        if self.on_tool_start is not None:
            self.on_tool_start(call.name, call.arguments)

        # Snapshot first, so even an approved change can be undone later.
        if self.checkpoints is not None:
            targets = paths_for_tool(call.name, call.arguments)
            if targets:
                snapshot = self.checkpoints.capture(
                    targets, label=f"{call.name} {targets[0]}", tool=call.name
                )
                if snapshot is not None:
                    self._run_checkpoints.append(snapshot.id)

        # Ask before anything that changes the filesystem or runs a command.
        decision = self.approver.check(call.name, call.arguments)
        if not decision.allowed:
            output = f"ERROR: {decision.reason}"
        else:
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
