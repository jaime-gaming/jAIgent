"""Package scaffolding and remote-serving policy tests for ChatGPT."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jaigent import cli
from jaigent.config import Settings
from jaigent.tools import Tool, ToolRegistry, build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def _tool(name: str, *, dangerous: bool = False, read_only: bool = False) -> Tool:
    return Tool(
        name=name,
        description=f"Test tool {name}",
        parameters={"type": "object", "properties": {}},
        func=lambda: "ok",
        dangerous=dangerous,
        read_only=read_only,
    )


def test_plugin_creator_starter_uses_current_manifest_layout() -> None:
    root = ROOT / "integrations" / "chatgpt"
    portable = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    compatibility = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    app_config = json.loads((root / ".app.json").read_text(encoding="utf-8"))

    assert portable["name"] == "jaigent"
    assert portable["version"] == compatibility["version"] == "0.5.7"
    assert portable["extensions"]["com.openai"]["apps"] == "./.app.json"
    assert portable["extensions"]["com.openai"]["interface"]["brandColor"] == "#FF8A00"
    assert compatibility["apps"] == "./.app.json"
    assert compatibility["skills"] == "./skills/"
    assert app_config == {"apps": {}}
    assert (root / "skills" / "jaigent" / "SKILL.md").is_file()
    assert not (root / "ai-plugin.json").exists()


def test_builtin_tools_declare_read_only_state_conservatively(tmp_path: Path) -> None:
    settings = Settings(
        provider="openai",
        model="test-model",
        api_key="test-key",
        workspace=tmp_path,
        memory=True,
        plugins_enabled=False,
    )
    tools = {tool.name: tool for tool in build_default_registry(settings, interactive=False)}

    assert tools["read_file"].read_only is True
    assert tools["search_files"].read_only is True
    assert tools["write_file"].read_only is False
    assert tools["edit_file"].read_only is False
    assert tools["delete_file"].read_only is False
    assert tools["remember"].read_only is False
    assert tools["recall"].read_only is True


def test_read_only_serve_filters_writers_even_if_shell_was_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        provider="openai",
        model="test-model",
        api_key="test-key",
        workspace=tmp_path,
        allow_shell=True,
    )
    registry = ToolRegistry()
    registry.extend(
        [
            _tool("read_file", read_only=True),
            _tool("write_file"),
            _tool("edit_file"),
            _tool("delete_file", dangerous=True),
            _tool("run_command", dangerous=True),
            _tool("remember"),
            _tool("safe_custom", read_only=True),
        ]
    )
    captured: dict[str, Any] = {}

    class AgentStub:
        def __init__(self, settings: Settings, *, tools: ToolRegistry, **_kwargs: Any) -> None:
            self.settings = settings
            self.tools = tools

    class ServerStub:
        def serve_forever(self) -> None:
            pass

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(cli, "resolve_settings", lambda _args: settings)
    monkeypatch.setattr(cli, "build_default_registry", lambda *_args, **_kwargs: registry)
    monkeypatch.setattr(cli, "Agent", AgentStub)
    monkeypatch.setattr(cli.gateway, "load_keys", lambda: [])

    def build_server(factory: Any, config: Any) -> ServerStub:
        agent = factory()
        captured["tool_names"] = agent.tools.names()
        captured["allow_shell"] = agent.settings.allow_shell
        captured["read_only"] = config.read_only
        captured["shell_enabled"] = config.allow_shell
        return ServerStub()

    monkeypatch.setattr(cli.gateway, "build_server", build_server)
    args = cli.build_parser().parse_args(["serve", "--read-only", "--allow-shell"])

    assert cli.cmd_serve(args) == 0
    assert set(captured["tool_names"]) == {"read_file", "safe_custom"}
    assert captured["allow_shell"] is False
    assert captured["read_only"] is True
    assert captured["shell_enabled"] is False
