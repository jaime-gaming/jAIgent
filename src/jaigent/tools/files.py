"""Filesystem tools: list, read, write, edit, delete and search files.

All of them are confined to the agent workspace by
:func:`jaigent.tools.sandbox.resolve_in_workspace`.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator
from pathlib import Path

from jaigent.errors import ToolError
from jaigent.tools.base import Tool
from jaigent.tools.sandbox import (
    ensure_size_ok,
    is_secret_path,
    refuse_if_blocked,
    relative_to_workspace,
    resolve_in_workspace,
)

IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".jaigent",
}

MAX_MATCHES = 200


def _is_ignored(path: Path, root: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.relative_to(root).parts)


def _walk_tree(root: Path) -> Iterator[Path]:
    """Yield every file and directory under ``root``, pruning noise first.

    ``rglob("*")`` descends into ``node_modules`` and friends and the old code
    then ``sorted()`` the whole walk — on a real project that is hundreds of
    thousands of stats before the 200-entry cap ever applies. This walk skips
    ignored directories without entering them, never follows symlinked
    directories (like ``rglob``), and tolerates unreadable directories.
    """
    stack = [root]
    while stack:
        try:
            children = sorted(stack.pop().iterdir())
        except OSError:
            continue
        for child in children:
            if child.name in IGNORED_DIRS:
                continue
            yield child
            if child.is_dir() and not child.is_symlink():
                stack.append(child)


def _search_candidates(workspace: Path, root: Path, glob: str) -> Iterator[Path]:
    """Files to grep: lazily, so the hit cap stops the walk.

    The old ``sorted(root.rglob(glob))`` stat'ed the whole tree — ignored
    directories included — before the cap could stop it. The glob matches
    against the workspace-relative path, as before.
    """
    if root.is_file():
        yield root
        return
    for item in _walk_tree(root):
        if item.is_file() and fnmatch.fnmatch(relative_to_workspace(workspace, item), glob):
            yield item


# ----------------------------------------------------------------------
# Implementations
# ----------------------------------------------------------------------
def list_files(workspace: Path, path: str = ".", pattern: str = "*", recursive: bool = True) -> str:
    root = resolve_in_workspace(workspace, path)
    if not root.exists():
        raise ToolError(f"{path!r} does not exist")
    if root.is_file():
        refuse_if_blocked(workspace, root)
        return f"{relative_to_workspace(workspace, root)} ({root.stat().st_size} bytes)"

    entries: list[str] = []
    # Lazily, so the cap stops the walk instead of sorting the whole tree
    # first; the capped set is sorted for stable output. Non-recursive uses
    # iterdir rather than glob("*") so dotfiles are listed: the glob hid
    # them, and even an explicit pattern=".*" matched nothing.
    iterator = _walk_tree(root) if recursive else iter(sorted(root.iterdir()))
    for item in iterator:
        if _is_ignored(item, workspace) or is_secret_path(item):
            continue
        if not fnmatch.fnmatch(item.name, pattern):
            continue
        rel = relative_to_workspace(workspace, item)
        entries.append(f"{rel}/" if item.is_dir() else f"{rel} ({item.stat().st_size} B)")
        if len(entries) >= MAX_MATCHES:
            truncated = True
            break
    else:
        truncated = False

    if not entries:
        return f"No entries matching {pattern!r} under {path!r}"
    entries.sort()
    if truncated:
        entries.append(f"... truncated at {MAX_MATCHES} entries")
    return "\n".join(entries)


def read_file(workspace: Path, path: str, offset: int = 1, limit: int = 500) -> str:
    target = resolve_in_workspace(workspace, path)
    refuse_if_blocked(workspace, target)
    if not target.is_file():
        raise ToolError(f"{path!r} is not a readable file")
    ensure_size_ok(target)

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"{path!r} is not UTF-8 text ({exc.reason}); it looks binary") from exc

    lines = text.splitlines()
    start = max(1, offset) - 1
    end = start + max(1, limit)
    chunk = lines[start:end]
    if not chunk:
        return f"{path!r} has {len(lines)} lines; offset {offset} is past the end."

    body = "\n".join(f"{start + i + 1:>5}| {line}" for i, line in enumerate(chunk))
    footer = ""
    if end < len(lines):
        footer = f"\n... {len(lines) - end} more lines (call again with offset={end + 1})"
    return body + footer


def write_file(workspace: Path, path: str, content: str, append: bool = False) -> str:
    target = resolve_in_workspace(workspace, path)
    refuse_if_blocked(workspace, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding="utf-8") as handle:
        handle.write(content)
    verb = "Appended to" if append else "Wrote"
    return (
        f"{verb} {relative_to_workspace(workspace, target)} "
        f"({len(content)} chars, {content.count(chr(10)) + 1} lines)"
    )


def edit_file(workspace: Path, path: str, old_text: str, new_text: str, count: int = 1) -> str:
    target = resolve_in_workspace(workspace, path)
    refuse_if_blocked(workspace, target)
    if not target.is_file():
        raise ToolError(f"{path!r} is not a readable file")
    if not old_text:
        raise ToolError("old_text must not be empty; use write_file to create content")
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise ToolError(f"count must be an integer, got {count!r}") from None
    if count == 0 or count < -1:
        raise ToolError(f"count must be 1 or more, or -1 for every occurrence, got {count}")

    text = target.read_text(encoding="utf-8")
    occurrences = text.count(old_text)
    if occurrences == 0:
        raise ToolError(
            f"old_text was not found in {path!r}. Read the file first and copy the exact text, "
            "including indentation."
        )
    if count == 1 and occurrences > 1:
        raise ToolError(
            f"old_text appears {occurrences} times in {path!r}. Include more surrounding context "
            "to make it unique, or pass count=-1 to replace every occurrence."
        )

    replaced = (
        text.replace(old_text, new_text) if count == -1 else text.replace(old_text, new_text, count)
    )
    target.write_text(replaced, encoding="utf-8")
    n = occurrences if count == -1 else min(count, occurrences)
    return f"Replaced {n} occurrence(s) in {relative_to_workspace(workspace, target)}"


def delete_file(workspace: Path, path: str) -> str:
    target = resolve_in_workspace(workspace, path)
    refuse_if_blocked(workspace, target)
    if target == Path(workspace).resolve():
        raise ToolError("Refusing to delete the workspace root")
    if not target.exists():
        raise ToolError(f"{path!r} does not exist")
    if target.is_dir():
        try:
            target.rmdir()
        except OSError as exc:
            raise ToolError(f"Directory {path!r} is not empty: {exc}") from exc
        return f"Removed empty directory {relative_to_workspace(workspace, target)}"
    target.unlink()
    return f"Deleted {relative_to_workspace(workspace, target)}"


def search_files(
    workspace: Path,
    query: str,
    path: str = ".",
    glob: str = "*",
    regex: bool = False,
    max_results: int = 50,
) -> str:
    root = resolve_in_workspace(workspace, path)
    if not root.exists():
        raise ToolError(f"{path!r} does not exist")
    if max_results < 1:
        raise ToolError(f"max_results must be at least 1, got {max_results!r}")

    try:
        matcher = re.compile(query) if regex else re.compile(re.escape(query), re.IGNORECASE)
    except re.error as exc:
        raise ToolError(f"Invalid regular expression {query!r}: {exc}") from exc

    capped = False
    hits: list[tuple[str, int, str]] = []
    for file in _search_candidates(workspace, root, glob):
        if _is_ignored(file, workspace) or is_secret_path(file):
            continue
        try:
            if file.stat().st_size > 2_000_000:
                continue
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if matcher.search(line):
                rel = relative_to_workspace(workspace, file)
                hits.append((rel, lineno, line.strip()[:200]))
                if len(hits) >= max_results:
                    capped = True
                    break
        if capped:
            break

    if not hits:
        return f"No matches for {query!r} under {path!r}"
    lines = [f"{rel}:{lineno}: {text}" for rel, lineno, text in sorted(hits)]
    if capped:
        lines.append(f"... truncated at {max_results} matches")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Tool descriptors
# ----------------------------------------------------------------------
def build_file_tools(workspace: Path) -> list[Tool]:
    """Create the filesystem tools bound to ``workspace``."""
    return [
        Tool(
            name="list_files",
            description=(
                "List files and directories in the workspace. Use this first to understand "
                "the project layout before reading or editing anything."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory relative to the workspace root. Defaults to '.'.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob filter on the file name, e.g. '*.py'. Defaults to '*'."
                        ),
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Descend into subdirectories. Defaults to true.",
                    },
                },
                "required": [],
            },
            func=lambda path=".", pattern="*", recursive=True: list_files(
                workspace, path, pattern, recursive
            ),
        ),
        Tool(
            name="read_file",
            description=(
                "Read a UTF-8 text file from the workspace with line numbers. "
                "Large files are paginated: use offset and limit to read further."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the workspace.",
                    },
                    "offset": {"type": "integer", "description": "1-based first line to return."},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to return.",
                    },
                },
                "required": ["path"],
            },
            func=lambda path, offset=1, limit=500: read_file(workspace, path, offset, limit),
        ),
        Tool(
            name="write_file",
            description=(
                "Create a file or overwrite it completely with new content. Parent directories "
                "are created automatically. Prefer edit_file for small changes to existing files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the workspace.",
                    },
                    "content": {"type": "string", "description": "Full content to write."},
                    "append": {
                        "type": "boolean",
                        "description": "Append instead of overwriting. Defaults to false.",
                    },
                },
                "required": ["path", "content"],
            },
            func=lambda path, content, append=False: write_file(workspace, path, content, append),
        ),
        Tool(
            name="edit_file",
            description=(
                "Replace an exact snippet of text inside an existing file. The old_text must "
                "match character for character, including indentation, and must be unique "
                "unless count is -1."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the workspace.",
                    },
                    "old_text": {"type": "string", "description": "Exact text to find."},
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text (may be empty).",
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "How many occurrences to replace; -1 replaces all. Defaults to 1."
                        ),
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
            func=lambda path, old_text, new_text, count=1: edit_file(
                workspace, path, old_text, new_text, count
            ),
        ),
        Tool(
            name="delete_file",
            description="Delete a file, or an empty directory, from the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the workspace.",
                    }
                },
                "required": ["path"],
            },
            func=lambda path: delete_file(workspace, path),
            dangerous=True,
        ),
        Tool(
            name="search_files",
            description=(
                "Search file contents for a string or regular expression and return "
                "path:line: match rows. Much faster than reading files one by one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text or regex to look for."},
                    "path": {
                        "type": "string",
                        "description": "Directory to search. Defaults to '.'.",
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "Restrict to matching file names, e.g. '*.md'. Defaults to '*'."
                        ),
                    },
                    "regex": {
                        "type": "boolean",
                        "description": (
                            "Treat query as a regex. Defaults to false "
                            "(case-insensitive substring)."
                        ),
                    },
                    "max_results": {"type": "integer", "description": "Cap on returned matches."},
                },
                "required": ["query"],
            },
            func=lambda query, path=".", glob="*", regex=False, max_results=50: search_files(
                workspace, query, path, glob, regex, max_results
            ),
        ),
    ]
