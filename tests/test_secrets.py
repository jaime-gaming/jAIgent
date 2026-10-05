"""Provider keys stored in the private user secrets file."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent.config import Settings
from jaigent.errors import ConfigurationError
from jaigent.secrets import listed_keys, load_user_secrets, mask_key, set_key, unset_key


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("JAIGENT_API_KEY", raising=False)
    return tmp_path / "home"


def test_set_key_is_loaded_by_settings(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_key("openai", "sk-from-secrets")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    load_user_secrets()
    settings = Settings.from_env(dotenv=None)
    assert settings.api_key == "sk-from-secrets"


def test_list_masks_the_secret(home: Path) -> None:
    set_key("openai", "sk-supersecret-value")
    rows = listed_keys()
    assert rows[0][0] == "openai"
    assert "supersecret" not in rows[0][2]
    assert mask_key("sk-abcdef") != "sk-abcdef"


def test_unset(home: Path) -> None:
    set_key("groq", "gsk-x")
    assert unset_key("groq") is True
    assert listed_keys() == []


def test_empty_key_is_refused(home: Path) -> None:
    with pytest.raises(ConfigurationError):
        set_key("openai", "  ")


def test_multiline_key_is_refused_instead_of_truncated(home: Path) -> None:
    """A key pasted with an embedded newline used to be written as-is and read
    back as only its first line — a truncated, unusable credential saved with
    no warning."""
    with pytest.raises(ConfigurationError, match="single line"):
        set_key("openai", "sk-line1\nsk-line2")
    assert listed_keys() == []


def test_key_with_control_character_is_refused(home: Path) -> None:
    with pytest.raises(ConfigurationError, match="control characters"):
        set_key("openai", "sk-tab\there")
    assert listed_keys() == []


def test_key_with_surrounding_newlines_is_accepted(home: Path) -> None:
    set_key("openai", "sk-clean\n")
    assert listed_keys()[0][0] == "openai"


def test_local_provider_needs_no_key(home: Path) -> None:
    with pytest.raises(ConfigurationError, match="locally"):
        set_key("ollama", "x")
