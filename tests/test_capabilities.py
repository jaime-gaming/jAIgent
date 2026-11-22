"""The agent's derived self-knowledge: context, capabilities and limits.

The point of this block is that it never lies. Every claim is read from the real
settings and the real tool registry, so an agent built read-only is told it
cannot write, an agent with no shell is told it has none, and an agent nobody is
watching is told not to ask a question. These tests pin that down surface by
surface, because a confident wrong answer about what the agent can do is worse
than no answer at all.
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeProvider
from jaigent.agent import Agent
from jaigent.approval import MUTATING_TOOLS
from jaigent.capabilities import describe
from jaigent.config import Settings
from jaigent.tools import ToolRegistry, build_default_registry


def _registry(settings: Settings, *, interactive: bool | None = None) -> ToolRegistry:
    return build_default_registry(settings, interactive=interactive)


def _read_only(settings: Settings) -> ToolRegistry:
    full = _registry(settings)
    safe = ToolRegistry()
    safe.extend(
        [t for t in full if t.name not in MUTATING_TOOLS and t.read_only and not t.dangerous]
    )
    return safe


def test_it_names_the_version_provider_and_surface(settings: Settings) -> None:
    text = describe(settings, _registry(settings), surface="in the terminal")

    assert "jAIgent" in text
    assert settings.provider in text
    assert "in the terminal" in text


def test_a_surface_is_never_invented(settings: Settings) -> None:
    text = describe(settings, _registry(settings))

    # No surface passed means no "running ..." clause rather than a guessed one.
    assert ", running" not in text


def test_shell_is_claimed_only_when_it_is_there(settings: Settings) -> None:
    without = describe(settings, _registry(settings))
    settings.allow_shell = True
    with_shell = describe(settings, _registry(settings))

    assert "you have no shell access" in without
    assert "run shell commands" not in without
    assert "run shell commands inside the workspace" in with_shell
    assert "you have no shell access" not in with_shell


def test_a_read_only_run_says_it_cannot_write(settings: Settings) -> None:
    text = describe(settings, _read_only(settings))

    assert "this is a read-only run, so you cannot change files" in text
    assert "create, edit and delete files" not in text


def test_a_writable_run_does_not_call_itself_read_only(settings: Settings) -> None:
    text = describe(settings, _registry(settings))

    assert "create, edit and delete files in the workspace" in text
    assert "read-only run" not in text


def test_nobody_watching_means_no_promise_to_ask(settings: Settings) -> None:
    attended = describe(settings, _registry(settings), interactive=None)
    unattended = describe(settings, _registry(settings), interactive=False)

    assert "ask the user one clarifying question" in attended
    assert "nobody can answer" not in attended

    assert "ask the user one clarifying question" not in unattended
    assert "nobody can answer a question this run, so use your own judgement" in unattended


def test_the_approval_mode_is_described_not_assumed(settings: Settings) -> None:
    settings.approval = "ask"
    ask = describe(settings, _registry(settings))
    settings.approval = "dry-run"
    dry = describe(settings, _registry(settings))
    settings.approval = "auto"
    auto = describe(settings, _registry(settings))

    assert "a human approves each mutating call" in ask
    assert "mutations are simulated, so nothing is written" in dry
    assert "mutations apply without asking" in auto


def test_the_step_and_budget_caps_are_stated(settings: Settings) -> None:
    settings.max_steps = 7
    settings.budget = 2.5
    text = describe(settings, _registry(settings))

    assert "you stop after 7 tool-calling steps" in text
    assert "you stop before this run costs $2.50" in text


def test_no_budget_means_no_budget_sentence(settings: Settings) -> None:
    settings.budget = 0.0
    text = describe(settings, _registry(settings))

    assert "costs $" not in text


def test_checkpoints_are_mentioned_only_when_changes_can_happen(settings: Settings) -> None:
    settings.checkpoints = True
    writable = describe(settings, _registry(settings))
    read_only = describe(settings, _read_only(settings))

    assert "checkpointed so they can be undone" in writable
    # A read-only run takes no checkpoints, so claiming undo would be a lie.
    assert "checkpointed so they can be undone" not in read_only


def test_memory_adds_a_capability_and_a_rule(settings: Settings) -> None:
    settings.memory = True
    text = describe(settings, _registry(settings))

    assert "save and recall standing notes" in text
    assert "standing notes persist between runs" in text


def test_the_block_reaches_the_system_prompt(settings: Settings) -> None:
    agent = Agent(
        settings,
        provider=FakeProvider([]),
        tools=_registry(settings),
        surface="in the terminal, as an interactive chat",
    )

    assert "Your context and capabilities" in agent.system_prompt
    assert "in the terminal, as an interactive chat" in agent.system_prompt
    assert "jAIgent" in agent.system_prompt


def test_a_custom_system_prompt_is_not_decorated(settings: Settings) -> None:
    agent = Agent(
        settings,
        provider=FakeProvider([]),
        tools=_registry(settings),
        system_prompt="You are a terse bot.",
        surface="in the terminal",
    )

    assert agent.system_prompt == "You are a terse bot."
    assert "Your context and capabilities" not in agent.system_prompt


def test_an_empty_system_prompt_stays_empty(settings: Settings) -> None:
    agent = Agent(
        settings,
        provider=FakeProvider([]),
        tools=ToolRegistry(),
        system_prompt="",
        surface="in the terminal",
    )

    assert agent.system_prompt == ""


def test_the_workspace_path_is_a_path_object_and_still_renders(
    settings: Settings, workspace: Path
) -> None:
    text = describe(settings, _registry(settings))

    # The block describes capabilities, not the path; the prompt already carries
    # the workspace. Just confirm nothing here crashes on a real Path workspace.
    assert isinstance(text, str) and text
    assert str(workspace) not in text
