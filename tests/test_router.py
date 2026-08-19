"""Automatic model selection."""

from __future__ import annotations

import pytest

from conftest import FakeProvider
from jaigent.agent import Agent
from jaigent.config import DEFAULT_MODELS, KNOWN_PROVIDERS, Settings
from jaigent.llm.base import AssistantMessage
from jaigent.models import CATALOGUE
from jaigent.router import (
    PREFERENCES,
    Difficulty,
    choose_model,
    classify,
    explain,
    models_for,
    score_prompt,
)


class TestClassification:
    @pytest.mark.parametrize(
        "prompt",
        ["hi", "hello there", "thanks", "what is python?", "list the files", "define recursion"],
    )
    def test_simple_prompts(self, prompt: str) -> None:
        assert classify(prompt)[0] is Difficulty.SIMPLE

    @pytest.mark.parametrize(
        "prompt",
        [
            "refactor this package and write the tests",
            "why does this deadlock under load? debug it step by step",
            "design an architecture for the billing service",
            "do a security audit of the auth module",
            "optimise this algorithm and explain the complexity trade-offs",
        ],
    )
    def test_complex_prompts(self, prompt: str) -> None:
        assert classify(prompt)[0] is Difficulty.COMPLEX

    def test_empty_prompt_is_simple(self) -> None:
        assert classify("")[0] is Difficulty.SIMPLE

    def test_long_prompts_score_higher(self) -> None:
        short = score_prompt("summarise this")[0]
        long = score_prompt(" ".join(["summarise this document carefully"] * 30))[0]
        assert long > short

    def test_code_blocks_add_weight(self) -> None:
        plain = score_prompt("fix this")[0]
        fenced = score_prompt("fix this\n```python\nx = 1\n```")[0]
        assert fenced > plain

    def test_several_questions_add_weight(self) -> None:
        one = score_prompt("what is this?")[0]
        many = score_prompt("what is this? how does it work? why?")[0]
        assert many > one

    def test_reasons_are_human_readable(self) -> None:
        _, reasons = score_prompt("refactor the concurrency code")
        assert "refactoring" in reasons
        assert "concurrency" in reasons
        # No raw regex should leak into a user-facing explanation.
        assert not any("\\b" in reason for reason in reasons)

    def test_score_never_negative(self) -> None:
        assert score_prompt("hi thanks ok")[0] >= 0


class TestModelChoice:
    def test_complex_gets_a_stronger_model_than_simple(self) -> None:
        simple = choose_model("hi", "openai", fallback="x").model
        complex_ = choose_model("refactor everything and write tests", "openai", fallback="x").model
        assert simple != complex_

    @pytest.mark.parametrize("provider", sorted(PREFERENCES))
    def test_every_provider_routes(self, provider: str) -> None:
        routing = choose_model("summarise this", provider, fallback="fallback-model")
        assert routing.model
        assert routing.model != "fallback-model"

    @pytest.mark.parametrize("provider", sorted(PREFERENCES))
    def test_preferred_ids_exist_in_the_catalogue(self, provider: str) -> None:
        known = {m.id for m in CATALOGUE}
        for tier, candidates in PREFERENCES[provider].items():
            assert any(c in known for c in candidates), (
                f"{provider}/{tier.value} has no known model"
            )

    def test_unknown_provider_uses_the_fallback(self) -> None:
        routing = choose_model("anything", "not-a-provider", fallback="safe-default")
        assert routing.model == "safe-default"
        assert "no routing table" in routing.reason

    def test_falls_back_across_tiers_when_needed(self) -> None:
        # Only one model is available, and it is not in the simple tier.
        routing = choose_model("hi", "openai", fallback="x", available={"gpt-4o"})
        assert routing.model == "gpt-4o"

    def test_routing_summary_is_readable(self) -> None:
        summary = choose_model("hi", "openai", fallback="x").summary()
        assert "auto ->" in summary
        assert "simple" in summary

    def test_explain_mentions_model_and_difficulty(self) -> None:
        text = explain("refactor this and write tests", "anthropic", fallback="x")
        assert "complex" in text
        assert "claude" in text

    def test_models_for_lists_routable_entries(self) -> None:
        entries = models_for("openai")
        assert entries
        assert all(m.provider == "openai" for m in entries)

    def test_gemini_deepseek_and_grok_are_routable(self) -> None:
        for provider in ("gemini", "deepseek", "xai"):
            assert provider in PREFERENCES
            assert choose_model("hi", provider, fallback="x").model != "x"


class TestAgentIntegration:
    def _agent(self, provider: str, model: str, **kwargs):  # noqa: ANN202
        settings = Settings(provider=provider, model=model, api_key="k", workspace="/tmp")
        return Agent(settings, provider=FakeProvider([AssistantMessage(content="done")]), **kwargs)

    def test_auto_picks_a_concrete_model(self) -> None:
        agent = self._agent("openai", "auto")
        result = agent.run("refactor the package and write all the tests")

        assert result.routing is not None
        assert result.routing.model != "auto"
        assert agent.settings.model == result.routing.model

    def test_explicit_model_is_left_alone(self) -> None:
        agent = self._agent("openai", "gpt-4o")
        result = agent.run("hi")

        assert result.routing is None
        assert agent.settings.model == "gpt-4o"

    def test_simple_and_complex_diverge(self) -> None:
        simple = self._agent("openai", "auto").run("hi").routing
        hard = (
            self._agent("openai", "auto")
            .run("refactor this, fix the race condition and write tests")
            .routing
        )

        assert simple is not None and hard is not None
        assert simple.model != hard.model

    def test_observer_is_notified(self) -> None:
        seen: list = []
        agent = self._agent("openai", "auto", on_route=seen.append)
        agent.run("design a distributed architecture")

        assert len(seen) == 1
        assert seen[0].difficulty is Difficulty.COMPLEX

    def test_injected_provider_is_not_replaced(self) -> None:
        # Rebuilding would drop the fake and try to reach the network.
        agent = self._agent("openai", "auto")
        original = agent.provider
        agent.run("hi")

        assert agent.provider is original
        assert original.model == agent.settings.model


def test_every_known_provider_has_a_default_model() -> None:
    for provider in KNOWN_PROVIDERS:
        assert DEFAULT_MODELS.get(provider)
