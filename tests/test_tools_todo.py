"""The task plan: validation, model feedback and the live progress summary."""

from __future__ import annotations

import pytest

from jaigent.errors import ToolError
from jaigent.tools import build_default_registry
from jaigent.tools.todo import MAX_TODOS, STATUSES, build_todo_tools, write_todos


def plan(*entries: tuple[str, str]) -> list[dict[str, str]]:
    return [{"title": title, "status": status} for title, status in entries]


class TestWriteTodos:
    def test_a_plan_reports_its_counts_back(self) -> None:
        out = write_todos(plan(("Scaffold routes", "done"), ("Wire up login", "in_progress")))

        assert "1/2 done" in out
        assert "[x] Scaffold routes" in out
        assert "[>] Wire up login" in out

    def test_pending_tasks_use_an_empty_box(self) -> None:
        out = write_todos(plan(("Add tests", "pending")))

        assert "[ ] Add tests" in out

    def test_a_missing_status_defaults_to_pending(self) -> None:
        out = write_todos([{"title": "Add tests"}])  # type: ignore[list-item]

        assert "[ ] Add tests" in out

    def test_titles_are_stripped(self) -> None:
        out = write_todos(plan(("  Add tests\n", "pending")))

        assert "[ ] Add tests" in out

    def test_status_is_case_insensitive(self) -> None:
        out = write_todos(plan(("Add tests", "DONE")))

        assert "[x] Add tests" in out


class TestValidation:
    """Every rejection must tell the model what to send instead."""

    def test_rejects_a_non_list(self) -> None:
        with pytest.raises(ToolError, match="must be a list"):
            write_todos("not a plan")  # type: ignore[arg-type]

    def test_rejects_an_empty_plan(self) -> None:
        with pytest.raises(ToolError, match="at least one task"):
            write_todos([])

    def test_rejects_too_many_tasks(self) -> None:
        with pytest.raises(ToolError, match=f"at most {MAX_TODOS}"):
            write_todos([{"title": f"task {i}", "status": "pending"} for i in range(MAX_TODOS + 1)])

    def test_rejects_a_non_object_task(self) -> None:
        with pytest.raises(ToolError, match="Task 1 must be"):
            write_todos(["Add tests"])  # type: ignore[list-item]

    def test_rejects_a_blank_title(self) -> None:
        with pytest.raises(ToolError, match="Task 2 needs a title"):
            write_todos(plan(("First", "done"), ("   ", "pending")))

    def test_rejects_an_unknown_status(self) -> None:
        with pytest.raises(ToolError, match="one of: pending, in_progress, done"):
            write_todos(plan(("Add tests", "started")))

    def test_rejects_two_tasks_in_progress(self) -> None:
        with pytest.raises(ToolError, match="Only one task can be in_progress"):
            write_todos(plan(("One", "in_progress"), ("Two", "in_progress")))

    def test_error_surfaces_through_the_registry(self, settings) -> None:  # noqa: ANN001
        registry = build_default_registry(settings)

        assert "ERROR" in registry.call("write_todos", {"todos": []})

    def test_all_statuses_are_reachable(self) -> None:
        assert set(STATUSES) == {"pending", "in_progress", "done"}


class TestRegistration:
    def test_the_tool_is_on_by_default(self, settings) -> None:  # noqa: ANN001
        assert "write_todos" in build_default_registry(settings)

    def test_the_schema_is_valid(self) -> None:
        (tool,) = build_todo_tools()

        assert tool.description.strip()
        assert tool.parameters["type"] == "object"
        assert "description" in tool.parameters["properties"]["todos"]
