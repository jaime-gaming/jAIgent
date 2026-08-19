"""LLM provider registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jaigent.errors import ConfigurationError
from jaigent.llm.anthropic import AnthropicProvider
from jaigent.llm.base import AssistantMessage, LLMProvider, ToolCall
from jaigent.llm.gemini import GeminiProvider
from jaigent.llm.openai import OpenAIProvider

if TYPE_CHECKING:  # pragma: no cover
    from jaigent.config import Settings

__all__ = [
    "PROVIDERS",
    "AnthropicProvider",
    "AssistantMessage",
    "GeminiProvider",
    "LLMProvider",
    "OpenAIProvider",
    "ToolCall",
    "get_provider",
]

#: Every provider except Anthropic speaks the OpenAI chat-completions shape,
#: so they share one adapter and differ only in base URL and default model.
PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "openrouter": OpenAIProvider,
    "groq": OpenAIProvider,
    "deepseek": OpenAIProvider,
    "mistral": OpenAIProvider,
    "xai": OpenAIProvider,
    "together": OpenAIProvider,
    "ollama": OpenAIProvider,
}


def get_provider(settings: Settings) -> LLMProvider:
    """Instantiate the provider described by ``settings``.

    Raises:
        ConfigurationError: for an unknown provider or a missing API key.
    """
    try:
        provider_cls = PROVIDERS[settings.provider]
    except KeyError as exc:
        raise ConfigurationError(
            f"Unknown provider {settings.provider!r}. Available: {', '.join(sorted(PROVIDERS))}"
        ) from exc

    from jaigent.config import DEFAULT_BASE_URLS

    return provider_cls(
        api_key=settings.require_api_key(),
        model=settings.model,
        base_url=settings.base_url or DEFAULT_BASE_URLS[settings.provider],
        timeout=settings.timeout,
    )
