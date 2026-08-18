"""The opt-in shell tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent.errors import ToolError
from jaigent.tools.shell import IS_WINDOWS, SHELL_NAME, build_shell_tools, run_command


def test_runs_and_reports_stdout(workspace: Path) -> None:
    out = run_command(workspace, "echo hello")
    assert "hello" in out
    assert "exit code: 0" in out


def test_runs_inside_the_workspace(workspace: Path) -> None:
    # cmd.exe has no `pwd`, and a POSIX shell on Windows reports an msys path.
    out = run_command(workspace, "cd" if IS_WINDOWS else "pwd")

    assert workspace.name in out


def test_captures_stderr_and_exit_code(workspace: Path) -> None:
    # `;` separates commands in sh; cmd.exe uses `&`.
    command = "echo oops 1>&2 & exit 3" if IS_WINDOWS else "echo oops >&2; exit 3"
    out = run_command(workspace, command)

    assert "exit code: 3" in out
    assert "oops" in out


def test_timeout(workspace: Path) -> None:
    # cmd.exe's `timeout` refuses to run when stdin is redirected, which it is
    # here, and exits immediately. `ping` is the durable Windows sleep.
    command = "ping -n 10 127.0.0.1 > NUL" if IS_WINDOWS else "sleep 5"
    with pytest.raises(ToolError, match="timed out"):
        run_command(workspace, command, timeout=1)


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


class TestTheModelIsToldWhichShell:
    """A model that does not know the shell writes POSIX and gets cmd.exe."""

    def test_the_description_names_the_shell(self, workspace: Path) -> None:
        description = build_shell_tools(workspace)[0].description

        assert SHELL_NAME in description

    def test_the_shell_name_is_platform_appropriate(self) -> None:
        assert ("cmd.exe" if IS_WINDOWS else "/bin/sh") == SHELL_NAME


class TestWindowsDestructiveCommandsAreBlocked:
    """The blocklist was written for POSIX, so on Windows it guarded nothing."""

    @pytest.mark.parametrize(
        "command",
        [
            "format c:",
            "format C: /fs:ntfs",
            "del /s /q C:\\",
            "rd /s /q C:\\",
            "rmdir /s /q c:\\",
            "diskpart",
            "vssadmin delete shadows /all",
            "reg delete HKLM\\SYSTEM /f",
            "takeown /f C:\\ /r",
            "cipher /w:C:",
        ],
    )
    def test_it_is_refused(self, workspace: Path, command: str) -> None:
        with pytest.raises(ToolError, match="Refusing to run"):
            run_command(workspace, command)

    @pytest.mark.parametrize(
        "command",
        [
            "del build\\temp.txt",
            "rd build",
            "reg query HKLM\\SOFTWARE",
            "echo format c: is a dangerous command",
        ],
    )
    def test_harmless_lookalikes_still_run(self, workspace: Path, command: str) -> None:
        # An over-eager blocklist that refuses ordinary work is its own bug.
        run_command(workspace, command)
