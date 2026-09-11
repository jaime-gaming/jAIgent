"""A shared task plan the model maintains for multi-step work.

``write_todos`` replaces the whole plan on every call: the model sends the full
list with updated statuses, the way it would rewrite a checklist on a
whiteboard. The CLI renders the new list the moment it arrives, so the plan is
visible live while the agent works.

Stateless by design. The latest call in the conversation holds the current
plan, so there is nothing to persist, migrate, or keep in sync — and nothing
for ``/workspace`` switches or ``/resume`` to carry over.
"""

from __future__ import annotations

from typing import Any

from jaigent.errors import ToolError
from jaigent.tools.base import Tool

__all__ = ["MAX_TODOS", "STATUSES", "build_todo_tools", "write_todos"]

#: The only states a task can be in.
STATUSES = ("pending", "in_progress", "done")

#: A plan longer than this is a project breakdown, not a checklist.
MAX_TODOS = 20


def _validate(todos: Any) -> list[dict[str, str]]:
    """Check the model's plan and normalise it to ``[{title, status}]``."""
    if not isinstance(todos, list):
        raise ToolError(
            "todos must be a list of {title, status} objects, "
            f"got {type(todos).__name__}. Send the full plan on every call."
        )
    if not todos:
        raise ToolError(
            "The plan needs at least one task. Keep finished work as done "
            "instead of removing it, so the record of what happened survives."
        )
    if len(todos) > MAX_TODOS:
        raise ToolError(
            f"The plan holds at most {MAX_TODOS} tasks, got {len(todos)}. "
            "Group smaller steps into fewer tasks."
        )
    items: list[dict[str, str]] = []
    for index, entry in enumerate(todos, start=1):
        if not isinstance(entry, dict):
            raise ToolError(
                f"Task {index} must be a {{title, status}} object, got {type(entry).__name__}."
            )
        title = str(entry.get("title", "")).strip()
        if not title:
            raise ToolError(f"Task {index} needs a title. Describe the step in a few words.")
        status = str(entry.get("status", "pending")).strip().lower()
        if status not in STATUSES:
            raise ToolError(
                f"Task {index} has status {status!r}. Expected one of: {', '.join(STATUSES)}."
            )
        items.append({"title": title, "status": status})
    if sum(1 for item in items if item["status"] == "in_progress") > 1:
        raise ToolError(
            "Only one task can be in_progress at a time. Finish or park the "
            "others first, then promote the next one."
        )
    return items


def write_todos(todos: list[dict[str, str]]) -> str:
    """Replace the task plan and report the new state back to the model."""
    items = _validate(todos)
    done = sum(1 for item in items if item["status"] == "done")
    marks = {"done": "x", "in_progress": ">", "pending": " "}
    lines = [f"Plan updated: {done}/{len(items)} done."]
    for item in items:
        lines.append(f"[{marks[item['status']]}] {item['title']}")
    return "\n".join(lines)


def build_todo_tools() -> list[Tool]:
    """Create the task-plan tool. It holds no state, so it needs no arguments."""
    return [
        Tool(
            name="write_todos",
            description=(
                "Track a multi-step task as a visible checklist the user watches "
                "live. Call it with the FULL plan — every task and its status — "
                "whenever the plan changes: when you start (first task in_progress, "
                "the rest pending), when a task finishes (mark it done and promote "
                "the next one), and when the plan itself changes. Skip it for "
                "single-step requests. Statuses are pending, in_progress (exactly "
                "one at a time) and done."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "The full plan: every task with its current status.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "The step in a few words.",
                                },
                                "status": {
                                    "type": "string",
                                    "description": "pending, in_progress or done.",
                                },
                            },
                            "required": ["title", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
            func=lambda todos: write_todos(todos),
        ),
    ]
