"""The agent loop, driven by a scripted fake provider."""

from __future__ import annotations

from pathlib import Path

from conftest import FakeProvider
from jaigent.agent import Agent
from jaigent.config import Settings
from jaigent.llm.base import AssistantMessage, ToolCall


def make_agent(settings: Settings, script: list[AssistantMessage]) -> tuple[Agent, FakeProvider]:
    provider = FakeProvider(script)
    return Agent(settings, provider=provider), provider


def test_answers_without_tools(settings: Settings) -> None:
    agent, _ = make_agent(settings, [AssistantMessage(content="42")])
    result = agent.run("What is six times seven?")

    assert result.output == "42"
    assert result.tool_calls == 0
    assert result.stopped_early is False


def test_executes_a_tool_then_answers(settings: Settings, workspace: Path) -> None:
    script = [
        AssistantMessage(tool_calls=[ToolCall("c1", "read_file", {"path": "notes.md"})]),
        AssistantMessage(content="The notes say hello world."),
    ]
    agent, _ = make_agent(settings, script)
    result = agent.run("What is in notes.md?")

    assert result.output == "The notes say hello world."
    assert result.tool_calls == 1
    assert result.steps[0].tool == "read_file"
    assert "hello world" in result.steps[0].output


def test_writes_a_file_through_a_tool(settings: Settings, workspace: Path) -> None:
    script = [
        AssistantMessage(
            tool_calls=[ToolCall("c1", "write_file", {"path": "out.txt", "content": "written"})]
        ),
        AssistantMessage(content="Saved to out.txt"),
    ]
    agent, _ = make_agent(settings, script)
    agent.run("Write a file")

    assert (workspace / "out.txt").read_text(encoding="utf-8") == "written"


def test_multiple_tool_calls_in_one_turn(settings: Settings) -> None:
    script = [
        AssistantMessage(
            tool_calls=[
                ToolCall("c1", "read_file", {"path": "notes.md"}),
                ToolCall("c2", "list_files", {}),
            ]
        ),
        AssistantMessage(content="done"),
    ]
    agent, _ = make_agent(settings, script)
    result = agent.run("Inspect the project")

    assert result.tool_calls == 2
    assert [s.tool for s in result.steps] == ["read_file", "list_files"]


def test_tool_errors_are_fed_back_not_raised(settings: Settings) -> None:
    script = [
        AssistantMessage(tool_calls=[ToolCall("c1", "read_file", {"path": "../../etc/passwd"})]),
        AssistantMessage(content="I cannot read outside the workspace."),
    ]
    agent, _ = make_agent(settings, script)
    result = agent.run("Read the password file")

    assert "ERROR" in result.steps[0].output
    assert "outside the workspace" in result.steps[0].output
    assert result.output == "I cannot read outside the workspace."


def test_unknown_tool_is_reported_to_the_model(settings: Settings) -> None:
    script = [
        AssistantMessage(tool_calls=[ToolCall("c1", "teleport", {})]),
        AssistantMessage(content="No such tool."),
    ]
    agent, _ = make_agent(settings, script)
    result = agent.run("Teleport")

    assert "Unknown tool" in result.steps[0].output


def test_step_budget_forces_a_final_answer(settings: Settings) -> None:
    # Exactly max_steps tool turns, then the forced no-tools final answer.
    looping = [AssistantMessage(tool_calls=[ToolCall(f"c{i}", "list_files", {})]) for i in range(3)]
    script = [*looping, AssistantMessage(content="I ran out of steps.")]
    agent, _ = make_agent(settings.merged_with(max_steps=3), script)
    result = agent.run("Loop forever")

    assert result.stopped_early is True
    assert result.tool_calls == 3
    assert result.output == "I ran out of steps."


def test_history_persists_between_runs(settings: Settings) -> None:
    agent, provider = make_agent(
        settings, [AssistantMessage(content="first"), AssistantMessage(content="second")]
    )
    agent.run("one")
    agent.run("two")

    last_conversation = provider.calls[-1]
    contents = [m.get("content") for m in last_conversation]
    assert "one" in contents
    assert "first" in contents


def test_reset_clears_history(settings: Settings) -> None:
    agent, provider = make_agent(
        settings, [AssistantMessage(content="first"), AssistantMessage(content="second")]
    )
    agent.run("one")
    agent.reset()
    agent.run("two")

    assert len(provider.calls[-1]) == 2  # system + new user message only


def test_on_tool_call_observer(settings: Settings) -> None:
    seen: list[tuple[str, dict, str]] = []
    provider = FakeProvider(
        [
            AssistantMessage(tool_calls=[ToolCall("c1", "list_files", {})]),
            AssistantMessage(content="ok"),
        ]
    )
    agent = Agent(settings, provider=provider, on_tool_call=lambda n, a, o: seen.append((n, a, o)))
    agent.run("list")

    assert len(seen) == 1
    assert seen[0][0] == "list_files"


def test_usage_is_accumulated(settings: Settings) -> None:
    script = [
        AssistantMessage(tool_calls=[ToolCall("c1", "list_files", {})], usage={"total_tokens": 10}),
        AssistantMessage(content="ok", usage={"total_tokens": 5}),
    ]
    agent, _ = make_agent(settings, script)
    assert agent.run("x").usage["total_tokens"] == 15


def test_chat_returns_plain_text(settings: Settings) -> None:
    agent, _ = make_agent(settings, [AssistantMessage(content="  hi  ")])
    assert agent.chat("hello") == "hi"


def test_system_prompt_mentions_workspace_and_tools(settings: Settings) -> None:
    agent, _ = make_agent(settings, [])
    assert str(settings.workspace) in agent.system_prompt
    assert "web_search" in agent.system_prompt


def test_custom_instructions_are_appended(settings: Settings) -> None:
    provider = FakeProvider([])
    agent = Agent(settings, provider=provider, instructions="Always answer in Catalan.")
    assert "Always answer in Catalan." in agent.system_prompt


def test_shell_tool_absent_unless_enabled(settings: Settings) -> None:
    agent, _ = make_agent(settings, [])
    assert "run_command" not in agent.tools

    enabled, _ = make_agent(settings.merged_with(allow_shell=True), [])
    assert "run_command" in enabled.tools
