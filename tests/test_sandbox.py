"""The workspace boundary must hold against traversal, absolute paths and symlinks."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent.errors import SandboxViolation
from jaigent.tools.sandbox import ensure_size_ok, relative_to_workspace, resolve_in_workspace


def test_relative_path_resolves_inside(workspace: Path) -> None:
    assert resolve_in_workspace(workspace, "notes.md") == workspace / "notes.md"
    assert resolve_in_workspace(workspace, "src/app.py") == workspace / "src" / "app.py"


def test_workspace_root_itself_is_allowed(workspace: Path) -> None:
    assert resolve_in_workspace(workspace, ".") == workspace


def test_nonexistent_paths_are_allowed_for_writing(workspace: Path) -> None:
    assert resolve_in_workspace(workspace, "new/deep/file.txt").name == "file.txt"


@pytest.mark.parametrize(
    "escape",
    ["../secrets.txt", "../../etc/passwd", "src/../../outside.txt", "/etc/passwd", "~/.ssh/id_rsa"],
)
def test_traversal_is_rejected(workspace: Path, escape: str) -> None:
    with pytest.raises(SandboxViolation):
        resolve_in_workspace(workspace, escape)


def test_symlink_pointing_outside_is_rejected(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_target.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "escape_link"
    link.symlink_to(outside)

    with pytest.raises(SandboxViolation):
        resolve_in_workspace(workspace, "escape_link")


def test_size_limit(workspace: Path) -> None:
    big = workspace / "big.bin"
    big.write_text("x" * 100, encoding="utf-8")
    ensure_size_ok(big, limit=1000)
    with pytest.raises(SandboxViolation, match="read limit"):
        ensure_size_ok(big, limit=10)


def test_relative_to_workspace(workspace: Path) -> None:
    assert relative_to_workspace(workspace, workspace / "src" / "app.py") == "src/app.py"


class TestPathsAreRenderedPosixStyle:
    """Relative paths shown to the model must always use forward slashes.

    ``str(Path)`` yields the native separator, so on Windows the model saw
    ``src\\app.py`` while every path *it* writes uses ``/``. Mixing the two in
    one conversation invites the model to guess, and the strings end up in the
    checkpoint index as well, where the separator becomes part of the key.

    On POSIX these assertions are trivially true; the Windows CI job is what
    makes them bite.
    """

    def test_a_nested_path_uses_forward_slashes(self, workspace: Path) -> None:
        rendered = relative_to_workspace(workspace, workspace / "src" / "deep" / "app.py")

        assert rendered == "src/deep/app.py"
        assert "\\" not in rendered

    def test_a_top_level_file_is_unchanged(self, workspace: Path) -> None:
        assert relative_to_workspace(workspace, workspace / "notes.md") == "notes.md"

    def test_the_workspace_itself_renders_as_dot(self, workspace: Path) -> None:
        assert relative_to_workspace(workspace, workspace) == "."
