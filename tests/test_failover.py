"""Tests for provider retry and failover."""

from __future__ import annotations

import pytest

from jaigent.config import Settings
from jaigent.errors import ProviderError
from jaigent.failover import (
    FALLBACK_ORDER,
    RETRYABLE_STATUSES,
    Attempt,
    FailoverPolicy,
    FailoverProvider,
    HealthReport,
    available_providers,
    is_retryable,
)
from jaigent.llm.base import AssistantMessage, LLMProvider


class ScriptedProvider(LLMProvider):
    """A provider that replays a script of outcomes, one per call."""

    name = "scripted"
    supports_streaming = True

    def __init__(self, *outcomes, model="test-model", label="scripted"):
        super().__init__(api_key="k", model=model, base_url="https://example.invalid")
        self.outcomes = list(outcomes)
        self.label = label
        self.calls = 0

    def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return AssistantMessage(content=outcome)

    def format_assistant_message(self, message):
        return {"role": "assistant", "content": message.content}

    def format_tool_result(self, call, output):
        return {"role": "tool", "content": output}


@pytest.fixture()
def settings():
    return Settings(provider="openai", model="gpt-4o-mini", api_key="k")


def wrap(primary, settings, **kwargs):
    """Build a FailoverProvider with instant backoff and no live network.

    The chain defaults to the primary alone so a test that exhausts its retries
    fails loudly instead of quietly reaching out to a real provider.
    """
    kwargs.setdefault("sleep", lambda _: None)
    kwargs.setdefault("providers", [settings.provider])
    return FailoverProvider(primary, settings, **kwargs)


MESSAGES = [{"role": "user", "content": "hi"}]


# -------------------------------------------------------------- is_retryable


@pytest.mark.parametrize("status", RETRYABLE_STATUSES)
def test_transient_statuses_are_retryable(status):
    assert is_retryable(ProviderError(f"openai: HTTP {status} something broke"))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retryable(status):
    assert not is_retryable(ProviderError(f"openai: HTTP {status} bad request"))


@pytest.mark.parametrize(
    "message",
    [
        "could not reach api.openai.com",
        "request timed out",
        "connection reset by peer",
        "the model is temporarily unavailable",
        "server overloaded",
        "rate limit exceeded",
    ],
)
def test_transient_phrases_are_retryable(message):
    assert is_retryable(ProviderError(message))


def test_phrase_matching_is_case_insensitive():
    assert is_retryable(ProviderError("Connection Reset"))


def test_an_unrecognised_error_is_not_retryable():
    assert not is_retryable(ProviderError("invalid model name"))


def test_a_status_inside_a_longer_message_is_found():
    assert is_retryable(ProviderError("anthropic: HTTP 529 overloaded_error"))


# -------------------------------------------------------- available_providers


def test_a_provider_without_a_key_is_not_offered(settings):
    """No ANTHROPIC_API_KEY means anthropic cannot serve a request."""
    assert "anthropic" not in available_providers(settings, env={})


def test_available_providers_finds_keys_in_the_environment(settings):
    env = {"ANTHROPIC_API_KEY": "a", "GROQ_API_KEY": "g"}

    found = available_providers(settings, env=env)

    assert "anthropic" in found
    assert "groq" in found


def test_the_active_provider_comes_first(settings):
    settings.provider = "groq"

    found = available_providers(settings, env={"ANTHROPIC_API_KEY": "a", "GROQ_API_KEY": "g"})

    assert found[0] == "groq"


def test_local_providers_need_no_key(settings):
    """A local model has no API key, but it is still a usable last resort."""
    found = available_providers(settings, env={})

    assert "ollama" in found


def test_no_duplicates(settings):
    found = available_providers(settings, env={"OPENAI_API_KEY": "k"})

    assert len(found) == len(set(found))


def test_fallback_order_is_complete_and_unique():
    assert len(FALLBACK_ORDER) == len(set(FALLBACK_ORDER))


# --------------------------------------------------------------------- policy


def test_backoff_grows_with_each_attempt():
    policy = FailoverPolicy(backoff=1.0, max_backoff=100.0)

    assert policy.delay_for(0) < policy.delay_for(4)


def test_backoff_is_capped():
    policy = FailoverPolicy(backoff=1.0, max_backoff=5.0)

    assert policy.delay_for(20) <= 5.0


def test_backoff_is_jittered():
    policy = FailoverPolicy(backoff=1.0, max_backoff=100.0)

    assert len({policy.delay_for(3) for _ in range(30)}) > 1


def test_backoff_is_never_negative():
    assert FailoverPolicy().delay_for(0) >= 0


# -------------------------------------------------------------------- retries


def test_a_transient_failure_is_retried_then_succeeds(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503 unavailable"), "recovered")

    result = wrap(primary, settings).complete(MESSAGES)

    assert result.content == "recovered"
    assert primary.calls == 2


def test_a_permanent_failure_is_not_retried(settings):
    primary = ScriptedProvider(ProviderError("HTTP 400 bad request"))

    with pytest.raises(ProviderError):
        wrap(primary, settings, policy=FailoverPolicy(chain=False)).complete(MESSAGES)

    assert primary.calls == 1


def test_retries_stop_at_the_attempt_limit(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503 unavailable"))

    with pytest.raises(ProviderError):
        wrap(primary, settings, policy=FailoverPolicy(attempts=3, chain=False)).complete(MESSAGES)

    assert primary.calls == 3


def test_attempts_of_one_disables_retrying(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503 unavailable"))

    with pytest.raises(ProviderError):
        wrap(primary, settings, policy=FailoverPolicy(attempts=1, chain=False)).complete(MESSAGES)

    assert primary.calls == 1


def test_a_success_on_the_first_try_does_not_sleep(settings):
    slept = []
    primary = ScriptedProvider("fine")

    wrap(primary, settings, sleep=slept.append).complete(MESSAGES)

    assert slept == []


def test_backoff_is_applied_between_retries(settings):
    slept = []
    primary = ScriptedProvider(ProviderError("HTTP 503"), "ok")

    wrap(primary, settings, sleep=slept.append, policy=FailoverPolicy(chain=False)).complete(
        MESSAGES
    )

    assert len(slept) == 1


# -------------------------------------------------------------------- chaining


def test_it_chains_to_the_next_provider(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503 unavailable"))
    backup = ScriptedProvider("from the backup")

    provider = wrap(
        primary,
        settings,
        build=lambda _s: backup,
        providers=["openai", "anthropic"],
        policy=FailoverPolicy(attempts=1),
    )

    assert provider.complete(MESSAGES).content == "from the backup"


def test_chaining_is_skipped_when_disabled(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503 unavailable"))
    backup = ScriptedProvider("never reached")

    with pytest.raises(ProviderError):
        wrap(
            primary,
            settings,
            build=lambda _s: backup,
            policy=FailoverPolicy(attempts=1, chain=False),
        ).complete(MESSAGES)

    assert backup.calls == 0


def test_the_last_error_surfaces_when_everything_fails(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503 primary is down"))
    backup = ScriptedProvider(ProviderError("HTTP 500 backup is down too"))

    with pytest.raises(ProviderError) as excinfo:
        wrap(
            primary,
            settings,
            build=lambda _s: backup,
            providers=["openai", "anthropic"],
            policy=FailoverPolicy(attempts=1),
        ).complete(MESSAGES)

    assert "down" in str(excinfo.value)


def test_a_provider_that_cannot_be_built_is_skipped(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503"))

    def build(_settings):
        raise ProviderError("no api key configured")

    with pytest.raises(ProviderError):
        wrap(
            primary,
            settings,
            build=build,
            providers=["openai", "anthropic"],
            policy=FailoverPolicy(attempts=1),
        ).complete(MESSAGES)


# ------------------------------------------------------------------ reporting


def test_each_failure_is_reported(settings):
    seen: list[Attempt] = []
    primary = ScriptedProvider(ProviderError("HTTP 503 unavailable"), "ok")

    wrap(primary, settings, on_failover=seen.append).complete(MESSAGES)

    assert len(seen) == 1
    assert seen[0].provider == "openai"
    assert "503" in seen[0].error


def test_attempts_are_recorded_on_the_provider(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503"), "ok")

    provider = wrap(primary, settings)
    provider.complete(MESSAGES)

    assert len(provider.attempts) == 1


def test_the_attempt_log_resets_between_calls(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503"), "ok", "ok again")
    provider = wrap(primary, settings)

    provider.complete(MESSAGES)
    provider.complete(MESSAGES)

    assert provider.attempts == []


def test_a_reporting_callback_is_optional(settings):
    primary = ScriptedProvider(ProviderError("HTTP 503"), "ok")

    assert wrap(primary, settings, on_failover=None).complete(MESSAGES).content == "ok"


# ----------------------------------------------------------------- delegation


def test_streaming_support_follows_the_primary(settings):
    class NoStream(ScriptedProvider):
        supports_streaming = False

    assert wrap(ScriptedProvider("x"), settings).supports_streaming is True
    assert wrap(NoStream("x"), settings).supports_streaming is False


def test_message_formatting_is_delegated(settings):
    primary = ScriptedProvider("x")

    formatted = wrap(primary, settings).format_assistant_message(AssistantMessage(content="hello"))

    assert formatted["content"] == "hello"


def test_tool_result_formatting_is_delegated(settings):
    formatted = wrap(ScriptedProvider("x"), settings).format_tool_result(None, "output")

    assert formatted["content"] == "output"


def test_the_wrapper_adopts_the_primarys_identity(settings):
    primary = ScriptedProvider("x", model="claude-3-5-sonnet-latest")

    assert wrap(primary, settings).model == "claude-3-5-sonnet-latest"


# --------------------------------------------------------------- health report


def test_health_report_defaults():
    report = HealthReport(provider="openai", ok=True)

    assert report.detail == ""
    assert report.models == []


def test_health_reports_do_not_share_a_model_list():
    a = HealthReport(provider="openai", ok=True)
    b = HealthReport(provider="groq", ok=True)
    a.models.append("gpt-4o-mini")

    assert b.models == []
