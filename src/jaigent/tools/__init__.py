"""Built-in tool collection and the registry that exposes it to the model."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from jaigent.tools.ask import build_ask_tools
from jaigent.tools.base import Tool, ToolFunc, ToolRegistry
from jaigent.tools.files import build_file_tools
from jaigent.tools.sandbox import resolve_in_workspace
from jaigent.tools.shell import build_shell_tools
from jaigent.tools.todo import build_todo_tools
from jaigent.tools.web import build_web_tools

if TYPE_CHECKING:  # pragma: no cover
    from jaigent.config import Settings

__all__ = [
    "Tool",
    "ToolFunc",
    "ToolRegistry",
    "build_ask_tools",
    "build_default_registry",
    "build_file_tools",
    "build_shell_tools",
    "build_todo_tools",
    "build_web_tools",
    "resolve_in_workspace",
]


def build_default_registry(
    settings: Settings,
    *,
    interactive: bool | None = None,
    console: Console | None = None,
) -> ToolRegistry:
    """Assemble the standard toolset for ``settings``.

    Includes the file and web tools always, ``ask_user`` for clarifying
    questions, ``write_todos`` for the visible task plan, ``load_skill`` when
    skills are enabled and at least one exists, and ``run_command`` when
    ``settings.allow_shell`` is enabled.

    ``interactive`` forces ``ask_user`` on or off; ``None`` probes the
    terminal. Non-interactive hosts (``serve``, schedules) pass False so the
    model is told nobody can answer instead of blocking on stdin.

    ``console`` is where ``ask_user`` renders. The CLI passes its own console
    so the question panel and any live status line coordinate instead of
    fighting over the screen; without one the tool builds a private console.
    """
    registry = ToolRegistry()
    workspace = Path(settings.workspace)
    registry.extend(build_ask_tools(console=console, interactive=interactive))
    registry.extend(build_todo_tools())
    registry.extend(build_file_tools(workspace))
    registry.extend(
        build_web_tools(
            backend=settings.search_backend,
            api_key=settings.search_api_key,
            timeout=settings.timeout,
        )
    )
    if getattr(settings, "skills_enabled", True):
        from jaigent.skills import build_skill_tools, discover

        registry.extend(build_skill_tools(discover()))
    if getattr(settings, "plugins_enabled", True):
        from jaigent.plugins import apply as apply_plugins

        apply_plugins(registry, settings)
    if getattr(settings, "memory", False):
        from jaigent.memory import build_memory_tools

        registry.extend(build_memory_tools(workspace))
    if settings.allow_shell:
        registry.extend(build_shell_tools(workspace))
    return registry
