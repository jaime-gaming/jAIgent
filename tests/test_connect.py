"""Guided first-run connection: verify before storing, never guess.

Every test here is offline. ``httpx`` is replaced with a stub that answers like
a provider would, so the decisions under test — what counts as a good key, what
gets stored, and what the user is told — never depend on the network.
"""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
from rich.console import Console

from jaigent import cli, connect
from jaigent.errors import ConfigurationError


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _Client:
    """Stands in for ``httpx.Client`` and records what was asked of it."""

    def __init__(self, status_code: int | None = 200, error: Exception | None = None) -> None:
        self.status_code = status_code
        self.error = error
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, headers: dict[str, str] | None = None, **_: Any) -> _Response:
        self.calls.append(("GET", url, headers or {}))
        if self.error is not None:
            raise self.error
        assert self.status_code is not None
        return _Response(self.status_code)


def _install(monkeypatch: pytest.MonkeyPatch, client: _Client) -> _Client:
    monkeypatch.setattr(connect.httpx, "Client", lambda **_: client)
    return client


class TestVerify:
    def test_a_good_key_is_accepted_and_only_the_key_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(monkeypatch, _Client(200))

        check = connect.verify_key("openai", "sk-good")

        assert check == connect.Check(True, "openai accepted the key", True)
        assert "rejected" not in check.detail
        method, url, headers = client.calls[0]
        assert method == "GET"
        assert headers["authorization"] == "Bearer sk-good"

    def test_a_rejected_key_is_conclusive_so_it_is_not_saved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _Client(401))

        check = connect.verify_key("openai", "sk-bad")

        assert check.ok is False
        assert check.conclusive is True
        assert "rejected" in check.detail

    def test_a_forbidden_key_is_also_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _Client(403))

        assert connect.verify_key("anthropic", "sk-bad").conclusive is True

    def test_a_rate_limited_key_is_accepted_and_said_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """429 means the key authenticated; saying "wrong key" would be a lie."""
        _install(monkeypatch, _Client(429))

        check = connect.verify_key("groq", "gsk-1")

        assert check.ok is True
        assert "rate limit" in check.detail

    def test_an_unexpected_status_is_not_called_a_bad_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _Client(404))

        check = connect.verify_key("openrouter", "sk-or-1")

        assert check.conclusive is False
        assert check.ok is True

    def test_a_network_failure_is_inconclusive_not_a_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _Client(error=httpx.ConnectError("offline")))

        check = connect.verify_key("openai", "sk-1")

        assert check.conclusive is False
        assert "could not reach" in check.detail

    def test_a_timeout_is_inconclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _Client(error=httpx.ReadTimeout("slow")))

        assert connect.verify_key("openai", "sk-1").conclusive is False

    def test_local_providers_need_no_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _Client(200))

        check = connect.verify_key("ollama", "anything")

        assert check.ok is True
        assert client.calls == []

    def test_an_empty_key_is_never_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _Client(200))

        check = connect.verify_key("openai", "   ")

        assert check.ok is False
        assert client.calls == []

    @pytest.mark.parametrize("provider", connect.CHOICES)
    def test_every_offered_provider_has_a_url_and_can_be_probed(
        self, monkeypatch: pytest.MonkeyPatch, provider: str
    ) -> None:
        _install(monkeypatch, _Client(200))

        check = connect.verify_key(provider, "key-123")

        assert connect.key_url(provider).startswith("https://")
        assert check.ok is True


class TestStore:
    def test_a_stored_key_is_found_again(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        path = connect.store_key("openai", "sk-stored")

        assert path.exists()
        assert connect.has_key("openai") is True
        assert "sk-stored" in path.read_text()

    def test_the_file_is_private(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))

        path = connect.store_key("openai", "sk-private")

        assert path.stat().st_mode & 0o077 == 0

    def test_an_environment_key_counts_as_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")

        assert connect.has_key("anthropic") is True

    def test_empty_keys_are_refused(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))

        with pytest.raises(ConfigurationError):
            connect.store_key("openai", "   ")

    def test_known_settings_names_a_model_and_url(self) -> None:
        settings = connect.known_settings("anthropic")

        assert settings.provider == "anthropic"
        assert settings.model
        assert "anthropic.com" in str(settings.base_url)
        assert settings.api_key == ""


def _fake_terminal(monkeypatch: pytest.MonkeyPatch, *, terminal: bool) -> None:
    """``Console.is_terminal`` is a read-only property, so patch the class.

    Patching the instance raises ``AttributeError``; the behaviour under test is
    "does the caller ask, or does it print advice", so the class is the right
    seam and it also exercises the real detection path in reverse.
    """
    from rich.console import Console as RichConsole

    monkeypatch.setattr(RichConsole, "is_terminal", property(lambda self: terminal))


class TestChatWiring:
    def test_chat_asks_instead_of_dying_when_no_key_is_stored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """The old behaviour printed a configuration error and exited."""
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JAIGENT_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        _fake_terminal(monkeypatch, terminal=True)
        monkeypatch.delenv("JAIGENT_NO_PROMPT", raising=False)

        chosen = {"provider": "anthropic"}
        monkeypatch.setattr(cli, "_choose_provider", lambda default="openai": chosen["provider"])
        monkeypatch.setattr(cli, "_paste_key", lambda provider: "sk-ant-pasted")
        monkeypatch.setattr(
            connect,
            "verify_key",
            lambda *a, **k: connect.Check(True, "anthropic accepted the key", True),
        )
        built: list[object] = []
        monkeypatch.setattr(cli, "build_agent", lambda settings: built.append(settings) or _stop())

        with pytest.raises(_Stop):
            cli.cmd_chat(_args())

        assert built, "the session should have been built with the new settings"
        settings = built[0]
        assert settings.provider == "anthropic"  # type: ignore[union-attr]

    def test_a_rejected_key_never_gets_stored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JAIGENT_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        _fake_terminal(monkeypatch, terminal=True)
        monkeypatch.delenv("JAIGENT_NO_PROMPT", raising=False)
        monkeypatch.setattr(cli, "_choose_provider", lambda default="openai": "openai")
        monkeypatch.setattr(cli, "_paste_key", lambda provider: "sk-bad")
        monkeypatch.setattr(
            connect,
            "verify_key",
            lambda *a, **k: connect.Check(False, "openai rejected the key", True),
        )
        stored: list[str] = []
        monkeypatch.setattr(connect, "store_key", lambda p, k: stored.append(k))

        code = cli.cmd_chat(_args())

        assert code == 78
        assert stored == []

    def test_a_pipe_gets_the_old_error_and_no_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JAIGENT_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        _fake_terminal(monkeypatch, terminal=False)
        asked: list[bool] = []
        monkeypatch.setattr(
            cli, "_choose_provider", lambda default="openai": asked.append(True) or ""
        )

        code = cli.cmd_chat(_args())

        assert code == 78
        assert asked == []

    def test_the_landing_page_says_what_is_actually_connected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "key_for_provider", lambda p: "sk-x" if p == "openai" else None)

        assert cli._connected_provider() == ("openai", cli.DEFAULT_MODELS["openai"])

    def test_a_local_provider_alone_is_not_called_connected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ollama answers with a placeholder key; nothing may be listening."""
        monkeypatch.setattr(
            cli, "key_for_provider", lambda p: "jaigent-local" if p in cli.LOCAL_PROVIDERS else None
        )

        assert cli._connected_provider() is None


class _Stop(Exception):
    """Raised by a stub to end the REPL as soon as it is reached."""


def _stop() -> None:
    raise _Stop


def _args() -> Any:
    import argparse

    return argparse.Namespace(
        provider=None,
        model=None,
        resume=None,
        no_save=True,
        approval=None,
        workspace=None,
        max_steps=None,
    )


def test_choices_come_from_the_config_module() -> None:
    """One list of providers, so the picker cannot drift from the settings."""
    from jaigent.config import KNOWN_PROVIDERS

    assert set(connect.CHOICES) <= set(KNOWN_PROVIDERS)


def test_the_console_output_names_the_provider_it_verified() -> None:
    console = Console(file=io.StringIO(), force_terminal=False, width=100)
    with console.capture() as capture:
        console.print(connect.Check(True, "openai accepted the key", True).detail)

    assert "openai" in capture.get()
