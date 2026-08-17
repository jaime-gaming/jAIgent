"""Tool primitives: the :class:`Tool` descriptor and the :class:`ToolRegistry`."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from jaigent.errors import ToolError

#: A tool implementation receives validated keyword arguments and returns text.
ToolFunc = Callable[..., str]


@dataclass(slots=True, frozen=True)
class Tool:
    """A capability exposed to the model.

    Args:
        name: Identifier the model uses to call the tool. Must be unique.
        description: Explains *when* to use the tool. The model only sees this.
        parameters: JSON Schema object describing the arguments.
        func: Python callable implementing the tool.
        dangerous: Marks tools that mutate the machine; used by the CLI to warn.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunc
    dangerous: bool = False

    def __call__(self, **kwargs: Any) -> str:
        return self.func(**kwargs)

    def to_openai_schema(self) -> dict[str, Any]:
        """Serialise to the OpenAI ``tools`` array format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Serialise to the Anthropic ``tools`` array format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


@dataclass(slots=True)
class ToolRegistry:
    """An ordered collection of tools, addressable by name."""

    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        """Add ``tool`` to the registry.

        Raises:
            ToolError: if the name is already taken and ``replace`` is false.
        """
        if tool.name in self._tools and not replace:
            raise ToolError(f"A tool named {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def extend(self, tools: list[Tool], *, replace: bool = False) -> None:
        for tool in tools:
            self.register(tool, replace=replace)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._tools)) or "<none>"
            raise ToolError(f"Unknown tool {name!r}. Available tools: {known}") from exc

    def names(self) -> list[str]:
        return list(self._tools)

    def call(self, name: str, arguments: dict[str, Any] | str | None) -> str:
        """Invoke a tool, converting *any* failure into a readable string.

        The agent loop feeds the returned string straight back to the model, so
        errors must be descriptive enough for it to self-correct rather than
        crashing the whole run.
        """
        parsed: Any = arguments
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed or "{}")
            except json.JSONDecodeError as exc:
                return f"ERROR: arguments for {name!r} were not valid JSON: {exc}"
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            return (
                f"ERROR: arguments for {name!r} must be a JSON object, got {type(parsed).__name__}"
            )
        args: dict[str, Any] = parsed

        try:
            tool = self.get(name)
        except ToolError as exc:
            return f"ERROR: {exc}"

        try:
            result = tool(**args)
        except ToolError as exc:
            return f"ERROR: {exc}"
        except TypeError as exc:
            return f"ERROR: bad arguments for {name!r}: {exc}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, never fatal
            return f"ERROR: {type(exc).__name__}: {exc}"

        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    def to_openai_schema(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def to_anthropic_schema(self) -> list[dict[str, Any]]:
        return [tool.to_anthropic_schema() for tool in self._tools.values()]

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
