"""Local tool plugins."""

from __future__ import annotations

from pathlib import Path

import pytest

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
