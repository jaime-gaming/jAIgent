"""Opt-in shell tool.

Running arbitrary commands is the single most dangerous capability an agent can
have, so it is **disabled by default**. Enable it explicitly with
``--allow-shell`` or ``JAIGENT_ALLOW_SHELL=1``, and only in a directory you are
happy to see modified.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from jaigent.errors import ToolError
from jaigent.tools.base import Tool

DEFAULT_TIMEOUT = 60
MAX_OUTPUT_CHARS = 10_000

#: Regexes refused outright, even with the shell tool enabled.
#:
#: This is an accident guard, not a security boundary — a determined adversary
#: with shell access has already won. It exists to stop a confused model from
#: doing something catastrophic and irreversible. Patterns are matched against a
#: normalised form of the command so trivial spacing tricks do not slip past.
BLOCKED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-z]*\s+)*-?[a-z]*[rf][a-z]*\s+/(\s|$)", "recursive delete of /"),
    (r"\brm\s+(-[a-z]*\s+)*~(/\s*)?(\s|$)", "delete of your home directory"),
    (r"\bmkfs(\.[a-z0-9]+)?\b", "filesystem format"),
    (r":\(\)\s*\{.*\|.*&.*\}\s*;?\s*:", "fork bomb"),
    (r"\bdd\b.*\bof=/dev/(sd|nvme|hd|disk)", "raw write to a disk device"),
    (r">\s*/dev/(sd|nvme|hd|disk)", "raw write to a disk device"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shutting the machine down"),
    (r"\bchmod\s+(-[a-z]+\s+)*777\s+/(\s|$)", "world-writable root"),
    (r"\bchown\s+.*\s+/(\s|$)", "changing ownership of /"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|)sh\b", "piping a download into a shell"),
    (r"\bhistory\s+-c\b", "clearing shell history"),
    (r"\bgit\s+push\b.*--force\b", "force push"),
    (r"\bgit\s+reset\s+--hard\b.*\borigin\b", "discarding local work"),
    (r"/etc/(passwd|shadow|sudoers)", "touching system credential files"),
    (r"\b(ssh|gpg)\s+.*(id_rsa|id_ed25519|\.gnupg)", "reading private keys"),
    (r"~/\.ssh\b", "the SSH directory"),
    (r"\bsudo\b", "sudo"),
)

#: Collapse whitespace so `rm  -rf   /` matches the same rule as `rm -rf /`.
_WHITESPACE_RE = re.compile(r"\s+")


def run_command(workspace: Path, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run ``command`` with the workspace as the working directory."""
    command = (command or "").strip()
    if not command:
        raise ToolError("command must not be empty")

    normalised = _WHITESPACE_RE.sub(" ", command.lower())
    for pattern, description in BLOCKED_PATTERNS:
        if re.search(pattern, normalised):
            raise ToolError(
                f"Refusing to run this command: it looks like {description}. "
                "If you genuinely need to, run it yourself outside jaigent."
            )

    timeout = max(1, min(int(timeout), 300))
    try:
        # ruff: S602 / bandit: B602 -- shell=True is the entire point of this
        # tool. It is absent from the toolset unless the user passes --allow-shell,
        # every command is screened by BLOCKED_PATTERNS, it is time-limited, and it
        # runs with the workspace as its working directory. Documented in SECURITY.md.
        completed = subprocess.run(  # noqa: S602  # nosec B602
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
