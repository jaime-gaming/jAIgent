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
