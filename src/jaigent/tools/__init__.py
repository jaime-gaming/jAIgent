"""Built-in tool collection and the registry that exposes it to the model."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from jaigent.tools.base import Tool, ToolFunc, ToolRegistry
from jaigent.tools.files import build_file_tools
from jaigent.tools.sandbox import resolve_in_workspace
from jaigent.tools.shell import build_shell_tools
from jaigent.tools.web import build_web_tools

if TYPE_CHECKING:  # pragma: no cover
    from jaigent.config import Settings

__all__ = [
    "Tool",
    "ToolFunc",
    "ToolRegistry",
    "build_default_registry",
    "build_file_tools",
    "build_shell_tools",
    "build_web_tools",
    "resolve_in_workspace",
]


def build_default_registry(settings: Settings) -> ToolRegistry:
    """Assemble the standard toolset for ``settings``.

    Includes the file and web tools always, plus ``run_command`` when
    ``settings.allow_shell`` is enabled.
    """
    registry = ToolRegistry()
    workspace = Path(settings.workspace)
    registry.extend(build_file_tools(workspace))
    registry.extend(
        build_web_tools(
            backend=settings.search_backend,
            api_key=settings.search_api_key,
            timeout=settings.timeout,
        )
    )
    if settings.allow_shell:
        registry.extend(build_shell_tools(workspace))
    return registry
