"""Settings resolution, dotenv loading and secret redaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent.config import DEFAULT_MODELS, Settings, key_for_provider, load_dotenv
from jaigent.errors import ConfigurationError


@pytest.mark.usefixtures("clean_env")
class TestFromEnv:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        settings = Settings.from_env(dotenv=None)

        assert settings.provider == "openai"
        assert settings.model == DEFAULT_MODELS["openai"]
        assert settings.api_key is None
        assert settings.max_steps == 12

    def test_reads_provider_specific_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-123")
        monkeypatch.setenv("JAIGENT_PROVIDER", "anthropic")
        settings = Settings.from_env(dotenv=None)

        assert settings.api_key == "sk-ant-123"
        assert settings.model == DEFAULT_MODELS["anthropic"]

    def test_generic_key_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_API_KEY", "generic")
        monkeypatch.setenv("OPENAI_API_KEY", "specific")
        assert Settings.from_env(dotenv=None).api_key == "generic"

    def test_unknown_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_PROVIDER", "skynet")
        with pytest.raises(ConfigurationError, match="Unknown provider"):
            Settings.from_env(dotenv=None)

    def test_numeric_and_boolean_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_MAX_STEPS", "30")
        monkeypatch.setenv("JAIGENT_TEMPERATURE", "0.9")
        monkeypatch.setenv("JAIGENT_ALLOW_SHELL", "true")
        settings = Settings.from_env(dotenv=None)

        assert settings.max_steps == 30
        assert settings.temperature == 0.9
        assert settings.allow_shell is True

    def test_bad_integer_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_MAX_STEPS", "many")
        with pytest.raises(ConfigurationError, match="must be an integer"):
            Settings.from_env(dotenv=None)


class TestKeyForProvider:
    def test_uses_the_provider_specific_variable(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        monkeypatch.setenv("GROQ_API_KEY", "gsk-groq")
        monkeypatch.setenv("JAIGENT_API_KEY", "sk-openai")
        assert key_for_provider("groq") == "gsk-groq"

    def test_does_not_reuse_the_generic_key_for_another_provider(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        monkeypatch.setenv("JAIGENT_API_KEY", "sk-openai")
        assert key_for_provider("groq") is None

    def test_local_providers_get_a_placeholder(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        assert key_for_provider("ollama") == "jaigent-local"


class TestValidation:
    def test_workspace_is_absolute(self, tmp_path: Path) -> None:
        assert Settings(workspace=tmp_path).workspace.is_absolute()

    def test_max_steps_must_be_positive(self) -> None:
        with pytest.raises(ConfigurationError, match="max_steps"):
            Settings(max_steps=0)

    def test_require_api_key_explains_how_to_set_it(self) -> None:
        with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
            Settings(api_key=None).require_api_key()

    def test_require_api_key_returns_the_key(self) -> None:
        assert Settings(api_key="sk-abc").require_api_key() == "sk-abc"


class TestMergedWith:
    def test_overrides_applied(self) -> None:
        merged = Settings(model="a").merged_with(model="b", temperature=0.7)
        assert merged.model == "b"
        assert merged.temperature == 0.7

    def test_none_values_ignored(self) -> None:
        merged = Settings(model="a").merged_with(model=None)
        assert merged.model == "a"


class TestRedaction:
    def test_api_key_is_masked(self) -> None:
        redacted = Settings(api_key="sk-supersecretvalue").redacted()
        assert "supersecret" not in str(redacted)
        assert redacted["api_key"] == "<set>"

    def test_unset_key(self) -> None:
        assert Settings(api_key=None).redacted()["api_key"] == "<unset>"


class TestDotenv:
    def test_parses_pairs_comments_and_quotes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# a comment\n\nFOO=bar\nexport BAZ='quoted'\nQUX=\"double\"\nbroken-line\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("FOO", raising=False)
        applied = load_dotenv(env_file)

        assert applied == {"FOO": "bar", "BAZ": "quoted", "QUX": "double"}

    def test_existing_env_wins_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=from_file\n", encoding="utf-8")
        monkeypatch.setenv("FOO", "from_shell")
        load_dotenv(env_file)

        import os

        assert os.environ["FOO"] == "from_shell"

    def test_override_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=from_file\n", encoding="utf-8")
        monkeypatch.setenv("FOO", "from_shell")
        load_dotenv(env_file, override=True)

        import os

        assert os.environ["FOO"] == "from_file"

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_dotenv(tmp_path / "absent.env") == {}


class TestReliabilitySettings:
    """checkpoints, failover and retries across the precedence chain."""

    def test_defaults_are_on(self, clean_env: None) -> None:
        settings = Settings.from_env()

        assert settings.checkpoints is True
        assert settings.failover is True
        assert settings.retries == 3

    @pytest.mark.parametrize("value", ["0", "false", "no"])
    def test_checkpoints_can_be_disabled_by_env(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("JAIGENT_CHECKPOINTS", value)

        assert Settings.from_env().checkpoints is False

    @pytest.mark.parametrize("value", ["0", "false", "no"])
    def test_failover_can_be_disabled_by_env(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("JAIGENT_FAILOVER", value)

        assert Settings.from_env().failover is False

    def test_retries_comes_from_the_environment(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JAIGENT_RETRIES", "7")

        assert Settings.from_env().retries == 7

    @pytest.mark.parametrize("bad", [0, -1])
    def test_retries_below_one_is_rejected(self, bad: int) -> None:
        with pytest.raises(ConfigurationError, match="retries"):
            Settings(api_key="k", retries=bad)

    def test_one_retry_is_allowed_and_means_no_retrying(self) -> None:
        assert Settings(api_key="k", retries=1).retries == 1

    def test_they_survive_redaction(self) -> None:
        redacted = Settings(api_key="k", retries=5).redacted()

        assert redacted["retries"] == 5
        assert redacted["checkpoints"] is True
