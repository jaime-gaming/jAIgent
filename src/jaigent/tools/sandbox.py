"""Workspace sandboxing helpers.

Every filesystem tool routes its paths through :func:`resolve_in_workspace`, so
a confused (or adversarial) model cannot read ``~/.ssh/id_rsa`` or write outside
the directory the user pointed the agent at.
"""

from __future__ import annotations

from pathlib import Path

from jaigent.errors import SandboxViolation

#: Files above this size are refused outright by the read tools.
MAX_READ_BYTES = 1_000_000


def resolve_in_workspace(workspace: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` relative to ``workspace`` and assert containment.

    Symlinks are resolved *before* the check, so a symlink pointing outside the
    workspace is rejected as well.

    Raises:
        SandboxViolation: if the resolved path escapes the workspace.
    """
    root = Path(workspace).expanduser().resolve()
    raw = Path(candidate).expanduser()
    target = (raw if raw.is_absolute() else root / raw).resolve()

    if target != root and root not in target.parents:
        raise SandboxViolation(
            f"Path {candidate!r} resolves to {target} which is outside the workspace {root}. "
            "Only paths inside the workspace can be accessed."
        )
    return target


def relative_to_workspace(workspace: Path, path: Path) -> str:
    """Render ``path`` relative to the workspace for user-facing messages."""
    try:
        return str(path.relative_to(Path(workspace).resolve()))
    except ValueError:  # pragma: no cover - defensive
        return str(path)


def ensure_size_ok(path: Path, limit: int = MAX_READ_BYTES) -> None:
    """Raise if ``path`` is larger than ``limit`` bytes."""
    size = path.stat().st_size
    if size > limit:
        raise SandboxViolation(
            f"{path.name} is {size:,} bytes, above the {limit:,} byte read limit. "
            "Read a slice of it instead (use the offset/limit arguments)."
        )
