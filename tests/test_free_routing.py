"""--model free picks a no-cost model from a provider you can actually reach."""

from __future__ import annotations

from conftest import FakeProvider
from jaigent.agent import Agent
from jaigent.config import Settings
from jaigent.llm.base import AssistantMessage
from jaigent.models import CATALOGUE, free_models
from jaigent.router import FREE_PREFERENCES, Difficulty, choose_free_model


def test_catalogue_has_free_models() -> None:
    found = free_models()
    assert found
    assert all(model.free for model in found)
    assert any(model.provider == "ollama" for model in found)
    assert any(model.provider == "groq" for model in found)


def test_free_models_can_be_filtered_by_provider() -> None:
    groq = free_models(provider="groq")
    assert groq
    assert all(model.provider == "groq" for model in groq)


def test_prefers_local_over_hosted() -> None:
    routing = choose_free_model(
        "hi",
        usable=["openai", "ollama", "groq"],
        fallback_provider="openai",
        fallback_model="gpt-4o-mini",
    )
    assert routing.provider == "ollama"
    assert routing.model
    assert routing.difficulty is Difficulty.SIMPLE


def test_skips_providers_without_a_key() -> None:
    routing = choose_free_model(
        "summarise this",
        usable=["openai", "groq"],
        fallback_provider="openai",
        fallback_model="gpt-4o-mini",
    )
    assert routing.provider == "groq"


def test_falls_back_when_nothing_free_is_usable() -> None:
    routing = choose_free_model(
        "hi",
        usable=["openai"],
        fallback_provider="openai",
        fallback_model="gpt-4o-mini",
    )
    assert routing.model == "gpt-4o-mini"
    assert routing.provider == "openai"
    assert "no free provider" in routing.reason


def test_free_preference_ids_exist_in_the_catalogue() -> None:
    known = {model.id for model in CATALOGUE}
    for provider, table in FREE_PREFERENCES.items():
        for tier, candidates in table.items():
            assert any(candidate in known for candidate in candidates), (
                f"{provider}/{tier.value} has no known free model"
            )


def test_agent_free_mode_switches_provider(monkeypatch, settings: Settings) -> None:
    def factory(configured):  # noqa: ANN001, ANN202
        return FakeProvider([AssistantMessage(content="ok")])

    monkeypatch.setattr("jaigent.agent.get_provider", factory)
    monkeypatch.setenv("GROQ_API_KEY", "g")
    agent = Agent(settings.merged_with(model="free", provider="openai", failover=False))
    result = agent.run("hi")

    assert result.routing is not None
    assert result.routing.provider in {"ollama", "groq"}
    assert agent.settings.model != "free"
