"""Connecting a provider key, at the moment the user needs it.

``jaigent init`` is a three-step form that writes a project ``.env``. It works,
but it is the wrong doorway for the two situations that actually happen:

* **Nothing is configured yet.** ``jaigent chat`` printed a configuration
  error naming three other commands, and the first thing a new user saw was a
  wall of options instead of a prompt.
* **A key was just pasted and is wrong.** Nothing checked it, so the failure
  arrived later — in the middle of a task, as a provider 401 dressed up as a
  tool error.

This module does the smallest useful thing in each case: ask for the key in
place, check it against the provider before claiming success, and store it in
the owner-only secrets file so it works from any directory. A project ``.env``
is still what ``jaigent init`` writes, because a team may want to commit which
provider a repo uses — but a key pasted here never lands in a file that could
be committed by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from jaigent.config import (
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    KEY_URLS,
    LOCAL_PROVIDERS,
    Settings,
    key_for_provider,
)
from jaigent.errors import ConfigurationError
from jaigent.secrets import set_key

#: Providers offered first, in the order most people reach for them. The rest
#: of :data:`jaigent.config.KNOWN_PROVIDERS` follows.
_PREFERRED = ("openai", "anthropic", "gemini", "openrouter", "groq")
#: "ollama" is deliberately absent: it needs no key, so it is not an answer to
#: "paste a key", and offering it here only adds a dead end.
CHOICES = tuple(name for name in _PREFERRED if name not in LOCAL_PROVIDERS)


@dataclass(slots=True)
class Check:
    """The result of asking a provider whether a key works."""

    ok: bool
    detail: str
    #: False when we could not tell (no network, an endpoint without /models).
    #: A wrong key and an unreachable host must not share a message.
    conclusive: bool = True


def _headers(provider: str, key: str) -> dict[str, str]:
    if provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if provider == "gemini":
        return {"x-goog-api-key": key}
    return {"authorization": f"Bearer {key}"}


def verify_key(provider: str, key: str, *, timeout: float = 8.0) -> Check:
    """Ask ``provider`` whether ``key`` authenticates, without spending tokens.

    Every provider here exposes a model listing that is free to call, so the
    check costs nothing and returns fast. Anything we cannot interpret is
    reported as *inconclusive* rather than as a bad key: telling someone their
    key is wrong when the network is down is worse than saying nothing.
    """
    name = provider.strip().lower()
    if not key.strip():
        # Nothing to prove. Cheap to check here, and it keeps a stray Enter
        # from being reported as a provider verdict.
        return Check(False, "no key was given to check")
    if name in LOCAL_PROVIDERS:
        return Check(True, f"{name} runs locally; no key needed")

    root = (DEFAULT_BASE_URLS.get(name) or "").rstrip("/")
    if not root:
        return Check(False, f"unknown provider {name!r}", conclusive=False)

    url = f"{root}/models"
    params: dict[str, str] = {}
    if name == "gemini":
        # Gemini's listing lives under a versioned path and takes the key as a
        # query parameter, not a header, on this endpoint.
        url = f"{root}/v1beta/models"
        params = {"key": key}
        headers = {"content-type": "application/json"}
    else:
        headers = _headers(name, key)

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers, params=params or None)
    except httpx.HTTPError as exc:
        return Check(False, f"could not reach {name} ({type(exc).__name__})", conclusive=False)

    if response.status_code in {200, 201}:
        return Check(True, f"{name} accepted the key")
    if response.status_code in {401, 403}:
        return Check(False, f"{name} rejected the key")
    if response.status_code == 429:
        # A rate limit proves the key authenticated well enough to be counted.
        return Check(True, "key accepted (rate limited right now)", conclusive=False)
    if response.status_code == 404:
        # An OpenAI-compatible gateway without /models: nothing was proven.
        return Check(True, "endpoint has no model list; not verified", conclusive=False)
    return Check(
        False,
        f"the provider answered HTTP {response.status_code}",
        conclusive=response.status_code < 500,
    )


def store_key(provider: str, key: str) -> Path:
    """Save ``key`` for ``provider``. Returns the file it went to."""
    if not key.strip():
        raise ConfigurationError("Refusing to store an empty API key.")
    return set_key(provider, key)


def known_settings(provider: str) -> Settings:
    """``provider`` wired to its default model and endpoint, still keyless."""
    return Settings(
        provider=provider,
        model=DEFAULT_MODELS.get(provider, ""),
        base_url=DEFAULT_BASE_URLS.get(provider, "") or None,
        api_key="",
    )


def key_url(provider: str) -> str:
    """Where the user gets a key for ``provider``, or an empty string."""
    return KEY_URLS.get(provider, "")


def has_key(provider: str) -> bool:
    """Whether a key for ``provider`` is already reachable from this process."""
    return bool(key_for_provider(provider))
