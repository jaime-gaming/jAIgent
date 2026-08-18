"""The model catalogue.

A curated list of models that are known to support tool calling, which is the
one capability jaigent cannot work without. Use ``jaigent models`` to browse it.

The catalogue is a convenience, not a restriction: any model id can be passed to
``--model``, and gateways expose thousands more than are listed here.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Providers that speak the OpenAI ``/chat/completions`` shape.
OPENAI_COMPATIBLE = (
    "openai",
    "openrouter",
    "groq",
    "together",
    "deepseek",
    "mistral",
    "xai",
    "ollama",
    "omniroute",
)


@dataclass(slots=True, frozen=True)
class ModelInfo:
    """One entry in the catalogue."""

    id: str
    provider: str
    label: str
    context: str = ""
    note: str = ""


#: Curated models, grouped by the provider they are reached through.
#: Prices live separately in :mod:`jaigent.pricing`.
CATALOGUE: tuple[ModelInfo, ...] = (
    # ---------------------------------------------------------------- OpenAI
    ModelInfo("gpt-4o-mini", "openai", "GPT-4o mini", "128K", "fast and cheap; the default"),
    ModelInfo("gpt-4o", "openai", "GPT-4o", "128K", "strong general-purpose"),
    ModelInfo("gpt-4.1", "openai", "GPT-4.1", "1M", "long context"),
    ModelInfo("gpt-4.1-mini", "openai", "GPT-4.1 mini", "1M", "long context, cheaper"),
    ModelInfo("gpt-4.1-nano", "openai", "GPT-4.1 nano", "1M", "cheapest OpenAI option"),
    ModelInfo("o3-mini", "openai", "o3-mini", "200K", "reasoning"),
    ModelInfo("o1", "openai", "o1", "200K", "deep reasoning, slow and pricey"),
    # ------------------------------------------------------------- Anthropic
    ModelInfo("claude-sonnet-4-20250514", "anthropic", "Claude Sonnet 4", "200K", "recommended"),
    ModelInfo("claude-opus-4-20250514", "anthropic", "Claude Opus 4", "200K", "most capable"),
    ModelInfo("claude-3-7-sonnet-latest", "anthropic", "Claude 3.7 Sonnet", "200K", ""),
    ModelInfo("claude-3-5-sonnet-latest", "anthropic", "Claude 3.5 Sonnet", "200K", ""),
    ModelInfo("claude-3-5-haiku-latest", "anthropic", "Claude 3.5 Haiku", "200K", "fast"),
    # ---------------------------------------------------------------- Gemini
    ModelInfo("gemini-2.5-pro", "gemini", "Gemini 2.5 Pro", "1M", "most capable"),
    ModelInfo("gemini-2.5-flash", "gemini", "Gemini 2.5 Flash", "1M", "fast; the default"),
    ModelInfo("gemini-2.0-flash", "gemini", "Gemini 2.0 Flash", "1M", ""),
    ModelInfo("gemini-2.0-flash-lite", "gemini", "Gemini 2.0 Flash Lite", "1M", "cheapest"),
    # -------------------------------------------------------------- DeepSeek
    ModelInfo("deepseek-chat", "deepseek", "DeepSeek V3", "64K", "very cheap"),
    ModelInfo("deepseek-reasoner", "deepseek", "DeepSeek R1", "64K", "reasoning"),
    # ------------------------------------------------------------------ Groq
    ModelInfo("llama-3.3-70b-versatile", "groq", "Llama 3.3 70B", "128K", "very fast"),
    ModelInfo("llama-3.1-8b-instant", "groq", "Llama 3.1 8B", "128K", "fastest"),
    ModelInfo("qwen-2.5-32b", "groq", "Qwen 2.5 32B", "128K", ""),
    # --------------------------------------------------------------- Mistral
    ModelInfo("mistral-large-latest", "mistral", "Mistral Large", "128K", ""),
    ModelInfo("mistral-small-latest", "mistral", "Mistral Small", "128K", "cheap"),
    # ------------------------------------------------------------------- xAI
    ModelInfo("grok-4", "xai", "Grok 4", "256K", "most capable"),
    ModelInfo("grok-3", "xai", "Grok 3", "131K", ""),
    ModelInfo("grok-3-mini", "xai", "Grok 3 mini", "131K", "cheap"),
    ModelInfo("grok-2-latest", "xai", "Grok 2", "131K", ""),
    # ------------------------------------------------------------ OpenRouter
    ModelInfo("anthropic/claude-sonnet-4", "openrouter", "Claude Sonnet 4", "200K", "via gateway"),
    ModelInfo("openai/gpt-4o-mini", "openrouter", "GPT-4o mini", "128K", "via gateway"),
    ModelInfo("google/gemini-2.5-pro", "openrouter", "Gemini 2.5 Pro", "1M", "via gateway"),
    ModelInfo("deepseek/deepseek-chat", "openrouter", "DeepSeek V3", "64K", "via gateway"),
    # ---------------------------------------------------------------- Ollama
    ModelInfo("qwen2.5:14b", "ollama", "Qwen 2.5 14B", "32K", "local, free"),
    ModelInfo("llama3.1:8b", "ollama", "Llama 3.1 8B", "128K", "local, free"),
    # ------------------------------------------------------------- OmniRoute
    # Model ids are provider-prefixed; see https://github.com/diegosouzapw/OmniRoute
    ModelInfo("if/kimi-k2-thinking", "omniroute", "Kimi K2 Thinking", "256K", "free tier"),
    ModelInfo("cc/claude-sonnet-4-20250514", "omniroute", "Claude Sonnet 4", "200K", ""),
    ModelInfo("gg/gemini-2.5-pro", "omniroute", "Gemini 2.5 Pro", "1M", ""),
    ModelInfo("glm/glm-4.7", "omniroute", "GLM 4.7", "128K", "cheap"),
    ModelInfo("auto", "omniroute", "Auto", "—", "let OmniRoute choose and fall back"),
)


def for_provider(provider: str) -> list[ModelInfo]:
    """Catalogue entries reached through ``provider``."""
    name = provider.strip().lower()
    return [model for model in CATALOGUE if model.provider == name]


def providers() -> list[str]:
    """Every provider mentioned in the catalogue, in catalogue order."""
    seen: list[str] = []
    for model in CATALOGUE:
        if model.provider not in seen:
            seen.append(model.provider)
    return seen


def find(model_id: str) -> ModelInfo | None:
    """Look up one model by its exact id."""
    for model in CATALOGUE:
        if model.id == model_id:
            return model
    return None


def search(term: str) -> list[ModelInfo]:
    """Case-insensitive substring search across id, label and provider."""
    needle = term.strip().lower()
    if not needle:
        return list(CATALOGUE)
    return [
        model
        for model in CATALOGUE
        if needle in model.id.lower()
        or needle in model.label.lower()
        or needle in model.provider.lower()
    ]
