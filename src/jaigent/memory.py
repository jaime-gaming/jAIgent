"""Optional project memory.

Off by default. Turn it on with ``jaigent settings set memory true`` (or
``JAIGENT_MEMORY=1``). Notes live in ``.jaigent/memory.md`` inside the
workspace — never sent anywhere until the setting is on, and never written
outside the sandbox.
"""

from __future__ import annotations

from pathlib import Path

from jaigent.errors import ToolError
from jaigent.paths import project_home
from jaigent.tools.base import Tool
from jaigent.tools.sandbox import resolve_in_workspace

MEMORY_FILENAME = "memory.md"
MAX_MEMORY_CHARS = 20_000


def memory_path(workspace: Path) -> Path:
    """``<workspace>/.jaigent/memory.md``."""
    return project_home(workspace) / MEMORY_FILENAME


def load_memory(workspace: Path) -> str:
    """Return the stored notes, or an empty string."""
    path = memory_path(workspace)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:MAX_MEMORY_CHARS]
    except OSError:
        return ""


def append_memory(workspace: Path, note: str) -> str:
    """Append one note. Returns a short confirmation."""
    text = (note or "").strip()
    if not text:
        raise ToolError("note must not be empty")
    if len(text) > 2_000:
        raise ToolError("A single memory note must be under 2,000 characters.")

    # Confirm the path cannot leave the workspace even though we build it.
    target = resolve_in_workspace(workspace, Path(".jaigent") / MEMORY_FILENAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = load_memory(workspace)
    if len(existing) + len(text) + 2 > MAX_MEMORY_CHARS:
        raise ToolError(
            f"Memory is full ({MAX_MEMORY_CHARS:,} characters). "
            "Edit or delete .jaigent/memory.md, then try again."
        )
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    target.write_text(existing + prefix + text + "\n", encoding="utf-8")
    return f"Remembered ({len(text)} chars)."


def build_memory_tools(workspace: Path) -> list[Tool]:
    """Tools the model uses to read and write project memory."""
    return [
        Tool(
            name="remember",
            description=(
                "Save a short standing fact about this project for later turns. "
                "Use for preferences, decisions, names and conventions — not secrets."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "One or two sentences to remember.",
                    }
                },
                "required": ["note"],
            },
            func=lambda note: append_memory(workspace, note),
        ),
        Tool(
            name="recall",
            description="Read everything currently stored in project memory.",
            parameters={"type": "object", "properties": {}, "required": []},
            func=lambda: load_memory(workspace) or "(memory is empty)",
        ),
    ]


def memory_prompt_block(workspace: Path) -> str:
    """A short block for the system prompt, or empty if there is nothing yet."""
    body = load_memory(workspace).strip()
    if not body:
        return (
            "\n\nProject memory is on. Use the remember tool for standing facts "
            "and recall to read what is stored. Do not store secrets."
        )
    return (
        "\n\nProject memory (standing notes for this workspace):\n"
        f"{body}\n"
        "Update it with the remember tool. Do not store secrets."
    )
