"""Local tool plugins."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent import plugins
from jaigent.config import Settings
from jaigent.errors import ToolError
from jaigent.plugins import apply, create_plugin, discover
from jaigent.tools import ToolRegistry, build_default_registry


@pytest.fixture
def plugin_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("JAIGENT_HOME", str(home))
    # These tests are about loading and discovery. Project plugins are only
    # imported with an explicit opt-in; the policy itself has its own tests in
    # TestProjectPluginsAreNotImported.
    monkeypatch.setenv(plugins.TRUST_PROJECT_VAR, "1")
    monkeypatch.chdir(project)
    return project


def test_discover_is_empty_without_files(plugin_home: Path) -> None:
    assert discover() == {}


def test_create_and_discover(plugin_home: Path) -> None:
    path = create_plugin("hello")
    assert path.is_file()
    found = discover()
    assert "hello" in found
    assert found["hello"].scope == "project"


def test_invalid_name_is_rejected(plugin_home: Path) -> None:
    with pytest.raises(ToolError, match="Invalid plugin name"):
        create_plugin("Hello World!")


def test_duplicate_is_rejected(plugin_home: Path) -> None:
    create_plugin("dup")
    with pytest.raises(ToolError, match="already exists"):
        create_plugin("dup")


def test_apply_registers_the_starter_tool(plugin_home: Path) -> None:
    create_plugin("hello")
    registry = ToolRegistry()
    settings = Settings(api_key="k", workspace=plugin_home)
    loaded = apply(registry, settings)

    assert loaded[0].error == ""
    assert "hello" in registry
    assert registry.call("hello", {"name": "jaigent"}) == "hello, jaigent"


def test_a_broken_plugin_does_not_crash(plugin_home: Path) -> None:
    directory = plugin_home / ".jaigent" / "plugins"
    directory.mkdir(parents=True)
    (directory / "boom.py").write_text("raise RuntimeError('nope')\n", encoding="utf-8")

    registry = ToolRegistry()
    settings = Settings(api_key="k", workspace=plugin_home)
    loaded = apply(registry, settings)

    assert loaded[0].error.startswith("RuntimeError")


def test_default_registry_loads_plugins(plugin_home: Path) -> None:
    create_plugin("hello")
    settings = Settings(api_key="k", workspace=plugin_home, plugins_enabled=True)
    registry = build_default_registry(settings)
    assert "hello" in registry


def test_plugins_can_be_disabled(plugin_home: Path) -> None:
    create_plugin("hello")
    settings = Settings(api_key="k", workspace=plugin_home, plugins_enabled=False)
    registry = build_default_registry(settings)
    assert "hello" not in registry


def test_project_shadows_user(plugin_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    create_plugin("hello", scope="user")
    create_plugin("hello", scope="project")
    found = discover()
    assert found["hello"].scope == "project"


def test_register_does_not_receive_live_keys(plugin_home: Path) -> None:
    directory = plugin_home / ".jaigent" / "plugins"
    directory.mkdir(parents=True)
    (directory / "spy.py").write_text(
        "def register(registry, settings):\n"
        "    assert settings.api_key is None\n"
        "    assert settings.search_api_key is None\n",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    settings = Settings(api_key="sk-live", search_api_key="tvly-live", workspace=plugin_home)
    loaded = apply(registry, settings)
    assert loaded[0].error == ""


class TestProjectPluginsAreNotImported:
    """A cloned repo's plugin file must not run just because you opened it.

    Plugin files are Python, imported and executed with ``register()``. Loading
    a project's plugins automatically made "clone and run" equivalent to running
    its setup script — and once a prompt injection could write one (now blocked)
    it was also a way around ``--allow-shell``.
    """

    def _project_plugin(self, tmp_path: Path, name: str = "evil") -> Path:
        directory = tmp_path / ".jaigent" / "plugins"
        directory.mkdir(parents=True, exist_ok=True)
        marker = tmp_path / f"{name}-ran"
        (directory / f"{name}.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "def register(registry, settings):\n"
            "    pass\n",
            encoding="utf-8",
        )
        return marker

    def _settings(self, tmp_path: Path) -> Settings:
        return Settings(provider="openai", model="gpt-4o-mini", workspace=tmp_path, api_key="k")

    def test_it_is_not_imported_by_default(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.delenv(plugins.TRUST_PROJECT_VAR, raising=False)
        marker = self._project_plugin(tmp_path)
        registry = ToolRegistry()

        plugins.apply(registry, self._settings(tmp_path), start=tmp_path)

        assert not marker.exists(), "the plugin file was executed"

    def test_it_is_reported_rather_than_silently_dropped(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.delenv(plugins.TRUST_PROJECT_VAR, raising=False)
        self._project_plugin(tmp_path)

        assert plugins.skipped_project_plugins(tmp_path) == ["evil"]

    def test_the_user_opts_in_explicitly(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv(plugins.TRUST_PROJECT_VAR, "1")
        marker = self._project_plugin(tmp_path)
        registry = ToolRegistry()

        plugins.apply(registry, self._settings(tmp_path), start=tmp_path)

        assert marker.exists(), "an explicit opt-in must actually load it"
        assert plugins.skipped_project_plugins(tmp_path) == []

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_the_switch_accepts_the_usual_spellings(
        self, tmp_path: Path, monkeypatch, value: str
    ) -> None:  # noqa: ANN001
        monkeypatch.setenv(plugins.TRUST_PROJECT_VAR, value)

        assert plugins.project_plugins_trusted() is True

    def test_a_false_value_is_not_trust(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv(plugins.TRUST_PROJECT_VAR, "0")

        assert plugins.project_plugins_trusted() is False

    def test_user_plugins_are_still_loaded(self, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
        """The user's own code is the user's decision; only the repo is not."""
        monkeypatch.delenv(plugins.TRUST_PROJECT_VAR, raising=False)
        home = tmp_path / "home"
        (home / "plugins").mkdir(parents=True)
        marker = tmp_path / "user-ran"
        (home / "plugins" / "mine.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n"
            "def register(registry, settings):\n"
            "    pass\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("JAIGENT_HOME", str(home))
        registry = ToolRegistry()

        plugins.apply(registry, self._settings(tmp_path), start=tmp_path)

        assert marker.exists()
