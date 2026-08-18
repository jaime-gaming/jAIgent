"""Persistent settings files and the precedence chain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jaigent import settings_store
from jaigent.config import Settings
from jaigent.errors import ConfigurationError
from jaigent.settings_store import (
    ALLOWED_KEYS,
    describe,
    load_layers,
    project_settings_path,
    read,
    set_value,
    unset_value,
    user_settings_path,
    validate_key,
    write,
)


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Isolated user and project settings files."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("JAIGENT_HOME", str(home))
    monkeypatch.chdir(project)
    return user_settings_path(), project_settings_path()


class TestValidation:
    def test_known_key(self) -> None:
        assert validate_key("Model") == "model"

    def test_dashes_are_normalised(self) -> None:
        assert validate_key("max-steps") == "max_steps"

    def test_unknown_key_lists_valid_ones(self) -> None:
        with pytest.raises(ConfigurationError, match="Unknown setting"):
            validate_key("modle")

    @pytest.mark.parametrize("secret", ["api_key", "search_api_key", "openai_api_key"])
    def test_secrets_are_refused(self, secret: str) -> None:
        with pytest.raises(ConfigurationError, match="never be stored"):
            validate_key(secret)


class TestReadWrite:
    def test_missing_file_is_empty(self, stores: tuple[Path, Path]) -> None:
        assert read(stores[0]) == {}

    def test_round_trip(self, stores: tuple[Path, Path]) -> None:
        write(stores[0], {"model": "gpt-4o", "max_steps": 5})
        assert read(stores[0]) == {"model": "gpt-4o", "max_steps": 5}

    def test_types_are_coerced(self, stores: tuple[Path, Path]) -> None:
        user, _ = stores
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(
            json.dumps({"max_steps": "7", "temperature": "0.9", "stream": "false"}),
            encoding="utf-8",
        )
        values = read(user)

        assert values["max_steps"] == 7
        assert values["temperature"] == 0.9
        assert values["stream"] is False

    def test_unknown_keys_are_dropped(self, stores: tuple[Path, Path]) -> None:
        user, _ = stores
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(json.dumps({"model": "x", "nonsense": 1}), encoding="utf-8")

        assert read(user) == {"model": "x"}

    def test_a_secret_in_the_file_is_ignored(self, stores: tuple[Path, Path]) -> None:
        user, _ = stores
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(json.dumps({"api_key": "sk-leak", "model": "x"}), encoding="utf-8")

        assert read(user) == {"model": "x"}

    def test_malformed_json_raises_clearly(self, stores: tuple[Path, Path]) -> None:
        user, _ = stores
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text("{oops", encoding="utf-8")

        with pytest.raises(ConfigurationError, match="not valid JSON"):
            read(user)

    def test_non_object_raises(self, stores: tuple[Path, Path]) -> None:
        user, _ = stores
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text("[1, 2]", encoding="utf-8")

        with pytest.raises(ConfigurationError, match="JSON object"):
            read(user)

    def test_bad_number_raises(self, stores: tuple[Path, Path]) -> None:
        user, _ = stores
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(json.dumps({"max_steps": "many"}), encoding="utf-8")

        with pytest.raises(ConfigurationError, match="must be an integer"):
            read(user)


class TestSetUnset:
    def test_set_creates_the_file(self, stores: tuple[Path, Path]) -> None:
        set_value("model", "gpt-4o")
        assert read(stores[0]) == {"model": "gpt-4o"}

    def test_set_project_scope(self, stores: tuple[Path, Path]) -> None:
        set_value("model", "gpt-4o", scope="project")

        assert read(stores[1]) == {"model": "gpt-4o"}
        assert read(stores[0]) == {}

    def test_set_preserves_other_keys(self, stores: tuple[Path, Path]) -> None:
        set_value("model", "a")
        set_value("max_steps", 9)

        assert read(stores[0]) == {"model": "a", "max_steps": 9}

    def test_unset(self, stores: tuple[Path, Path]) -> None:
        set_value("model", "a")
        assert unset_value("model") is True
        assert read(stores[0]) == {}

    def test_unset_missing_returns_false(self, stores: tuple[Path, Path]) -> None:
        assert unset_value("model") is False


class TestLayering:
    def test_project_overrides_user(self, stores: tuple[Path, Path]) -> None:
        set_value("model", "from-user")
        set_value("model", "from-project", scope="project")

        assert load_layers()["model"] == "from-project"

    def test_layers_merge_distinct_keys(self, stores: tuple[Path, Path]) -> None:
        set_value("model", "m")
        set_value("max_steps", 3, scope="project")

        merged = load_layers()
        assert merged == {"model": "m", "max_steps": 3}

    def test_describe_reports_the_source(self, stores: tuple[Path, Path]) -> None:
        set_value("model", "u")
        set_value("approval", "ask", scope="project")

        rows = {key: source for key, _, source in describe()}
        assert rows == {"model": "user", "approval": "project"}


class TestSettingsIntegration:
    """The files must actually change what Settings.from_env resolves."""

    def test_stored_model_is_used(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JAIGENT_MODEL", raising=False)
        set_value("model", "gpt-4.1-nano")

        assert Settings.from_env(dotenv=None).model == "gpt-4.1-nano"

    def test_environment_beats_the_file(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_value("model", "from-file")
        monkeypatch.setenv("JAIGENT_MODEL", "from-env")

        assert Settings.from_env(dotenv=None).model == "from-env"

    def test_stored_booleans_and_numbers(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("JAIGENT_MAX_STEPS", "JAIGENT_ALLOW_SHELL", "JAIGENT_STREAM"):
            monkeypatch.delenv(var, raising=False)
        set_value("max_steps", 25)
        set_value("allow_shell", True)
        set_value("stream", False)

        settings = Settings.from_env(dotenv=None)
        assert settings.max_steps == 25
        assert settings.allow_shell is True
        assert settings.stream is False

    def test_files_can_be_bypassed(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JAIGENT_MODEL", raising=False)
        set_value("model", "from-file")

        settings = Settings.from_env(dotenv=None, use_settings_files=False)
        assert settings.model != "from-file"

    def test_stored_provider_brings_its_defaults(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("JAIGENT_PROVIDER", "JAIGENT_MODEL", "JAIGENT_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        set_value("provider", "groq")

        settings = Settings.from_env(dotenv=None)
        assert settings.provider == "groq"
        assert settings.base_url == "https://api.groq.com/openai/v1"


def test_every_allowed_key_exists_on_settings() -> None:
    """A settable key that Settings does not have would silently do nothing."""
    settings = Settings(api_key="k")
    for key in ALLOWED_KEYS:
        assert hasattr(settings, key), f"Settings has no attribute {key!r}"


def test_paths_honour_jaigent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "h"))
    assert settings_store.user_settings_path() == tmp_path / "h" / "settings.json"
