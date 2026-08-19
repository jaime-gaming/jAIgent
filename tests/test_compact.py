"""History compacting and the spend cap."""

from __future__ import annotations

from conftest import FakeProvider
from jaigent.agent import Agent
from jaigent.config import Settings
from jaigent.llm.base import AssistantMessage


def test_compact_drops_old_turns(settings: Settings) -> None:
    agent = Agent(settings, provider=FakeProvider([]))
    agent.history = [
        {"role": "user", "content": f"q{i}"}
        if i % 2 == 0
        else {"role": "assistant", "content": f"a{i}"}
        for i in range(12)
    ]
    dropped = agent.compact(keep=4)
    assert dropped == 8
    assert len(agent.history) == 5  # summary + 4 kept
    assert "compacted" in agent.history[0]["content"]


def test_compact_is_a_no_op_when_short(settings: Settings) -> None:
    agent = Agent(settings, provider=FakeProvider([]))
    agent.history = [{"role": "user", "content": "hi"}]
    assert agent.compact() == 0


def test_spend_cap_stops_the_run(settings: Settings) -> None:
    provider = FakeProvider(
        [
            AssistantMessage(
                content="ok", usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
            )
        ]
    )
    agent = Agent(settings.merged_with(model="gpt-4o-mini", budget=0.01), provider=provider)
    result = agent.run("hi")
    assert result.stopped_early is True
    assert result.cost.usd is not None
    assert result.cost.usd >= 0.01
