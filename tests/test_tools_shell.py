"""The opt-in shell tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent.errors import ToolError
from jaigent.tools.shell import build_shell_tools, run_command


def test_runs_and_reports_stdout(workspace: Path) -> None:
    out = run_command(workspace, "echo hello")
    assert "hello" in out
    assert "exit code: 0" in out


def test_runs_inside_the_workspace(workspace: Path) -> None:
    out = run_command(workspace, "pwd")
    assert str(workspace) in out


def test_captures_stderr_and_exit_code(workspace: Path) -> None:
    out = run_command(workspace, "echo oops >&2; exit 3")
    assert "exit code: 3" in out
    assert "oops" in out


def test_timeout(workspace: Path) -> None:
    with pytest.raises(ToolError, match="timed out"):
        run_command(workspace, "sleep 5", timeout=1)


def test_empty_command(workspace: Path) -> None:
    with pytest.raises(ToolError, match="must not be empty"):
        run_command(workspace, "  ")


@pytest.mark.parametrize(
    "evil", ["rm -rf / --no-preserve-root", "sudo shutdown now", "mkfs.ext4 /dev/sda"]
)
def test_blocked_patterns(workspace: Path, evil: str) -> None:
    with pytest.raises(ToolError, match="Refusing"):
        run_command(workspace, evil)


def test_long_output_is_truncated(workspace: Path) -> None:
    out = run_command(workspace, "python3 -c \"print('x' * 50000)\"")
    assert "truncated" in out


def test_tool_is_marked_dangerous(workspace: Path) -> None:
    tool = build_shell_tools(workspace)[0]
    assert tool.name == "run_command"
    assert tool.dangerous is True
