"""Registry semantics and the never-crash contract of ToolRegistry.call."""

from __future__ import annotations

import pytest

from jaigent.errors import ToolError
from jaigent.tools import build_default_registry
from jaigent.tools.base import Tool, ToolRegistry

ECHO = Tool(
    name="echo",
    description="Echo the text back.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    func=lambda text: f"echo: {text}",
)


def test_register_and_get() -> None:
    registry = ToolRegistry()
    registry.register(ECHO)

    assert registry.get("echo") is ECHO
    assert "echo" in registry
    assert len(registry) == 1


def test_duplicate_registration_rejected() -> None:
    registry = ToolRegistry()
    registry.register(ECHO)
    with pytest.raises(ToolError, match="already registered"):
        registry.register(ECHO)


def test_duplicate_allowed_with_replace() -> None:
    registry = ToolRegistry()
    registry.register(ECHO)
    registry.register(ECHO, replace=True)
    assert len(registry) == 1


def test_unknown_tool_lists_alternatives() -> None:
    registry = ToolRegistry()
    registry.register(ECHO)
    with pytest.raises(ToolError, match="Available tools: echo"):
        registry.get("nope")


class TestCallNeverRaises:
    """Whatever happens, call() must return a string the model can read."""

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(ECHO)
        return registry

    def test_happy_path(self) -> None:
        assert self._registry().call("echo", {"text": "hi"}) == "echo: hi"

    def test_json_string_arguments_are_parsed(self) -> None:
        assert self._registry().call("echo", '{"text": "hi"}') == "echo: hi"

    def test_invalid_json_arguments(self) -> None:
        assert "not valid JSON" in self._registry().call("echo", "{oops")

    def test_unknown_tool(self) -> None:
        assert "Unknown tool" in self._registry().call("ghost", {})

    def test_wrong_arguments(self) -> None:
        assert "bad arguments" in self._registry().call("echo", {"wrong": 1})

    def test_exceptions_are_captured(self) -> None:
        registry = ToolRegistry()

        def boom() -> str:
            raise RuntimeError("kaboom")

        registry.register(Tool("boom", "d", {"type": "object", "properties": {}}, boom))
        assert "RuntimeError: kaboom" in registry.call("boom", {})

    def test_tool_error_is_prefixed(self) -> None:
        registry = ToolRegistry()

        def fail() -> str:
            raise ToolError("bad input")

        registry.register(Tool("fail", "d", {"type": "object", "properties": {}}, fail))
        assert registry.call("fail", {}) == "ERROR: bad input"

    def test_non_dict_arguments(self) -> None:
        assert "must be a JSON object" in self._registry().call("echo", "[1,2]")

    def test_non_string_result_is_serialised(self) -> None:
        registry = ToolRegistry()
        registry.register(
            Tool("num", "d", {"type": "object", "properties": {}}, lambda: {"a": 1})  # type: ignore[arg-type]
        )
        assert registry.call("num", {}) == '{"a": 1}'


class TestSchemas:
    def test_openai_schema(self) -> None:
        schema = ECHO.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "echo"
        assert schema["function"]["parameters"]["required"] == ["text"]

    def test_anthropic_schema(self) -> None:
        schema = ECHO.to_anthropic_schema()
        assert schema["name"] == "echo"
        assert "input_schema" in schema

    def test_every_default_tool_has_a_valid_schema(self, settings) -> None:  # noqa: ANN001
        for tool in build_default_registry(settings.merged_with(allow_shell=True)):
            assert tool.description.strip(), f"{tool.name} has no description"
            assert tool.parameters["type"] == "object"
            assert "properties" in tool.parameters
            for name, prop in tool.parameters["properties"].items():
                assert "description" in prop, f"{tool.name}.{name} lacks a description"


def test_default_registry_contents(settings) -> None:  # noqa: ANN001
    registry = build_default_registry(settings)
    assert set(registry.names()) == {
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "search_files",
        "web_search",
        "fetch_page",
        "load_skill",
    }


def test_shell_tool_added_when_allowed(settings) -> None:  # noqa: ANN001
    registry = build_default_registry(settings.merged_with(allow_shell=True))
    assert "run_command" in registry
