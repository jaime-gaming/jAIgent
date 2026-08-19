"""Model Context Protocol server for jaigent's tools.

This module implements a JSON-RPC 2.0 server over stdio that speaks the
`Model Context Protocol (MCP) <https://spec.modelcontextprotocol.io>`_,
letting ChatGPT, Claude Desktop and other MCP clients use jaigent's
sandboxed tools.

The client supplies the model via its own configuration, so no API key or
provider is needed here — this is purely a tool server.

Read-only tools are exposed by default. Pass ``--allow-write`` or set
``JAIGENT_MCP_WRITE=1`` to also expose write tools (write_file, edit_file,
delete_file). ``run_command`` is never exposed.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any

from jaigent.config import Settings
from jaigent.tools import Tool, ToolRegistry, build_default_registry

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 helpers
# ---------------------------------------------------------------------------

JSONRPC_VERSION = "2.0"


def _rpc_error(id: Any, code: int, message: str, data: Any = None) -> str:
    """Build a JSON-RPC 2.0 error response."""
    err: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": id,
        "error": {"code": code, "message": message},
    }
    if data is not None:
        err["error"]["data"] = data
    return json.dumps(err, ensure_ascii=False)


def _rpc_result(id: Any, result: Any) -> str:
    """Build a JSON-RPC 2.0 success response."""
    return json.dumps({"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}, ensure_ascii=False)


def _rpc_notification(method: str, params: dict[str, Any] | None = None) -> str:
    """Build a JSON-RPC 2.0 notification (no id)."""
    msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params:
        msg["params"] = params
    return json.dumps(msg, ensure_ascii=False)


# ---------------------------------------------------------------------------
# MCP protocol constants
# ---------------------------------------------------------------------------

MCP_LATEST_VERSION = "2025-03-26"

#: Tool names that modify the workspace, gated behind --allow-write.
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})

#: Tool names that are never exposed via MCP.
_BLOCKED_TOOLS = frozenset({"run_command"})


def _is_read_only(tool: Tool) -> bool:
    """Whether a tool is safe to expose without write permission."""
    if tool.name in _BLOCKED_TOOLS:
        return False
    return tool.name not in _WRITE_TOOLS


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


class MCPServer:
    """A JSON-RPC 2.0 stdio server implementing the MCP protocol.

    Reads JSON-RPC requests from stdin and writes responses to stdout.
    Protocol errors, tool failures and invalid arguments are returned as
    JSON-RPC errors — they never crash the server.
    """

    def __init__(self, settings: Settings, *, allow_write: bool = False) -> None:
        self.settings = settings
        self.allow_write = allow_write
        self._initialized = False

        # Build the tool registry with the right set of tools, then keep a
        # filtered registry so calls go through ToolRegistry.call (errors
        # become text, never a crash).
        built = build_default_registry(settings)
        self.tools: list[Tool] = []
        for tool in built:
            if tool.name in _BLOCKED_TOOLS:
                continue
            if tool.name in _WRITE_TOOLS and not allow_write:
                continue
            self.tools.append(tool)

        self.registry = ToolRegistry()
        self.registry.extend(self.tools)
        self._tool_map: dict[str, Tool] = {t.name: t for t in self.tools}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def serve_forever(self) -> None:
        """Read requests from stdin and respond on stdout until EOF.

        Stdin and stdout are used in text mode, line-delimited. stderr is
        reserved for diagnostics the client never sees.
        """
        # MCP stdio is UTF-8 JSON. Windows consoles default to cp1252 and
        # would raise UnicodeDecodeError on the first non-ASCII argument.
        _configure_stdio()

        # Use the raw binary streams wrapped in text I/O so we get proper
        # line buffering without blocking on partial reads.
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            response = self._handle_line(line)
            if response is not None:
                sys.stdout.write(response + "\n")
                sys.stdout.flush()

    def _handle_line(self, line: str) -> str | None:
        """Process one JSON-RPC message. Returns a response string or None
        (for notifications)."""
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return _rpc_error(None, -32700, "Parse error")

        if not isinstance(message, dict):
            return _rpc_error(None, -32600, "Invalid Request: expected a JSON object")

        # Notifications have no id.
        msg_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params", {})

        if method == "initialize":
            return self._handle_initialize(msg_id, params)
        if method == "notifications/initialized":
            self._initialized = True
            return None  # notification
        if method == "notifications/cancelled":
            return None
        if method == "ping":
            return _rpc_result(msg_id, {})
        if method == "logging/setLevel":
            return _rpc_result(msg_id, {}) if msg_id is not None else None
        if method == "tools/list":
            return self._handle_list_tools(msg_id, params)
        if method == "tools/call":
            return self._handle_call_tool(msg_id, params)
        # Clients often probe these even when we only expose tools. Empty
        # lists are more compatible than "Method not found".
        if method == "resources/list":
            return _rpc_result(msg_id, {"resources": []})
        if method == "resources/templates/list":
            return _rpc_result(msg_id, {"resourceTemplates": []})
        if method == "prompts/list":
            return _rpc_result(msg_id, {"prompts": []})
        return _rpc_error(msg_id, -32601, f"Method not found: {method}")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, msg_id: Any, params: dict[str, Any]) -> str:
        """Respond to the MCP initialize handshake."""
        # Extract protocol version from the client's capabilities.
        client_version = params.get("protocolVersion", MCP_LATEST_VERSION)
        self._initialized = True

        result = {
            "protocolVersion": client_version,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "jaigent",
                "version": self._server_version(),
            },
        }
        return _rpc_result(msg_id, result)

    def _handle_list_tools(self, msg_id: Any, params: dict[str, Any]) -> str:
        """Return the list of available tools as MCP tool schemas."""
        tools_mcp = []
        for tool in self.tools:
            schema = {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.parameters or {"type": "object", "properties": {}},
                "annotations": {
                    "readOnlyHint": tool.name not in _WRITE_TOOLS,
                    "destructiveHint": tool.name == "delete_file" or tool.dangerous,
                    "openWorldHint": tool.name in {"web_search", "fetch_page"},
                },
            }
            tools_mcp.append(schema)

        return _rpc_result(msg_id, {"tools": tools_mcp})

    def _handle_call_tool(self, msg_id: Any, params: dict[str, Any]) -> str:
        """Execute a tool and return its result."""
        name = params.get("name", "")
        arguments = params.get("arguments", {})

        if name not in self._tool_map:
            return _rpc_error(msg_id, -32602, f"Unknown tool: {name}")

        output = self.registry.call(name, arguments if isinstance(arguments, dict) else {})
        is_error = output.startswith("ERROR:")
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": output}],
        }
        if is_error:
            result["isError"] = True
        return _rpc_result(msg_id, result)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _server_version() -> str:
        from jaigent import __version__  # noqa: PLC0415 - avoid import at module level

        return __version__


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _configure_stdio() -> None:
    """Force UTF-8 on stdin/stdout so Windows cp1252 cannot break the protocol."""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")


def run_mcp(settings: Settings, *, allow_write: bool = False) -> int:
    """Start the MCP server and block until stdin closes."""
    server = MCPServer(settings, allow_write=allow_write)
    with contextlib.suppress(BrokenPipeError, OSError):
        server.serve_forever()
    return 0
