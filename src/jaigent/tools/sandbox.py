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

#: Filenames the model must never read or write. These live *inside* the
#: workspace (``.env`` next to the project, a copied private key) so the
#: sandbox containment check alone would allow them — and then the contents
#: would be sent to the LLM provider.
SECRET_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.staging",
        "keys.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "service-account.json",
    }
)

#: Templates are meant to be read; they hold placeholders, not live secrets.
SECRET_ALLOW = frozenset({".env.example", ".env.sample", ".env.template"})

SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks"})


def is_secret_path(path: Path) -> bool:
    """Whether ``path`` looks like a credential file that tools must refuse."""
    name = Path(path).name.lower()
    if name in SECRET_ALLOW:
        return False
    if name in SECRET_NAMES or name.startswith(".env."):
        return True
    if any(name.endswith(suffix) for suffix in SECRET_SUFFIXES):
        return True
    return name.startswith("id_") and not name.endswith(".pub")


def refuse_if_secret(path: Path) -> None:
    """Raise if ``path`` is a credential file the model must not touch."""
    if is_secret_path(path):
        raise SandboxViolation(
            f"Refusing to access {path.name!r}: it looks like a secret file. "
            "jaigent will not send credentials to the model. Use the environment "
            "or a git-ignored file the agent cannot read."
        )


def refuse_if_blocked(workspace: Path, path: Path) -> None:
    """Refuse secrets and anything under ``.git``."""
    refuse_if_secret(path)
    try:
        parts = Path(path).resolve().relative_to(Path(workspace).resolve()).parts
    except ValueError:
        return
    if ".git" in parts:
        raise SandboxViolation(
            "Refusing to access the .git directory. Use git yourself, not the file tools."
        )


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
    """Render ``path`` relative to the workspace for user-facing messages.

    Always with forward slashes. ``str(Path)`` uses the native separator, which
    on Windows would show the model ``src\\app.py`` while every path the model
    writes uses ``/`` — and these strings are also stored as keys in the
    checkpoint index, where a change of separator would orphan the entry.
    """
    try:
        return path.relative_to(Path(workspace).resolve()).as_posix()
    except ValueError:  # pragma: no cover - defensive
        return path.as_posix()


def ensure_size_ok(path: Path, limit: int = MAX_READ_BYTES) -> None:
    """Raise if ``path`` is larger than ``limit`` bytes."""
    size = path.stat().st_size
    if size > limit:
        raise SandboxViolation(
            f"{path.name} is {size:,} bytes, above the {limit:,} byte read limit. "
            "Read a slice of it instead (use the offset/limit arguments)."
        )
