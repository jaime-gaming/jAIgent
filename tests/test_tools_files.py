"""Filesystem tool behaviour."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jaigent.errors import SandboxViolation, ToolError
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

    def test_none_arguments_fall_back_to_defaults(self, workspace: Path) -> None:
        # Models send explicit nulls for optional arguments routinely; the
        # old `max(1, offset)` answered "'>' not supported between instances
        # of 'NoneType' and 'int'".
        out = read_file(workspace, "notes.md", offset=None, limit=None)
        assert "1| # Notes" in out

    def test_string_arguments_are_coerced(self, workspace: Path) -> None:
        (workspace / "long.txt").write_text(
            "\n".join(f"line{i}" for i in range(100)), encoding="utf-8"
        )
        out = read_file(workspace, "long.txt", offset="10", limit="2")
        assert "10| line9" in out
        assert "11| line10" in out
        assert "12| line11" not in out

    def test_integral_float_is_accepted(self, workspace: Path) -> None:
        (workspace / "long.txt").write_text(
            "\n".join(f"line{i}" for i in range(100)), encoding="utf-8"
        )
        out = read_file(workspace, "long.txt", offset=2.0, limit=1.0)
        assert "2| line1" in out

    def test_fractional_and_junk_arguments_are_named(self, workspace: Path) -> None:
        with pytest.raises(ToolError, match="offset must be a whole number"):
            read_file(workspace, "notes.md", offset=1.5)
        with pytest.raises(ToolError, match="limit must be an integer"):
            read_file(workspace, "notes.md", limit="many")
        with pytest.raises(ToolError, match="offset must be an integer"):
            read_file(workspace, "notes.md", offset=True)

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


class TestTheToolsKeepTheLineEndingsAFileHad:
    """A write must not re-end lines nobody asked about.

    ``Path.write_text`` translates every ``\n`` to this platform's own ending,
    so rewriting one line of an LF file on Windows re-ended all of them, and
    editing a CRLF file on Linux flattened it. Either way a one-line change
    arrives as a whole-file diff in a repository whose ``.gitattributes`` asks
    for LF, and the model cannot see that it did it. The web panel keeps the
    ending it finds; these tools now share that one rule.
    """

    def test_rewriting_a_windows_file_keeps_its_endings(self, workspace: Path) -> None:
        target = workspace / "notes.bat"
        target.write_bytes(b"@echo off\r\necho one\r\n")
        write_file(workspace, "notes.bat", "@echo off\necho two\n")
        assert target.read_bytes() == b"@echo off\r\necho two\r\n"

    def test_rewriting_a_unix_file_keeps_its_endings(self, workspace: Path) -> None:
        target = workspace / "run.sh"
        target.write_bytes(b"#!/bin/sh\necho one\n")
        write_file(workspace, "run.sh", "#!/bin/sh\necho two\n")
        assert target.read_bytes() == b"#!/bin/sh\necho two\n"

    def test_appending_to_a_windows_file_does_not_mix_two_endings(self, workspace: Path) -> None:
        target = workspace / "notes.bat"
        target.write_bytes(b"@echo off\r\necho one\r\n")
        write_file(workspace, "notes.bat", "echo two\n", append=True)
        assert target.read_bytes() == b"@echo off\r\necho one\r\necho two\r\n"

    def test_a_new_file_gets_this_platforms_ending(self, workspace: Path) -> None:
        write_file(workspace, "brand-new.md", "one\ntwo\n")
        assert (workspace / "brand-new.md").read_bytes() == os.linesep.join(
            ["one", "two", ""]
        ).encode("utf-8")

    def test_editing_one_line_of_a_windows_file_leaves_the_others_alone(
        self, workspace: Path
    ) -> None:
        target = workspace / "notes.bat"
        target.write_bytes(b"@echo off\r\necho one\r\necho two\r\n")
        edit_file(workspace, "notes.bat", "echo one", "echo three")
        assert target.read_bytes() == b"@echo off\r\necho three\r\necho two\r\n"

    def test_a_snippet_copied_from_the_read_tool_matches_a_windows_file(
        self, workspace: Path
    ) -> None:
        """The model reads LF and writes LF back; that must still find the text.

        ``read_file`` hands the model normalised lines, so ``old_text`` arrives
        with ``\n`` even for a CRLF file. Matching against the file's own bytes
        instead would make every edit of a Windows file fail with "not found".
        """
        (workspace / "notes.bat").write_bytes(b"@echo off\r\necho one\r\n")
        assert "echo one" in read_file(workspace, "notes.bat")
        edit_file(workspace, "notes.bat", "echo one", "echo two")
        assert (workspace / "notes.bat").read_bytes() == b"@echo off\r\necho two\r\n"

    def test_lines_a_replacement_adds_follow_the_file(self, workspace: Path) -> None:
        target = workspace / "notes.bat"
        target.write_bytes(b"@echo off\r\n")
        edit_file(workspace, "notes.bat", "@echo off", "@echo off\necho two")
        assert target.read_bytes() == b"@echo off\r\necho two\r\n"

    def test_the_panel_and_the_tools_share_one_rule(self) -> None:
        """Two writers, one convention: neither may grow its own copy again."""
        from jaigent import webui
        from jaigent.tools import sandbox

        assert webui.line_ending_of is sandbox.line_ending_of


class TestWhatAWriteReports:
    def test_a_file_ending_in_a_break_does_not_count_an_extra_line(self, workspace: Path) -> None:
        """The old count was ``newlines + 1``, so every normal file read one too many."""
        assert "2 lines" in write_file(workspace, "out.txt", "one\ntwo\n")

    def test_one_line_is_not_plural(self, workspace: Path) -> None:
        assert "1 line)" in write_file(workspace, "out.txt", "only\n")

    def test_an_empty_write_says_zero_lines(self, workspace: Path) -> None:
        assert "0 lines" in write_file(workspace, "out.txt", "")


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


class TestSecretFilesAreRefused:
    def test_read_env_is_blocked(self, workspace: Path) -> None:
        (workspace / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
        with pytest.raises(SandboxViolation, match="secret"):
            read_file(workspace, ".env")

    def test_write_env_is_blocked(self, workspace: Path) -> None:
        with pytest.raises(SandboxViolation, match="secret"):
            write_file(workspace, ".env", "OPENAI_API_KEY=sk-leaked")
        assert not (workspace / ".env").exists()

    def test_search_skips_env(self, workspace: Path) -> None:
        (workspace / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
        out = search_files(workspace, "sk-secret")
        assert ".env" not in out
        assert "No matches" in out

    def test_list_skips_env(self, workspace: Path) -> None:
        (workspace / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
        assert ".env" not in list_files(workspace)

    def test_env_example_is_readable(self, workspace: Path) -> None:
        (workspace / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
        assert "OPENAI_API_KEY" in read_file(workspace, ".env.example")


def test_delete_tool_is_flagged_dangerous(workspace: Path) -> None:
    tools = {tool.name: tool for tool in build_file_tools(workspace)}
    assert tools["delete_file"].dangerous is True
    assert tools["read_file"].dangerous is False


class TestTraversal:
    def test_ignored_directories_are_never_entered(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rglob used to stat everything under node_modules before filtering."""
        deep = workspace / "node_modules" / "pkg" / "dist"
        deep.mkdir(parents=True)
        (deep / "bundle.js").write_text("x", encoding="utf-8")

        def explode(self, pattern="*"):  # noqa: ANN001, ANN002, ANN202
            pytest.fail("the walk must prune ignored directories, not rglob them")

        monkeypatch.setattr(Path, "rglob", explode)

        assert "bundle.js" not in list_files(workspace)
        assert "bundle.js" not in search_files(workspace, "x")

    def test_deeply_nested_noise_is_hidden(self, workspace: Path) -> None:
        deep = workspace / "src" / ".venv" / "lib" / "python3.11"
        deep.mkdir(parents=True)
        (deep / "secret.py").write_text("password = 1", encoding="utf-8")

        assert "secret.py" not in list_files(workspace)
        assert "secret.py" not in search_files(workspace, "password")

    def test_search_globs_match_relative_paths(self, workspace: Path) -> None:
        (workspace / "src" / "skip.txt").write_text("hello", encoding="utf-8")

        assert "skip.txt" in search_files(workspace, "hello", glob="src/*.txt")
        assert "No matches" in search_files(workspace, "hello", glob="*.md", path="src")

    def test_listing_is_sorted_and_capped(self, workspace: Path) -> None:
        for i in range(205):
            (workspace / f"file-{i:03d}.txt").write_text("x", encoding="utf-8")

        lines = list_files(workspace).splitlines()

        assert lines[-1] == "... truncated at 200 entries"
        assert lines[:-1] == sorted(lines[:-1])
        assert len(lines) == 201

    def test_search_results_are_sorted_and_capped(self, workspace: Path) -> None:
        for i in range(60):
            (workspace / f"hit-{i:02d}.txt").write_text("needle", encoding="utf-8")

        lines = search_files(workspace, "needle", max_results=50).splitlines()

        assert lines[-1] == "... truncated at 50 matches"
        assert len(lines) == 51
        assert lines[0].startswith("hit-00.txt:1:")

    def test_listing_a_single_file(self, workspace: Path) -> None:
        out = list_files(workspace, "notes.md")

        assert out.startswith("notes.md (")
        assert out.endswith(" bytes)")
