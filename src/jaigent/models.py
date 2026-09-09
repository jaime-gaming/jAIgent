"""The model catalogue.

A curated list of models that are known to support tool calling, which is the
one capability jaigent cannot work without. Use ``jaigent models`` to browse it.

The catalogue is a convenience, not a restriction: any model id can be passed to
``--model``, and gateways expose thousands more than are listed here.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

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
)


@dataclass(slots=True, frozen=True)
class ModelInfo:
    """One entry in the catalogue."""

    id: str
    provider: str
    label: str
    context: str = ""
    note: str = ""
    #: True when the model can be used at no cost (local, a free gateway
    #: tier, or a provider that publishes a no-charge quota).
    free: bool = False


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
    ModelInfo(
        "gemini-2.5-flash", "gemini", "Gemini 2.5 Flash", "1M", "fast; the default", free=True
    ),
    ModelInfo("gemini-2.0-flash", "gemini", "Gemini 2.0 Flash", "1M", "", free=True),
    ModelInfo(
        "gemini-2.0-flash-lite", "gemini", "Gemini 2.0 Flash Lite", "1M", "cheapest", free=True
    ),
    # -------------------------------------------------------------- DeepSeek
    ModelInfo("deepseek-chat", "deepseek", "DeepSeek V3", "64K", "very cheap"),
    ModelInfo("deepseek-reasoner", "deepseek", "DeepSeek R1", "64K", "reasoning"),
    # ------------------------------------------------------------------ Groq
    ModelInfo("llama-3.3-70b-versatile", "groq", "Llama 3.3 70B", "128K", "very fast", free=True),
    ModelInfo("llama-3.1-8b-instant", "groq", "Llama 3.1 8B", "128K", "fastest", free=True),
    ModelInfo("qwen-2.5-32b", "groq", "Qwen 2.5 32B", "128K", "", free=True),
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
    ModelInfo(
        "meta-llama/llama-3.3-70b-instruct:free",
        "openrouter",
        "Llama 3.3 70B",
        "128K",
        "free tier",
        free=True,
    ),
    ModelInfo(
        "google/gemma-3-27b-it:free",
        "openrouter",
        "Gemma 3 27B",
        "128K",
        "free tier",
        free=True,
    ),
    ModelInfo(
        "qwen/qwen3-8b:free",
        "openrouter",
        "Qwen 3 8B",
        "32K",
        "free tier",
        free=True,
    ),
    # --------------------------------------------------------------- Together
    ModelInfo(
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "together",
        "Llama 3.3 70B Turbo",
        "128K",
        "",
    ),
    ModelInfo(
        "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "together",
        "Llama 3.1 8B Turbo",
        "128K",
        "cheap",
    ),
    # ---------------------------------------------------------------- Ollama
    ModelInfo("qwen2.5:14b", "ollama", "Qwen 2.5 14B", "32K", "local, free", free=True),
    ModelInfo("llama3.1:8b", "ollama", "Llama 3.1 8B", "128K", "local, free", free=True),
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


def free_models(*, provider: str | None = None) -> list[ModelInfo]:
    """Catalogue entries that can be used at no cost."""
    wanted = provider.strip().lower() if provider else None
    return [
        model for model in CATALOGUE if model.free and (wanted is None or model.provider == wanted)
    ]


def cache_path():
    """Where gathered models are remembered between runs."""
    from jaigent.paths import user_home

    return user_home() / "models-cache.json"


def load_cache() -> list[ModelInfo]:
    """Previously gathered models, or an empty list."""
    path = cache_path()
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    found: list[ModelInfo] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("id") or not item.get("provider"):
            continue
        found.append(
            ModelInfo(
                id=str(item["id"]),
                provider=str(item["provider"]),
                label=str(item.get("label") or item["id"]),
                context=str(item.get("context") or ""),
                note=str(item.get("note") or "gathered"),
                free=bool(item.get("free", False)),
            )
        )
    return found


def save_cache(entries: list[ModelInfo]) -> None:
    """Remember gathered models so ``jaigent models`` works offline."""
    payload = [
        {
            "id": m.id,
            "provider": m.provider,
            "label": m.label,
            "context": m.context,
            "note": m.note,
            "free": m.free,
        }
        for m in entries
    ]
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        return


def _ids_from_openai_shape(data: Any) -> list[str]:
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
            continue
        if not isinstance(item, dict):
            continue
        raw = item.get("id") or item.get("name") or ""
        name = str(raw).strip()
        if name.startswith("models/"):
            name = name[len("models/") :]
        if name:
            ids.append(name)
    return ids


def gather_provider(
    provider: str,
    *,
    api_key: str | None,
    base_url: str | None,
    timeout: float = 10.0,
) -> list[ModelInfo]:
    """Ask one provider which models it currently serves.

    Failures return an empty list: gathering must never break ``jaigent models``.
    """
    import httpx

    from jaigent.config import DEFAULT_BASE_URLS

    name = provider.strip().lower()
    root = (base_url or DEFAULT_BASE_URLS.get(name) or "").rstrip("/")
    if not root:
        return []

    headers: dict[str, str] = {"User-Agent": "jAIgent/models"}
    params: dict[str, str] = {}
    url = f"{root}/models"

    if name == "anthropic":
        if not api_key:
            return []
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif name == "gemini":
        if not api_key:
            return []
        params["key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = httpx.get(
            url, headers=headers, params=params, timeout=timeout, follow_redirects=True
        )
        response.raise_for_status()
        data = response.json()
    except Exception:  # noqa: BLE001 - gathering is best-effort
        return []

    ids = _ids_from_openai_shape(data)
    return [
        ModelInfo(id=model_id, provider=name, label=model_id, note="gathered") for model_id in ids
    ]


def gather_available(*, timeout: float = 10.0) -> list[ModelInfo]:
    """Gather from every provider that currently has a usable key.

    In parallel: sequential gathering made ``models --refresh`` wait out
    every provider's full timeout one after another. ``map`` keeps provider
    order, so the result stays deterministic.
    """
    from jaigent.config import DEFAULT_BASE_URLS, KNOWN_PROVIDERS, key_for_provider

    def one(provider: str) -> list[ModelInfo]:
        try:
            return gather_provider(
                provider,
                api_key=key_for_provider(provider),
                base_url=DEFAULT_BASE_URLS.get(provider),
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001 - gathering is best-effort
            return []

    gathered: list[ModelInfo] = []
    seen: set[tuple[str, str]] = set()
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="jaigent-models") as pool:
        for entries in pool.map(one, KNOWN_PROVIDERS):
            for model in entries:
                stamp = (model.provider, model.id)
                if stamp in seen:
                    continue
                seen.add(stamp)
                gathered.append(model)
    save_cache(gathered)
    return gathered


def combined(
    *,
    live: list[ModelInfo] | None = None,
    include_cache: bool = True,
) -> list[ModelInfo]:
    """Catalogue plus gathered models, catalogue winning on id collisions."""
    by_id: dict[str, ModelInfo] = {model.id: model for model in CATALOGUE}
    extra = list(live or [])
    if include_cache:
        extra = [*load_cache(), *extra]
    for model in extra:
        by_id.setdefault(model.id, model)
    return list(by_id.values())
