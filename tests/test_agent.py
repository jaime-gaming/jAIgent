"""The agent loop, driven by a scripted fake provider."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from conftest import FakeProvider
from jaigent.agent import Agent
from jaigent.approval import Approver, Mode
from jaigent.config import DEFAULT_BASE_URLS, DEFAULT_MODELS, Settings
from jaigent.errors import ConfigurationError
from jaigent.failover import FailoverProvider
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


class TestOnToolStart:
    """Fired *before* a tool runs, so the status line can name it while it works."""

    def _provider(self) -> FakeProvider:
        return FakeProvider(
            [
                AssistantMessage(tool_calls=[ToolCall("c1", "list_files", {"path": "."})]),
                AssistantMessage(content="ok"),
            ]
        )

    def test_observer_is_called_with_name_and_arguments(self, settings: Settings) -> None:
        seen: list[tuple[str, dict]] = []
        agent = Agent(
            settings,
            provider=self._provider(),
            on_tool_start=lambda name, args: seen.append((name, args)),
        )
        agent.run("list")

        assert seen == [("list_files", {"path": "."})]

    def test_it_fires_before_the_tool_runs(self, settings: Settings) -> None:
        order: list[str] = []
        agent = Agent(
            settings,
            provider=self._provider(),
            on_tool_start=lambda name, args: order.append(f"start:{name}"),
            on_tool_call=lambda name, args, out: order.append(f"done:{name}"),
        )
        agent.run("list")

        assert order == ["start:list_files", "done:list_files"]

    def test_it_can_also_be_assigned_after_construction(self, settings: Settings) -> None:
        seen: list[str] = []
        agent = Agent(settings, provider=self._provider())
        agent.on_tool_start = lambda name, args: seen.append(name)
        agent.run("list")

        assert seen == ["list_files"]

    def test_absent_observer_is_harmless(self, settings: Settings) -> None:
        agent = Agent(settings, provider=self._provider())
        assert agent.run("list").output == "ok"


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


class TestApprovalIntegration:
    """The agent must consult the approver before mutating tools."""

    def _agent(self, settings: Settings, mode: Mode, answers: list[str] | None = None):  # noqa: ANN202
        queue = list(answers or [])
        approver = Approver(
            mode,
            console=Console(width=80, no_color=True),
            prompt=lambda _: queue.pop(0) if queue else "n",
            workspace=settings.workspace,
        )
        provider = FakeProvider(
            [
                AssistantMessage(
                    tool_calls=[
                        ToolCall("c1", "write_file", {"path": "out.txt", "content": "data"})
                    ]
                ),
                AssistantMessage(content="finished"),
            ]
        )
        return Agent(settings, provider=provider, approver=approver)

    def test_auto_mode_writes_the_file(self, settings: Settings, workspace: Path) -> None:
        self._agent(settings, Mode.AUTO).run("write it")
        assert (workspace / "out.txt").read_text(encoding="utf-8") == "data"

    def test_dry_run_blocks_the_write(self, settings: Settings, workspace: Path) -> None:
        result = self._agent(settings, Mode.DRY_RUN).run("write it")

        assert not (workspace / "out.txt").exists()
        assert "dry-run" in result.steps[0].output

    def test_declining_blocks_the_write(self, settings: Settings, workspace: Path) -> None:
        result = self._agent(settings, Mode.ASK, ["n"]).run("write it")

        assert not (workspace / "out.txt").exists()
        assert "declined" in result.steps[0].output

    def test_accepting_allows_the_write(self, settings: Settings, workspace: Path) -> None:
        self._agent(settings, Mode.ASK, ["y"]).run("write it")
        assert (workspace / "out.txt").exists()

    def test_reads_are_never_gated(self, settings: Settings) -> None:
        approver = Approver(
            Mode.ASK,
            console=Console(width=80, no_color=True),
            prompt=lambda _: pytest.fail("read_file must not prompt"),
            workspace=settings.workspace,
        )
        provider = FakeProvider(
            [
                AssistantMessage(tool_calls=[ToolCall("c1", "read_file", {"path": "notes.md"})]),
                AssistantMessage(content="read it"),
            ]
        )
        result = Agent(settings, provider=provider, approver=approver).run("read")
        assert "hello world" in result.steps[0].output

    def test_default_agent_does_not_prompt(self, settings: Settings, workspace: Path) -> None:
        # The library default is AUTO so `Agent(...).run(...)` never blocks.
        provider = FakeProvider(
            [
                AssistantMessage(
                    tool_calls=[ToolCall("c1", "write_file", {"path": "d.txt", "content": "x"})]
                ),
                AssistantMessage(content="ok"),
            ]
        )
        Agent(settings, provider=provider).run("go")
        assert (workspace / "d.txt").exists()


class TestCostReporting:
    def test_cost_is_estimated_from_usage(self, settings: Settings) -> None:
        provider = FakeProvider(
            [
                AssistantMessage(
                    content="hi", usage={"prompt_tokens": 1000, "completion_tokens": 500}
                )
            ]
        )
        result = Agent(settings.merged_with(model="gpt-4o-mini"), provider=provider).run("x")

        assert result.cost.input_tokens == 1000
        assert result.cost.output_tokens == 500
        assert result.cost.usd == pytest.approx(1000 * 0.15 / 1e6 + 500 * 0.60 / 1e6)

    def test_cost_accumulates_across_steps(self, settings: Settings) -> None:
        provider = FakeProvider(
            [
                AssistantMessage(
                    tool_calls=[ToolCall("c1", "list_files", {})],
                    usage={"prompt_tokens": 100, "completion_tokens": 10},
                ),
                AssistantMessage(
                    content="done", usage={"prompt_tokens": 200, "completion_tokens": 20}
                ),
            ]
        )
        result = Agent(settings.merged_with(model="gpt-4o-mini"), provider=provider).run("x")

        assert result.cost.input_tokens == 300
        assert result.cost.output_tokens == 30

    def test_unknown_model_reports_tokens_without_price(self, settings: Settings) -> None:
        provider = FakeProvider(
            [AssistantMessage(content="hi", usage={"prompt_tokens": 50, "completion_tokens": 5})]
        )
        result = Agent(settings.merged_with(model="my-local-model"), provider=provider).run("x")

        assert result.cost.total_tokens == 55
        assert result.cost.usd is None


class TestHistoryRestore:
    def test_load_history_restores_a_conversation(self, settings: Settings) -> None:
        agent, provider = make_agent(settings, [AssistantMessage(content="second")])
        agent.load_history(
            [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
        )
        agent.run("now")

        contents = [m.get("content") for m in provider.calls[-1]]
        assert "earlier" in contents
        assert "reply" in contents

    def test_stale_system_prompt_is_dropped(self, settings: Settings) -> None:
        agent, provider = make_agent(settings, [AssistantMessage(content="x")])
        agent.load_history(
            [{"role": "system", "content": "OLD PROMPT"}, {"role": "user", "content": "hi"}]
        )
        agent.run("again")

        systems = [m for m in provider.calls[-1] if m.get("role") == "system"]
        assert len(systems) == 1
        assert "OLD PROMPT" not in systems[0]["content"]


class TestOwnedProviderKeepsFailover:
    """Auto-routing and set_model must not strip the FailoverProvider wrapper."""

    def test_auto_route_rebuilds_failover(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "jaigent.agent.get_provider",
            lambda configured: FakeProvider([AssistantMessage(content="ok")]),
        )
        agent = Agent(settings.merged_with(model="auto", failover=True))

        assert isinstance(agent.provider, FailoverProvider)
        agent.run("hi")
        assert isinstance(agent.provider, FailoverProvider)

    def test_set_model_rebuilds_failover(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "jaigent.agent.get_provider",
            lambda configured: FakeProvider([AssistantMessage(content="ok")]),
        )
        agent = Agent(settings.merged_with(failover=True))
        agent.set_model("gpt-4o")

        assert isinstance(agent.provider, FailoverProvider)
        assert agent.settings.model == "gpt-4o"

    def test_injected_provider_is_not_wrapped(self, settings: Settings) -> None:
        provider = FakeProvider([AssistantMessage(content="ok")])
        agent = Agent(settings.merged_with(failover=True), provider=provider)

        assert agent.provider is provider


class TestSetProvider:
    def test_switches_url_and_default_model(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        agent, _ = make_agent(settings, [])
        agent.set_provider("groq")

        assert agent.settings.provider == "groq"
        assert agent.settings.base_url == DEFAULT_BASE_URLS["groq"]
        assert agent.settings.model == DEFAULT_MODELS["groq"]
        assert agent.settings.api_key == "gsk-test"

    def test_refuses_to_reuse_the_previous_key(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        agent, _ = make_agent(settings.merged_with(api_key="sk-openai"), [])
        with pytest.raises(ConfigurationError, match="GROQ_API_KEY"):
            agent.set_provider("groq")
        assert agent.settings.provider == settings.provider
        assert agent.settings.api_key == "sk-openai"

    def test_unknown_provider_is_rejected(self, settings: Settings) -> None:
        agent, _ = make_agent(settings, [])
        with pytest.raises(ConfigurationError, match="Unknown provider"):
            agent.set_provider("skynet")


def test_shell_tool_absent_unless_enabled(settings: Settings) -> None:
    agent, _ = make_agent(settings, [])
    assert "run_command" not in agent.tools

    enabled, _ = make_agent(settings.merged_with(allow_shell=True), [])
    assert "run_command" in enabled.tools


class TestCheckpointIntegration:
    """The agent snapshots files before its tools change them."""

    def test_a_write_is_reversible(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
        target = tmp_path / "notes.md"
        target.write_text("original")
        settings = Settings(api_key="k", workspace=tmp_path)
        agent = Agent(
            settings,
            provider=FakeProvider(
                [
                    AssistantMessage(
                        tool_calls=[
                            ToolCall(
                                id="1",
                                name="write_file",
                                arguments={"path": "notes.md", "content": "replaced"},
                            )
                        ]
                    ),
                    AssistantMessage(content="done"),
                ]
            ),
            checkpoints=True,
        )

        result = agent.run("rewrite notes.md")

        assert target.read_text() == "replaced"
        assert len(result.checkpoints) == 1

        agent.checkpoints.restore(agent.checkpoints.latest())
        assert target.read_text() == "original"

    def test_a_created_file_is_deleted_on_undo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
        settings = Settings(api_key="k", workspace=tmp_path)
        agent = Agent(
            settings,
            provider=FakeProvider(
                [
                    AssistantMessage(
                        tool_calls=[
                            ToolCall(
                                id="1",
                                name="write_file",
                                arguments={"path": "new.md", "content": "hello"},
                            )
                        ]
                    ),
                    AssistantMessage(content="done"),
                ]
            ),
            checkpoints=True,
        )

        agent.run("make a file")
        assert (tmp_path / "new.md").exists()

        agent.checkpoints.restore(agent.checkpoints.latest())
        assert not (tmp_path / "new.md").exists()

    def test_no_store_when_disabled(self, tmp_path: Path) -> None:
        agent = Agent(
            Settings(api_key="k", workspace=tmp_path),
            provider=FakeProvider([AssistantMessage(content="ok")]),
            checkpoints=False,
        )

        assert agent.checkpoints is None

    def test_a_read_only_run_records_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
        agent = Agent(
            Settings(api_key="k", workspace=tmp_path),
            provider=FakeProvider([AssistantMessage(content="just answering")]),
            checkpoints=True,
        )

        assert agent.run("hello").checkpoints == []
