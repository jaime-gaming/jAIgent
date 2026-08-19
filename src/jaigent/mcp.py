"""Model Context Protocol server for jaigent's tools.

JSON-RPC 2.0 over stdio for ChatGPT, Claude Desktop and any MCP client.
The client supplies the model, so no API key is needed here.

Read-only tools by default. ``--allow-write`` / ``JAIGENT_MCP_WRITE=1`` adds
write tools. ``run_command`` is never exposed.

Beyond tools, the server advertises:

* **resources** — workspace files, sandboxed, secrets skipped
* **prompts** — skills and custom commands, as MCP prompt templates
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from jaigent.commands import Command
from jaigent.commands import discover as discover_commands
from jaigent.config import Settings
from jaigent.errors import SandboxViolation, ToolError
from jaigent.skills import Skill
from jaigent.skills import discover as discover_skills
from jaigent.tools import Tool, ToolRegistry, build_default_registry
from jaigent.tools.files import IGNORED_DIRS, _is_ignored
from jaigent.tools.sandbox import (
    MAX_READ_BYTES,
    is_secret_path,
    relative_to_workspace,
    resolve_in_workspace,
)

JSONRPC_VERSION = "2.0"

#: Newest protocol revision we speak. Older clients keep their own version.
MCP_LATEST_VERSION = "2025-11-25"
MCP_SUPPORTED_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})
_BLOCKED_TOOLS = frozenset({"run_command"})

_RESOURCE_PREFIX = "jaigent://workspace/"
_MAX_RESOURCES = 100

_TOOL_TITLES = {
    "web_search": "Search the web",
    "fetch_page": "Fetch a web page",
    "list_files": "List files",
    "read_file": "Read a file",
    "write_file": "Write a file",
    "edit_file": "Edit a file",
    "search_files": "Search file contents",
    "delete_file": "Delete a file",
    "load_skill": "Load a skill",
}

SERVER_INSTRUCTIONS = (
    "You are connected to jaigent. Use tools to search the web and work with "
    "files in the user's workspace. Paths are relative to the workspace. "
    "Explore with list_files and read_file before you edit. Cite web sources "
    "you actually fetched."
)


def _rpc_error(id: Any, code: int, message: str, data: Any = None) -> str:
    err: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": id,
        "error": {"code": code, "message": message},
    }
    if data is not None:
        err["error"]["data"] = data
    return json.dumps(err, ensure_ascii=False)


def _rpc_result(id: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}, ensure_ascii=False)


def _negotiate_version(requested: str) -> str:
    if requested in MCP_SUPPORTED_VERSIONS:
        return requested
    return MCP_LATEST_VERSION


def _tool_title(name: str) -> str:
    if name in _TOOL_TITLES:
        return _TOOL_TITLES[name]
    return name.replace("_", " ").strip().title()


class MCPServer:
    """A JSON-RPC 2.0 stdio server implementing the MCP protocol."""

    def __init__(
        self,
        settings: Settings,
        *,
        allow_write: bool = False,
        client: str = "generic",
    ) -> None:
        self.settings = settings
        self.allow_write = allow_write
        self.client = (client or "generic").strip().lower()
        self._initialized = False

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

        self.skills: dict[str, Skill] = discover_skills()
        self.commands: dict[str, Command] = discover_commands()

    def serve_forever(self) -> None:
        """Read requests from stdin and respond on stdout until EOF."""
        _configure_stdio()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            response = self._handle_line(line)
            if response is not None:
                sys.stdout.write(response + "\n")
                sys.stdout.flush()

    def _handle_line(self, line: str) -> str | None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return _rpc_error(None, -32700, "Parse error")

        if not isinstance(message, dict):
            return _rpc_error(None, -32600, "Invalid Request: expected a JSON object")

        msg_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        handlers = {
            "initialize": self._handle_initialize,
            "ping": lambda i, _p: _rpc_result(i, {}),
            "logging/setLevel": lambda i, _p: _rpc_result(i, {}) if i is not None else None,
            "tools/list": self._handle_list_tools,
            "tools/call": self._handle_call_tool,
            "resources/list": self._handle_list_resources,
            "resources/read": self._handle_read_resource,
            "resources/templates/list": lambda i, _p: _rpc_result(i, {"resourceTemplates": []}),
            "prompts/list": self._handle_list_prompts,
            "prompts/get": self._handle_get_prompt,
        }
        if method == "notifications/initialized":
            self._initialized = True
            return None
        if method == "notifications/cancelled":
            return None
        handler = handlers.get(method)
        if handler is None:
            return _rpc_error(msg_id, -32601, f"Method not found: {method}")
        return handler(msg_id, params)

    def _handle_initialize(self, msg_id: Any, params: dict[str, Any]) -> str:
        requested = str(params.get("protocolVersion") or MCP_LATEST_VERSION)
        result = {
            "protocolVersion": _negotiate_version(requested),
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {
                "name": "jaigent",
                "version": self._server_version(),
                "title": "jaigent",
            },
            "instructions": SERVER_INSTRUCTIONS,
        }
        return _rpc_result(msg_id, result)

    def _handle_list_tools(self, msg_id: Any, params: dict[str, Any]) -> str:
        tools_mcp = []
        for tool in self.tools:
            schema: dict[str, Any] = {
                "name": tool.name,
                "title": _tool_title(tool.name),
                "description": tool.description,
                "inputSchema": tool.parameters or {"type": "object", "properties": {}},
                "annotations": {
                    "title": _tool_title(tool.name),
                    "readOnlyHint": tool.name not in _WRITE_TOOLS,
                    "destructiveHint": tool.name == "delete_file" or tool.dangerous,
                    "openWorldHint": tool.name in {"web_search", "fetch_page"},
                },
            }
            tools_mcp.append(schema)
        return _rpc_result(msg_id, {"tools": tools_mcp})

    def _handle_call_tool(self, msg_id: Any, params: dict[str, Any]) -> str:
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        if name not in self._tool_map:
            return _rpc_error(msg_id, -32602, f"Unknown tool: {name}")

        output = self.registry.call(name, arguments if isinstance(arguments, dict) else {})
        is_error = output.startswith("ERROR:")
        result: dict[str, Any] = {"content": [{"type": "text", "text": output}]}
        if is_error:
            result["isError"] = True
        return _rpc_result(msg_id, result)

    def _workspace_files(self) -> list[Path]:
        root = Path(self.settings.workspace)
        found: list[Path] = []
        if not root.is_dir():
            return found
        for item in sorted(root.rglob("*")):
            if not item.is_file() or _is_ignored(item, root) or is_secret_path(item):
                continue
            found.append(item)
            if len(found) >= _MAX_RESOURCES:
                break
        return found

    def _handle_list_resources(self, msg_id: Any, params: dict[str, Any]) -> str:
        resources = []
        for path in self._workspace_files():
            rel = relative_to_workspace(self.settings.workspace, path)
            mime, _ = mimetypes.guess_type(path.name)
            resources.append(
                {
                    "uri": f"{_RESOURCE_PREFIX}{rel}",
                    "name": rel,
                    "title": rel,
                    "mimeType": mime or "text/plain",
                }
            )
        return _rpc_result(msg_id, {"resources": resources})

    def _handle_read_resource(self, msg_id: Any, params: dict[str, Any]) -> str:
        uri = str(params.get("uri") or "")
        rel = _uri_to_relative(uri)
        if rel is None:
            return _rpc_error(msg_id, -32602, f"Unknown resource: {uri}")
        try:
            target = resolve_in_workspace(self.settings.workspace, rel)
        except SandboxViolation as exc:
            return _rpc_result(
                msg_id,
                {
                    "contents": [{"uri": uri, "mimeType": "text/plain", "text": f"ERROR: {exc}"}],
                },
            )
        if is_secret_path(target) or any(part in IGNORED_DIRS for part in target.parts):
            return _rpc_error(msg_id, -32602, f"Refusing to read {rel}")
        if not target.is_file():
            return _rpc_error(msg_id, -32602, f"Not a file: {rel}")
        if target.stat().st_size > MAX_READ_BYTES:
            return _rpc_error(
                msg_id,
                -32602,
                f"{rel} is larger than the {MAX_READ_BYTES:,} byte cap",
            )
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _rpc_error(msg_id, -32602, f"{rel} is not UTF-8 text")
        except OSError as exc:
            return _rpc_error(msg_id, -32602, str(exc))
        mime, _ = mimetypes.guess_type(target.name)
        return _rpc_result(
            msg_id,
            {"contents": [{"uri": uri, "mimeType": mime or "text/plain", "text": text}]},
        )

    def _handle_list_prompts(self, msg_id: Any, params: dict[str, Any]) -> str:
        prompts: list[dict[str, Any]] = []
        for skill in sorted(self.skills.values(), key=lambda s: s.name):
            prompts.append(
                {
                    "name": f"skill.{skill.name}",
                    "title": skill.name,
                    "description": skill.description or f"Skill: {skill.name}",
                }
            )
        for command in sorted(self.commands.values(), key=lambda c: c.name):
            prompts.append(
                {
                    "name": f"command.{command.name}",
                    "title": f"/{command.name}",
                    "description": command.description or f"Command: /{command.name}",
                    "arguments": [
                        {
                            "name": "arguments",
                            "description": "Text after the command name",
                            "required": False,
                        }
                    ],
                }
            )
        return _rpc_result(msg_id, {"prompts": prompts})

    def _handle_get_prompt(self, msg_id: Any, params: dict[str, Any]) -> str:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            text = self._render_prompt(name, arguments)
        except ToolError as exc:
            return _rpc_error(msg_id, -32602, str(exc))
        return _rpc_result(
            msg_id,
            {
                "description": name,
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            },
        )

    def _render_prompt(self, name: str, arguments: dict[str, Any]) -> str:
        if name.startswith("skill."):
            skill = self.skills.get(name[len("skill.") :])
            if skill is None:
                raise ToolError(f"Unknown prompt: {name}")
            return skill.render()
        if name.startswith("command."):
            command = self.commands.get(name[len("command.") :])
            if command is None:
                raise ToolError(f"Unknown prompt: {name}")
            extra = str(arguments.get("arguments") or "")
            return command.render(extra, workspace=str(self.settings.workspace))
        raise ToolError(f"Unknown prompt: {name}")

    @staticmethod
    def _server_version() -> str:
        from jaigent import __version__

        return __version__


def _uri_to_relative(uri: str) -> str | None:
    """Turn a resource URI into a workspace-relative path, or None."""
    if uri.startswith(_RESOURCE_PREFIX):
        return unquote(uri[len(_RESOURCE_PREFIX) :])
    parsed = urlparse(uri)
    if parsed.scheme == "file" and parsed.path:
        # Only a workspace-relative path is accepted. An absolute host path
        # such as file:///etc/passwd must not become "etc/passwd".
        raw = unquote(parsed.path)
        if raw.startswith("/") or parsed.netloc:
            return None
        return raw.lstrip("/")
    return None


def client_config(client: str) -> str:
    """A ready-to-paste snippet for Claude Desktop or ChatGPT."""
    name = (client or "").strip().lower()
    if name in {"claude", "claude-desktop"}:
        payload = {
            "mcpServers": {
                "jaigent": {
                    "command": "jaigent",
                    "args": ["mcp", "--client", "claude"],
                }
            }
        }
        return json.dumps(payload, indent=2) + "\n"
    if name in {"chatgpt", "openai"}:
        return (
            "ChatGPT custom MCP connector\n"
            "  Command    jaigent\n"
            "  Arguments  mcp --client chatgpt\n"
            "\n"
            "Read-only tools, workspace resources and skill/command prompts.\n"
            "Add --allow-write to also expose write_file, edit_file and delete_file.\n"
        )
    raise ToolError(f"Unknown client {client!r}. Expected claude or chatgpt.")


def _configure_stdio() -> None:
    """Force UTF-8 on stdin/stdout so Windows cp1252 cannot break the protocol."""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")


def run_mcp(
    settings: Settings,
    *,
    allow_write: bool = False,
    client: str = "generic",
) -> int:
    """Start the MCP server and block until stdin closes."""
    server = MCPServer(settings, allow_write=allow_write, client=client)
    with contextlib.suppress(BrokenPipeError, OSError):
        server.serve_forever()
    return 0
