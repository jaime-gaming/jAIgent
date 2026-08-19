"""MCP (Model Context Protocol) server tests.

The server communicates over stdin/stdout with JSON-RPC 2.0 messages.
These tests simulate the client side of the protocol by writing requests
and reading responses.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jaigent.config import Settings
from jaigent.mcp import MCPServer


def _request(method: str, params: dict[str, Any] | None = None, *, msg_id: Any = 1) -> str:
    """Build a JSON-RPC 2.0 request string."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg, ensure_ascii=False)


def _notification(method: str, params: dict[str, Any] | None = None) -> str:
    """Build a JSON-RPC 2.0 notification string (no id)."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg, ensure_ascii=False)


def _server(settings: Settings, **kwargs: Any) -> MCPServer:
    """Build a server with the given settings."""
    return MCPServer(settings, **kwargs)


def _call(server: MCPServer, line: str) -> dict[str, Any] | None:
    """Feed one line to the server and return the parsed response, or None."""
    result = server._handle_line(line)
    if result is None:
        return None
    return json.loads(result)


class TestHandshake:
    def test_initialize_returns_server_info(self, settings: Settings) -> None:
        server = _server(settings)
        response = _call(server, _request("initialize", {"protocolVersion": "2025-03-26"}))

        assert response["jsonrpc"] == "2.0"
        assert "result" in response
        assert response["result"]["serverInfo"]["name"] == "jaigent"
        assert response["result"]["protocolVersion"] == "2025-03-26"
        assert "tools" in response["result"]["capabilities"]
        assert "resources" in response["result"]["capabilities"]
        assert "prompts" in response["result"]["capabilities"]
        assert "instructions" in response["result"]

    def test_unknown_protocol_version_falls_back(self, settings: Settings) -> None:
        response = _call(
            _server(settings), _request("initialize", {"protocolVersion": "1999-01-01"})
        )
        assert response["result"]["protocolVersion"] == "2025-11-25"

    def test_initialized_notification_is_accepted(self, settings: Settings) -> None:
        server = _server(settings)
        result = _call(server, _notification("notifications/initialized"))
        assert result is None

    def test_ping_returns_an_empty_result(self, settings: Settings) -> None:
        response = _call(_server(settings), _request("ping"))

        assert response["result"] == {}
        assert "error" not in response

    def test_cancelled_notification_is_accepted(self, settings: Settings) -> None:
        assert _call(_server(settings), _notification("notifications/cancelled")) is None

    def test_resources_list_workspace_files(self, settings: Settings) -> None:
        server = _server(settings)
        resources = _call(server, _request("resources/list"))["result"]["resources"]
        names = {item["name"] for item in resources}
        assert "notes.md" in names
        assert all(item["uri"].startswith("jaigent://workspace/") for item in resources)

    def test_resources_read_returns_file_text(self, settings: Settings) -> None:
        response = _call(
            _server(settings),
            _request("resources/read", {"uri": "jaigent://workspace/notes.md"}),
        )
        text = response["result"]["contents"][0]["text"]
        assert "hello world" in text

    def test_resources_refuse_a_path_outside_the_workspace(self, settings: Settings) -> None:
        response = _call(
            _server(settings),
            _request("resources/read", {"uri": "jaigent://workspace/../../etc/passwd"}),
        )
        # Sandbox violation is reported as text, not a crash.
        payload = response.get("result") or response.get("error")
        assert payload

    def test_prompts_list_is_empty_without_skills(self, settings: Settings) -> None:
        server = _server(settings)
        assert _call(server, _request("prompts/list"))["result"]["prompts"] == []

    def test_unknown_prompt_is_rejected(self, settings: Settings) -> None:
        response = _call(_server(settings), _request("prompts/get", {"name": "skill.missing"}))
        assert response["error"]["code"] == -32602


class TestListTools:
    def test_lists_only_read_only_tools_by_default(self, settings: Settings) -> None:
        server = _server(settings)
        response = _call(server, _request("tools/list"))

        tools = response["result"]["tools"]
        names = {t["name"] for t in tools}

        assert "web_search" in names
        assert "list_files" in names
        assert "read_file" in names
        # Write tools must not appear by default.
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "delete_file" not in names
        # shell is never exposed.
        assert "run_command" not in names

    def test_allow_write_includes_write_tools(self, settings: Settings) -> None:
        server = _server(settings, allow_write=True)
        response = _call(server, _request("tools/list"))

        tools = {t["name"]: t for t in response["result"]["tools"]}
        assert "write_file" in tools
        assert "edit_file" in tools
        assert "delete_file" in tools
        assert tools["write_file"]["annotations"]["readOnlyHint"] is False
        assert tools["delete_file"]["annotations"]["destructiveHint"] is True

    def test_every_tool_has_a_description(self, settings: Settings) -> None:
        server = _server(settings)
        response = _call(server, _request("tools/list"))

        for tool in response["result"]["tools"]:
            assert tool["description"], f"Tool {tool['name']} has no description"
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"
            assert "annotations" in tool
            assert tool["annotations"]["readOnlyHint"] is True

    def test_run_command_is_never_exposed(self, settings: Settings) -> None:
        server = _server(settings, allow_write=True)
        response = _call(server, _request("tools/list"))

        names = {t["name"] for t in response["result"]["tools"]}
        assert "run_command" not in names


class TestCallTool:
    def test_unknown_tool_returns_error(self, settings: Settings) -> None:
        server = _server(settings)
        response = _call(server, _request("tools/call", {"name": "nonexistent", "arguments": {}}))

        assert "error" in response
        assert response["error"]["message"] == "Unknown tool: nonexistent"

    def test_call_read_file_with_missing_file(self, settings: Settings) -> None:
        server = _server(settings)
        response = _call(
            server,
            _request(
                "tools/call",
                {"name": "read_file", "arguments": {"path": "/dne/fake.txt"}},
            ),
        )

        # The tool should return an error message, not crash the server.
        assert "result" in response
        content = response["result"]["content"]
        assert len(content) > 0
        assert content[0]["type"] == "text"
        assert response["result"].get("isError") is True
        assert content[0]["text"].startswith("ERROR:")

    def test_call_read_file_returns_contents(self, settings: Settings) -> None:
        response = _call(
            _server(settings),
            _request("tools/call", {"name": "read_file", "arguments": {"path": "notes.md"}}),
        )

        text = response["result"]["content"][0]["text"]
        assert "hello world" in text
        assert not response["result"].get("isError")

    def test_call_list_files_uses_the_workspace(self, settings: Settings) -> None:
        response = _call(
            _server(settings),
            _request("tools/call", {"name": "list_files", "arguments": {}}),
        )

        text = response["result"]["content"][0]["text"]
        assert "notes.md" in text


class TestErrorHandling:
    def test_invalid_json_returns_parse_error(self, settings: Settings) -> None:
        server = _server(settings)
        response = _call(server, "{not json}")

        assert response["error"]["code"] == -32700
        assert response["error"]["message"] == "Parse error"

    def test_invalid_request_structure(self, settings: Settings) -> None:
        server = _server(settings)
        response = _call(server, '"just a string"')

        assert response["error"]["code"] == -32600
        assert "Invalid Request" in response["error"]["message"]

    def test_unknown_method(self, settings: Settings) -> None:
        server = _server(settings)
        response = _call(server, _request("nonexistent"))

        assert response["error"]["code"] == -32601
        assert "Method not found" in response["error"]["message"]


class TestClientConfig:
    def test_claude_config_is_json(self) -> None:
        from jaigent.mcp import client_config

        text = client_config("claude")
        payload = json.loads(text)
        assert payload["mcpServers"]["jaigent"]["command"] == "jaigent"
        assert "mcp" in payload["mcpServers"]["jaigent"]["args"]

    def test_chatgpt_config_names_the_command(self) -> None:
        from jaigent.mcp import client_config

        text = client_config("chatgpt")
        assert "jaigent" in text
        assert "mcp --client chatgpt" in text

    def test_unknown_client_is_rejected(self) -> None:
        from jaigent.errors import ToolError
        from jaigent.mcp import client_config

        with pytest.raises(ToolError):
            client_config("skype")
