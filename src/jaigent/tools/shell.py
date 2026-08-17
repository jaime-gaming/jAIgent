"""Opt-in shell tool.

Running arbitrary commands is the single most dangerous capability an agent can
have, so it is **disabled by default**. Enable it explicitly with
``--allow-shell`` or ``JAIGENT_ALLOW_SHELL=1``, and only in a directory you are
happy to see modified.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from jaigent.errors import ToolError
from jaigent.tools.base import Tool

DEFAULT_TIMEOUT = 60
MAX_OUTPUT_CHARS = 10_000

#: Substrings that are refused outright, even with the shell tool enabled.
BLOCKED_PATTERNS = (
    "rm -rf /",
    "mkfs",
    ":(){:|:&};:",
    "dd if=/dev/zero",
    "> /dev/sda",
    "shutdown",
    "reboot",
)


def run_command(workspace: Path, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run ``command`` with the workspace as the working directory."""
    command = (command or "").strip()
    if not command:
        raise ToolError("command must not be empty")

    lowered = command.lower()
    for blocked in BLOCKED_PATTERNS:
        if blocked in lowered:
            raise ToolError(f"Refusing to run a command containing {blocked!r}")

    timeout = max(1, min(int(timeout), 300))
    try:
        completed = subprocess.run(  # noqa: S602 - intentional, gated behind allow_shell
            command,
            shell=True,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"Command timed out after {timeout}s: {command}") from exc
    except OSError as exc:
        raise ToolError(f"Could not run command: {exc}") from exc

    parts = [f"$ {command}", f"exit code: {completed.returncode}"]
    for label, stream in (("stdout", completed.stdout), ("stderr", completed.stderr)):
        text = (stream or "").strip()
        if not text:
            continue
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
        parts.append(f"--- {label} ---\n{text}")
    return "\n".join(parts)


def build_shell_tools(workspace: Path) -> list[Tool]:
    """Create the shell tool bound to ``workspace``. Only call this when allowed."""
    return [
        Tool(
            name="run_command",
            description=(
                "Run a shell command inside the workspace directory and return its exit code, "
                "stdout and stderr. Use it for builds, tests, git status and other read-mostly "
                "inspection. Never run destructive commands; prefer the dedicated file tools "
                "for editing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command line to execute, e.g. 'pytest -q'.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Seconds before the command is killed, 1-300. Defaults to 60."
                        ),
                    },
                },
                "required": ["command"],
            },
            func=lambda command, timeout=DEFAULT_TIMEOUT: run_command(workspace, command, timeout),
            dangerous=True,
        )
    ]


def quote(value: str) -> str:
    """Shell-quote ``value`` (re-exported convenience for tool authors)."""
    return shlex.quote(value)
