"""The model catalogue and the expanded provider list, OmniRoute included."""

from __future__ import annotations

import pytest

from jaigent import models
from jaigent.config import (
    API_KEY_ENV_VARS,
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    KNOWN_PROVIDERS,
    LOCAL_PROVIDERS,
    OMNIROUTE_DEFAULT_URL,
    Settings,
)
from jaigent.llm import PROVIDERS, get_provider
from jaigent.llm.anthropic import AnthropicProvider
from jaigent.llm.openai import OpenAIProvider


class TestCatalogue:
    def test_is_not_empty(self) -> None:
        assert len(models.CATALOGUE) > 20

    def test_ids_are_unique(self) -> None:
        ids = [m.id for m in models.CATALOGUE]
        assert len(ids) == len(set(ids))

    def test_every_entry_names_a_known_provider(self) -> None:
        for model in models.CATALOGUE:
            assert model.provider in KNOWN_PROVIDERS, model.id

    def test_every_entry_has_a_label(self) -> None:
        for model in models.CATALOGUE:
            assert model.label.strip(), model.id

    def test_lookup_by_id(self) -> None:
        assert models.find("gpt-4o-mini") is not None
        assert models.find("not-a-model") is None

    def test_for_provider(self) -> None:
        entries = models.for_provider("anthropic")
        assert entries
        assert all(m.provider == "anthropic" for m in entries)

    def test_providers_listed_once(self) -> None:
        found = models.providers()
        assert len(found) == len(set(found))

    def test_search_matches_id_label_and_provider(self) -> None:
        assert models.search("gpt-4o")
        assert models.search("Sonnet")
        assert models.search("groq")

    def test_search_is_case_insensitive(self) -> None:
        assert models.search("CLAUDE") == models.search("claude")

    def test_empty_search_returns_everything(self) -> None:
        assert len(models.search("  ")) == len(models.CATALOGUE)

    def test_free_models_are_marked(self) -> None:
        found = models.free_models()
        assert found
        assert all(model.free for model in found)

    def test_together_is_in_the_catalogue(self) -> None:
        assert models.for_provider("together")


class TestProviderRegistry:
    def test_every_known_provider_is_registered(self) -> None:
        for provider in KNOWN_PROVIDERS:
            assert provider in PROVIDERS, provider

    def test_every_provider_has_defaults(self) -> None:
        for provider in KNOWN_PROVIDERS:
            assert provider in DEFAULT_MODELS
            assert provider in DEFAULT_BASE_URLS
            assert provider in API_KEY_ENV_VARS

    def test_anthropic_uses_its_own_adapter(self) -> None:
        assert PROVIDERS["anthropic"] is AnthropicProvider

    @pytest.mark.parametrize(
        "provider", [p for p in KNOWN_PROVIDERS if p not in {"anthropic", "gemini"}]
    )
    def test_the_rest_are_openai_compatible(self, provider: str) -> None:
        # DeepSeek and Grok ship OpenAI-compatible endpoints, so they reuse the adapter.
        assert PROVIDERS[provider] is OpenAIProvider

    def test_gemini_uses_its_own_adapter(self) -> None:
        from jaigent.llm.gemini import GeminiProvider

        assert PROVIDERS["gemini"] is GeminiProvider

    @pytest.mark.parametrize("provider", KNOWN_PROVIDERS)
    def test_each_provider_builds(self, provider: str) -> None:
        built = get_provider(Settings(provider=provider, api_key="k", workspace="/tmp"))

        assert built.model == DEFAULT_MODELS[provider]
        assert built.base_url == DEFAULT_BASE_URLS[provider].rstrip("/")

    def test_settings_adopt_provider_defaults(self) -> None:
        # Regression: Settings(provider=...) used to keep OpenAI's model.
        settings = Settings(provider="groq", api_key="k", workspace="/tmp")

        assert settings.model == DEFAULT_MODELS["groq"]
        assert settings.base_url == DEFAULT_BASE_URLS["groq"]

    def test_explicit_model_wins(self) -> None:
        settings = Settings(provider="groq", model="custom", api_key="k", workspace="/tmp")
        assert settings.model == "custom"


class TestOmniRoute:
    def test_is_a_known_provider(self) -> None:
        assert "omniroute" in KNOWN_PROVIDERS

    def test_defaults_to_the_local_gateway(self) -> None:
        settings = Settings(provider="omniroute", workspace="/tmp")

        assert settings.base_url == OMNIROUTE_DEFAULT_URL
        assert "20128" in settings.base_url

    def test_default_model_is_auto(self) -> None:
        assert DEFAULT_MODELS["omniroute"] == "auto"

    def test_needs_no_api_key(self) -> None:
        # A local gateway accepts any token; the user should not have to invent one.
        assert Settings(provider="omniroute", workspace="/tmp").require_api_key()

    def test_counts_as_local(self) -> None:
        assert "omniroute" in LOCAL_PROVIDERS

    def test_uses_the_openai_adapter(self) -> None:
        built = get_provider(Settings(provider="omniroute", workspace="/tmp"))
        assert isinstance(built, OpenAIProvider)

    def test_catalogue_has_prefixed_model_ids(self) -> None:
        entries = models.for_provider("omniroute")

        assert entries
        # OmniRoute addresses models as <provider>/<model>, plus the "auto" router.
        assert any("/" in m.id for m in entries)
        assert any(m.id == "auto" for m in entries)

    def test_env_var_overrides_the_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JAIGENT_BASE_URL", raising=False)
        monkeypatch.setenv("JAIGENT_PROVIDER", "omniroute")
        monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://gateway:20128/v1")

        assert Settings.from_env(dotenv=None).base_url == "http://gateway:20128/v1"

    def test_generic_base_url_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_PROVIDER", "omniroute")
        monkeypatch.setenv("JAIGENT_BASE_URL", "http://explicit/v1")
        monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://ignored/v1")

        assert Settings.from_env(dotenv=None).base_url == "http://explicit/v1"

    def test_from_env_supplies_a_placeholder_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("JAIGENT_API_KEY", "OPENAI_API_KEY", "OMNIROUTE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("JAIGENT_PROVIDER", "omniroute")

        assert Settings.from_env(dotenv=None).api_key


class TestOllama:
    def test_needs_no_key(self) -> None:
        assert Settings(provider="ollama", workspace="/tmp").require_api_key()

    def test_points_at_localhost(self) -> None:
        assert "11434" in DEFAULT_BASE_URLS["ollama"]


def test_remote_providers_still_demand_a_key() -> None:
    from jaigent.errors import ConfigurationError

    for provider in ("openai", "anthropic", "groq", "openrouter"):
        settings = Settings(provider=provider, workspace="/tmp")
        with pytest.raises(ConfigurationError, match="No API key"):
            settings.require_api_key()
