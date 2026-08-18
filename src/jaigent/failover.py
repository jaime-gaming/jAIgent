"""Provider failover: keep working when a provider does not.

Rate limits, outages and quota exhaustion are routine, and an agent that dies
mid-task on a 429 wastes everything it had already done. This module wraps a
provider so that a *retryable* failure is retried with backoff, and a persistent
one falls through to the next provider you have a key for.

The fallback chain is built from whatever is actually configured — no key, no
entry — so it costs nothing to leave enabled. Non-retryable failures (a bad
request, an unknown model) are raised immediately, because retrying them would
just burn time.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jaigent.config import API_KEY_ENV_VARS, KNOWN_PROVIDERS, Settings
from jaigent.errors import ProviderError
from jaigent.llm.base import AssistantMessage, LLMProvider, TextStream
from jaigent.tools import ToolRegistry

#: HTTP statuses worth retrying, as they appear in ProviderError messages.
RETRYABLE_STATUSES = (408, 429, 500, 502, 503, 504, 529)

#: Substrings that indicate a transient network problem rather than a bad request.
RETRYABLE_PHRASES = (
    "could not reach",
    "timed out",
    "timeout",
    "connection",
    "temporarily unavailable",
    "overloaded",
    "rate limit",
)

_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")

#: Order preferred when building a chain automatically: cheap and fast first.
FALLBACK_ORDER = (
    "openai",
    "anthropic",
    "gemini",
    "deepseek",
    "groq",
    "xai",
    "mistral",
    "openrouter",
    "together",
    "omniroute",
    "ollama",
)


def is_retryable(error: Exception) -> bool:
    """Whether an error is worth trying again."""
    text = str(error).lower()
    match = _STATUS_RE.search(str(error))
    if match:
        return int(match.group(1)) in RETRYABLE_STATUSES
    return any(phrase in text for phrase in RETRYABLE_PHRASES)


def available_providers(settings: Settings, *, env: dict[str, str] | None = None) -> list[str]:
    """Providers that could actually serve a request right now.

    A provider qualifies if it has a key in the environment, or if it runs
    locally and needs none. The configured provider is always first.
    """
    import os

    environment = env if env is not None else dict(os.environ)
    from jaigent.config import LOCAL_PROVIDERS

    usable: list[str] = []
    for name in FALLBACK_ORDER:
        if name not in KNOWN_PROVIDERS:
            continue
        if name in LOCAL_PROVIDERS or environment.get(API_KEY_ENV_VARS.get(name, "")):
            usable.append(name)

    if settings.provider in usable:
        usable.remove(settings.provider)
    return [settings.provider, *usable]


@dataclass(slots=True)
class Attempt:
    """One try against one provider, for the run log."""

    provider: str
    model: str
    error: str = ""
    retried: bool = False


@dataclass(slots=True)
class FailoverPolicy:
    """How hard to try before giving up."""

    #: Attempts per provider before moving on. 1 disables retrying.
    attempts: int = 3
    #: First backoff delay in seconds; doubles each retry, with jitter.
    backoff: float = 1.0
    #: Cap on any single sleep.
    max_backoff: float = 20.0
    #: Fall through to other configured providers when one is exhausted.
    chain: bool = True

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with jitter, so retries do not synchronise."""
        base = min(self.backoff * (2**attempt), self.max_backoff)
        return base * (0.5 + random.random() / 2)


class FailoverProvider(LLMProvider):
    """Wraps providers so a failure moves to the next instead of ending the run.

    Args:
        primary: The provider to use first.
        settings: Used to build fallbacks and to read the model names.
        policy: Retry and chaining behaviour.
        build: Factory for a provider from settings; injected in tests.
        sleep: Sleep function; injected in tests so backoff is instant.
        on_failover: Called with each :class:`Attempt` that failed.
    """

    name = "failover"

    def __init__(
        self,
        primary: LLMProvider,
        settings: Settings,
        *,
        policy: FailoverPolicy | None = None,
        build: Callable[[Settings], LLMProvider] | None = None,
        sleep: Callable[[float], None] | None = None,
        on_failover: Callable[[Attempt], None] | None = None,
        providers: Sequence[str] | None = None,
    ) -> None:
        super().__init__(
            api_key=primary.api_key,
            model=primary.model,
            base_url=primary.base_url,
            timeout=primary.timeout,
        )
        self.primary = primary
        self.settings = settings
        self.policy = policy or FailoverPolicy()
        self.on_failover = on_failover
        self.attempts: list[Attempt] = []

        if build is None:
            from jaigent.llm import get_provider

            build = get_provider
        self._build = build
        self._sleep = sleep or time.sleep
        self._chain = list(providers) if providers is not None else None

    @property
    def supports_streaming(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.primary, "supports_streaming", False))

    # ------------------------------------------------------------------
    def _fallback_chain(self) -> list[str]:
        if self._chain is not None:
            return self._chain
        if not self.policy.chain:
            return [self.settings.provider]
        return available_providers(self.settings)

    def _providers_to_try(self) -> list[tuple[str, LLMProvider | None]]:
        """The primary first, then each configured fallback, built lazily."""
        chain = self._fallback_chain()
        result: list[tuple[str, LLMProvider | None]] = [(self.settings.provider, self.primary)]
        result.extend((name, None) for name in chain if name != self.settings.provider)
        return result

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: ToolRegistry | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        on_text: TextStream | None = None,
    ) -> AssistantMessage:
        self.attempts = []
        last: Exception | None = None

        for provider_name, prebuilt in self._providers_to_try():
            provider = prebuilt
            if provider is None:
                try:
                    provider = self._build(
                        self.settings.merged_with(provider=provider_name, model=None, base_url=None)
                    )
                except Exception as exc:  # noqa: BLE001 - a bad fallback is skippable
                    self._record(provider_name, "", str(exc))
                    last = exc
                    continue

            for attempt in range(self.policy.attempts):
                try:
                    reply = provider.complete(
                        messages,
                        tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        on_text=on_text,
                    )
                except Exception as exc:  # noqa: BLE001 - classified below
                    last = exc
                    retryable = is_retryable(exc)
                    self._record(provider_name, provider.model, str(exc), retried=retryable)
                    if not retryable:
                        break  # a bad request will fail identically on a retry
                    if attempt + 1 < self.policy.attempts:
                        self._sleep(self.policy.delay_for(attempt))
                    continue
                else:
                    # Adopt whichever provider actually worked.
                    self.primary = provider
                    self.model = provider.model
                    return reply

        if last is not None:
            raise ProviderError(self._explain(last))
        raise ProviderError("No provider was available to handle the request.")

    def _record(self, provider: str, model: str, error: str, *, retried: bool = False) -> None:
        attempt = Attempt(provider=provider, model=model, error=error[:300], retried=retried)
        self.attempts.append(attempt)
        if self.on_failover is not None:
            self.on_failover(attempt)

    def _explain(self, error: Exception) -> str:
        tried = ", ".join(dict.fromkeys(a.provider for a in self.attempts)) or "none"
        return (
            f"Every provider failed. Tried: {tried}.\n"
            f"Last error: {error}\n"
            "Check your keys and network, or set another provider's key so jaigent "
            "can fall back to it."
        )

    # -- delegation ----------------------------------------------------
    def format_assistant_message(self, message: AssistantMessage) -> dict[str, Any]:
        return self.primary.format_assistant_message(message)

    def format_tool_result(self, call: Any, output: str) -> dict[str, Any]:
        return self.primary.format_tool_result(call, output)


@dataclass(slots=True)
class HealthReport:
    """The outcome of probing one provider."""

    provider: str
    ok: bool
    detail: str = ""
    latency: float = 0.0
    models: list[str] = field(default_factory=list)
