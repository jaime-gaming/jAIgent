"""Optional project memory."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent.config import Settings
from jaigent.errors import ToolError
from jaigent.memory import append_memory, build_memory_tools, load_memory, memory_path
from jaigent.tools import build_default_registry


def test_memory_tools_absent_by_default(tmp_path: Path) -> None:
    registry = build_default_registry(Settings(api_key="k", workspace=tmp_path))
    assert "remember" not in registry
    assert "recall" not in registry


def test_memory_tools_appear_when_enabled(tmp_path: Path) -> None:
    registry = build_default_registry(Settings(api_key="k", workspace=tmp_path, memory=True))
    assert "remember" in registry
    assert "recall" in registry


def test_round_trip(tmp_path: Path) -> None:
    append_memory(tmp_path, "Prefer pytest over unittest.")
    assert "pytest" in load_memory(tmp_path)
    assert memory_path(tmp_path).is_file()


def test_empty_note_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="empty"):
        append_memory(tmp_path, "   ")


def test_tools_write_and_read(tmp_path: Path) -> None:
    remember, recall = build_memory_tools(tmp_path)
    remember(note="The package is named jaigent.")
    assert "jaigent" in recall()


def test_symlinked_memory_file_is_not_read_or_overwritten(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("OPENAI_API_KEY=private", encoding="utf-8")
    directory = tmp_path / ".jaigent"
    directory.mkdir()
    path = directory / "memory.md"
    try:
        path.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    assert load_memory(tmp_path) == ""
    with pytest.raises(ToolError, match="symlink"):
        append_memory(tmp_path, "do not overwrite the secret")
    assert secret.read_text(encoding="utf-8") == "OPENAI_API_KEY=private"


def test_symlinked_memory_directory_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "memory.md").write_text("private content", encoding="utf-8")
    link = tmp_path / ".jaigent"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    assert load_memory(tmp_path) == ""
    with pytest.raises(ToolError, match="symlink"):
        append_memory(tmp_path, "do not write outside")
