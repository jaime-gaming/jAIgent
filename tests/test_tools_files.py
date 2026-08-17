"""Filesystem tool behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent.errors import ToolError
from jaigent.tools.files import (
    build_file_tools,
    delete_file,
    edit_file,
    list_files,
    read_file,
    search_files,
    write_file,
)


class TestListFiles:
    def test_lists_recursively(self, workspace: Path) -> None:
        out = list_files(workspace)
        assert "notes.md" in out
        assert "src/app.py" in out

    def test_glob_filter(self, workspace: Path) -> None:
        out = list_files(workspace, pattern="*.py")
        assert "app.py" in out
        assert "notes.md" not in out

    def test_non_recursive(self, workspace: Path) -> None:
        out = list_files(workspace, recursive=False)
        assert "src/app.py" not in out

    def test_skips_noise_directories(self, workspace: Path) -> None:
        (workspace / "__pycache__").mkdir()
        (workspace / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")
        assert "junk.pyc" not in list_files(workspace)

    def test_missing_path(self, workspace: Path) -> None:
        with pytest.raises(ToolError, match="does not exist"):
            list_files(workspace, "nope")


class TestReadFile:
    def test_reads_with_line_numbers(self, workspace: Path) -> None:
        out = read_file(workspace, "notes.md")
        assert "1| # Notes" in out
        assert "2| hello world" in out

    def test_pagination(self, workspace: Path) -> None:
        (workspace / "long.txt").write_text(
            "\n".join(f"line{i}" for i in range(100)), encoding="utf-8"
        )
        out = read_file(workspace, "long.txt", offset=1, limit=10)
        assert "line0" in out
        assert "line50" not in out
        assert "more lines" in out

    def test_offset_past_end(self, workspace: Path) -> None:
        assert "past the end" in read_file(workspace, "notes.md", offset=999)

    def test_binary_file_is_rejected(self, workspace: Path) -> None:
        (workspace / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
        with pytest.raises(ToolError, match="binary"):
            read_file(workspace, "blob.bin")

    def test_directory_is_rejected(self, workspace: Path) -> None:
        with pytest.raises(ToolError, match="not a readable file"):
            read_file(workspace, "src")


class TestWriteFile:
    def test_creates_file_and_parents(self, workspace: Path) -> None:
        out = write_file(workspace, "deep/nested/out.txt", "hi")
        assert (workspace / "deep" / "nested" / "out.txt").read_text(encoding="utf-8") == "hi"
        assert "Wrote" in out

    def test_overwrites(self, workspace: Path) -> None:
        write_file(workspace, "notes.md", "replaced")
        assert (workspace / "notes.md").read_text(encoding="utf-8") == "replaced"

    def test_appends(self, workspace: Path) -> None:
        write_file(workspace, "notes.md", "extra\n", append=True)
        text = (workspace / "notes.md").read_text(encoding="utf-8")
        assert text.startswith("# Notes")
        assert text.endswith("extra\n")


class TestEditFile:
    def test_replaces_unique_snippet(self, workspace: Path) -> None:
        edit_file(workspace, "notes.md", "hello world", "goodbye world")
        assert "goodbye world" in (workspace / "notes.md").read_text(encoding="utf-8")

    def test_missing_snippet_errors(self, workspace: Path) -> None:
        with pytest.raises(ToolError, match="not found"):
            edit_file(workspace, "notes.md", "absent", "x")

    def test_ambiguous_snippet_errors(self, workspace: Path) -> None:
        (workspace / "dup.txt").write_text("a\na\n", encoding="utf-8")
        with pytest.raises(ToolError, match="appears 2 times"):
            edit_file(workspace, "dup.txt", "a", "b")

    def test_replace_all(self, workspace: Path) -> None:
        (workspace / "dup.txt").write_text("a\na\na\n", encoding="utf-8")
        out = edit_file(workspace, "dup.txt", "a", "b", count=-1)
        assert (workspace / "dup.txt").read_text(encoding="utf-8") == "b\nb\nb\n"
        assert "3 occurrence" in out

    def test_empty_old_text_errors(self, workspace: Path) -> None:
        with pytest.raises(ToolError, match="must not be empty"):
            edit_file(workspace, "notes.md", "", "x")


class TestDeleteFile:
    def test_deletes_file(self, workspace: Path) -> None:
        delete_file(workspace, "notes.md")
        assert not (workspace / "notes.md").exists()

    def test_refuses_workspace_root(self, workspace: Path) -> None:
        with pytest.raises(ToolError, match="workspace root"):
            delete_file(workspace, ".")

    def test_refuses_non_empty_directory(self, workspace: Path) -> None:
        with pytest.raises(ToolError, match="not empty"):
            delete_file(workspace, "src")

    def test_missing_file(self, workspace: Path) -> None:
        with pytest.raises(ToolError, match="does not exist"):
            delete_file(workspace, "ghost.txt")


class TestSearchFiles:
    def test_finds_substring_case_insensitively(self, workspace: Path) -> None:
        out = search_files(workspace, "HELLO")
        assert "notes.md:2" in out

    def test_regex_mode(self, workspace: Path) -> None:
        out = search_files(workspace, r"def \w+\(", regex=True)
        assert "app.py:1" in out

    def test_glob_restriction(self, workspace: Path) -> None:
        assert "No matches" in search_files(workspace, "hello", glob="*.py")

    def test_invalid_regex(self, workspace: Path) -> None:
        with pytest.raises(ToolError, match="Invalid regular expression"):
            search_files(workspace, "([", regex=True)

    def test_no_matches_message(self, workspace: Path) -> None:
        assert "No matches" in search_files(workspace, "zzzz-not-here")


def test_build_file_tools_exposes_expected_names(workspace: Path) -> None:
    names = {tool.name for tool in build_file_tools(workspace)}
    assert names == {
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "search_files",
    }


def test_delete_tool_is_flagged_dangerous(workspace: Path) -> None:
    tools = {tool.name: tool for tool in build_file_tools(workspace)}
    assert tools["delete_file"].dangerous is True
    assert tools["read_file"].dangerous is False
