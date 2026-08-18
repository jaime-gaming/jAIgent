"""Human-in-the-loop approval: diffs, policies and refusal messages."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from jaigent.approval import (
    MUTATING_TOOLS,
    Approver,
    Mode,
    build_diff,
    preview,
)


def render(renderable, width: int = 90) -> str:
    console = Console(width=width, no_color=True)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


class TestDiff:
    def test_shows_added_and_removed_lines(self) -> None:
        out = render(build_diff("a\nb\n", "a\nc\n", "f.txt"))
        assert "-b" in out
        assert "+c" in out

    def test_identical_content(self) -> None:
        assert "no textual change" in render(build_diff("same", "same", "f.txt"))

    def test_long_diffs_are_truncated(self) -> None:
        before = "\n".join(str(i) for i in range(200))
        after = "\n".join(str(i * 2) for i in range(200))
        out = render(build_diff(before, after, "f.txt", limit=10))
        assert "more diff lines" in out


class TestPreview:
    def test_new_file_is_labelled(self, workspace: Path) -> None:
        out = render(preview("write_file", {"path": "new.txt", "content": "hi"}, workspace))
        assert "new file" in out
        assert "+hi" in out

    def test_overwrite_shows_both_sides(self, workspace: Path) -> None:
        out = render(preview("write_file", {"path": "notes.md", "content": "replaced"}, workspace))
        assert "-# Notes" in out
        assert "+replaced" in out

    def test_append_is_shown_as_addition(self, workspace: Path) -> None:
        out = render(
            preview(
                "write_file", {"path": "notes.md", "content": "tail", "append": True}, workspace
            )
        )
        assert "+" in out
        assert "-# Notes" not in out

    def test_edit_preview(self, workspace: Path) -> None:
        out = render(
            preview(
                "edit_file",
                {"path": "notes.md", "old_text": "hello world", "new_text": "goodbye"},
                workspace,
            )
        )
        assert "-hello world" in out
        assert "+goodbye" in out

    def test_delete_preview(self, workspace: Path) -> None:
        out = render(preview("delete_file", {"path": "notes.md"}, workspace))
        assert "delete notes.md" in out

    def test_command_preview(self, workspace: Path) -> None:
        out = render(preview("run_command", {"command": "pytest -q"}, workspace))
        assert "$ pytest -q" in out


class TestPolicies:
    def _approver(self, mode: Mode, answers: list[str] | None = None, **kw) -> Approver:
        queue = list(answers or [])
        return Approver(
            mode,
            console=Console(width=90, no_color=True),
            prompt=lambda _: queue.pop(0) if queue else "n",
            **kw,
        )

    def test_read_only_tools_never_need_approval(self) -> None:
        approver = self._approver(Mode.ASK)
        for tool in ("read_file", "list_files", "web_search", "search_files", "fetch_page"):
            assert approver.check(tool, {}).allowed is True

    def test_mutating_tools_are_the_gated_set(self) -> None:
        assert {"write_file", "edit_file", "delete_file", "run_command"} == MUTATING_TOOLS

    def test_auto_allows_everything(self, workspace: Path) -> None:
        approver = self._approver(Mode.AUTO, workspace=workspace)
        assert approver.check("write_file", {"path": "a", "content": "b"}).allowed is True

    def test_dry_run_refuses_with_a_useful_message(self, workspace: Path) -> None:
        approver = self._approver(Mode.DRY_RUN, workspace=workspace)
        decision = approver.check("write_file", {"path": "a", "content": "b"})

        assert decision.allowed is False
        assert "dry-run" in decision.reason
        assert "nothing was changed" in decision.reason

    def test_dry_run_still_allows_reads(self, workspace: Path) -> None:
        approver = self._approver(Mode.DRY_RUN, workspace=workspace)
        assert approver.check("read_file", {"path": "notes.md"}).allowed is True

    @pytest.mark.parametrize("answer", ["y", "yes", "Y", ""])
    def test_ask_accepts(self, workspace: Path, answer: str) -> None:
        approver = self._approver(Mode.ASK, [answer], workspace=workspace)
        assert approver.check("write_file", {"path": "n.txt", "content": "x"}).allowed is True

    @pytest.mark.parametrize("answer", ["n", "no", "anything else"])
    def test_ask_declines(self, workspace: Path, answer: str) -> None:
        approver = self._approver(Mode.ASK, [answer], workspace=workspace)
        decision = approver.check("write_file", {"path": "n.txt", "content": "x"})

        assert decision.allowed is False
        assert "declined" in decision.reason

    def test_always_stops_asking_for_that_tool(self, workspace: Path) -> None:
        approver = self._approver(Mode.ASK, ["a"], workspace=workspace)

        assert approver.check("write_file", {"path": "1", "content": "x"}).allowed is True
        # The prompt queue is empty now; a second call must not consult it.
        assert approver.check("write_file", {"path": "2", "content": "y"}).allowed is True
        assert "write_file" in approver.always

    def test_always_does_not_leak_to_other_tools(self, workspace: Path) -> None:
        approver = self._approver(Mode.ASK, ["a", "n"], workspace=workspace)
        approver.check("write_file", {"path": "1", "content": "x"})

        assert approver.check("delete_file", {"path": "notes.md"}).allowed is False

    def test_quit_raises_keyboard_interrupt(self, workspace: Path) -> None:
        approver = self._approver(Mode.ASK, ["q"], workspace=workspace)
        with pytest.raises(KeyboardInterrupt):
            approver.check("write_file", {"path": "n", "content": "x"})

    def test_mode_accepts_plain_strings(self) -> None:
        assert Approver("dry-run").mode is Mode.DRY_RUN
        assert Approver("auto").mode is Mode.AUTO
