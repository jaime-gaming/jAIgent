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


class TestValueValidation:
    """A stored value is read at every startup, so a bad one breaks every command.

    ``settings set provider notreal`` used to be accepted: the value was
    type-checked but never validated, and afterwards ``run``, ``models`` and
    ``route`` all failed with a configuration error.
    """

    def test_unknown_provider_is_refused(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            set_value("provider", "notreal")

        message = str(excinfo.value)
        assert "notreal" in message
        assert "openai" in message, "the error should list the valid providers"

    def test_a_refused_value_is_not_written(self, stores: tuple[Path, Path]) -> None:
        user, _ = stores
        with pytest.raises(ConfigurationError):
            set_value("provider", "notreal")

        assert not user.exists(), "the settings file was written despite the error"

    def test_a_valid_provider_still_works(self, stores: tuple[Path, Path]) -> None:
        set_value("provider", "anthropic")
        assert read(user_settings_path())["provider"] == "anthropic"

    def test_provider_is_normalised(self, stores: tuple[Path, Path]) -> None:
        set_value("provider", "  Anthropic  ")
        assert read(user_settings_path())["provider"] == "anthropic"

    def test_unknown_approval_mode_is_refused(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            set_value("approval", "whenever")
        assert "dry-run" in str(excinfo.value)

    @pytest.mark.parametrize("mode", ["ask", "auto", "dry-run"])
    def test_every_real_approval_mode_is_accepted(
        self, stores: tuple[Path, Path], mode: str
    ) -> None:
        set_value("approval", mode)
        assert read(user_settings_path())["approval"] == mode

    def test_unknown_search_backend_is_refused(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            set_value("search_backend", "altavista")
        assert "duckduckgo" in str(excinfo.value)

    @pytest.mark.parametrize("backend", ["duckduckgo", "tavily"])
    def test_every_real_search_backend_is_accepted(
        self, stores: tuple[Path, Path], backend: str
    ) -> None:
        set_value("search_backend", backend)
        assert read(user_settings_path())["search_backend"] == backend

    @pytest.mark.parametrize("key", ["provider", "model", "base_url"])
    def test_empty_strings_are_refused(self, stores: tuple[Path, Path], key: str) -> None:
        # An empty model silently falls back, which is worse than a clear error.
        with pytest.raises(ConfigurationError):
            set_value(key, "   ")

    @pytest.mark.parametrize(("key", "value"), [("max_steps", 0), ("retries", 0)])
    def test_counts_must_be_positive(self, stores: tuple[Path, Path], key: str, value: int) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            set_value(key, value)
        assert key in str(excinfo.value)

    @pytest.mark.parametrize("value", [-1, 0, -0.5])
    def test_timeout_must_be_positive(self, stores: tuple[Path, Path], value: float) -> None:
        with pytest.raises(ConfigurationError):
            set_value("timeout", value)

    @pytest.mark.parametrize("value", [-0.1, 2.5, 100])
    def test_temperature_out_of_range_is_refused(
        self, stores: tuple[Path, Path], value: float
    ) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            set_value("temperature", value)
        assert "temperature" in str(excinfo.value)

    @pytest.mark.parametrize("value", [0, 0.2, 1, 2])
    def test_temperature_in_range_is_accepted(
        self, stores: tuple[Path, Path], value: float
    ) -> None:
        set_value("temperature", value)
        assert read(user_settings_path())["temperature"] == float(value)

    def test_max_tokens_must_be_positive(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(ConfigurationError):
            set_value("max_tokens", 0)

    def test_budget_zero_is_accepted(self, stores: tuple[Path, Path]) -> None:
        set_value("budget", 0)
        assert read(user_settings_path())["budget"] == 0.0

    def test_budget_must_not_be_negative(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(ConfigurationError, match="budget"):
            set_value("budget", -0.01)

    def test_memory_and_auto_compact_are_settable(self, stores: tuple[Path, Path]) -> None:
        set_value("memory", True)
        set_value("auto_compact", "yes")
        stored = read(user_settings_path())
        assert stored["memory"] is True
        assert stored["auto_compact"] is True

    def test_the_cli_survives_a_rejected_value(self, stores: tuple[Path, Path]) -> None:
        # The whole point: after a refused write, settings still load cleanly.
        set_value("provider", "anthropic")
        with pytest.raises(ConfigurationError):
            set_value("provider", "notreal")

        assert load_layers()["provider"] == "anthropic"
