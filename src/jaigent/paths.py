"""Where jaigent keeps its files, on every platform.

One function decides the base directory so the answer cannot drift between the
settings store, the skill loader, the scheduler and the key store.

Resolution order:

1. ``JAIGENT_HOME`` — always wins, useful for tests and portable installs.
2. ``%APPDATA%\\jaigent`` on Windows, which is where Windows applications are
   expected to store per-user data.
3. ``$XDG_CONFIG_HOME/jaigent`` when set, honouring the XDG base directory spec.
4. ``~/.jaigent`` everywhere else.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

APP_DIRNAME = "jaigent"
DOT_DIRNAME = ".jaigent"

#: Directory name used for per-project configuration, on all platforms.
PROJECT_DIR = DOT_DIRNAME


def is_windows() -> bool:
    """Whether we are running on a Windows console."""
    return sys.platform.startswith("win")


def user_home() -> Path:
    """The per-user jaigent directory for this platform."""
    override = os.getenv("JAIGENT_HOME")
    if override:
        return Path(override).expanduser()

    if is_windows():
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / APP_DIRNAME
        return Path.home() / APP_DIRNAME

    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_DIRNAME

    return Path.home() / DOT_DIRNAME


def project_home(start: Path | None = None) -> Path:
    """The per-project ``.jaigent`` directory for ``start`` (default: cwd)."""
    return Path(start or Path.cwd()) / PROJECT_DIR


def user_file(*parts: str) -> Path:
    """A path inside the user directory, e.g. ``user_file("settings.json")``."""
    return user_home().joinpath(*parts)


def scoped_dirs(name: str, start: Path | None = None) -> list[tuple[str, Path]]:
    """``[("user", ...), ("project", ...)]`` for a named subdirectory.

    Returned lowest-priority first, which is the order discovery walks so that
    project definitions shadow personal ones.
    """
    return [("user", user_home() / name), ("project", project_home(start) / name)]


def write_private(path: Path, content: str) -> Path:
    """Write ``content`` to ``path`` so only the owner can read it.

    Anything holding a credential goes through this. The permissions are set
    on the temporary file *before* the secret is written to it, so there is no
    window in which the plaintext is on disk world-readable. The final rename
    is atomic, so a reader never sees a half-written file.

    Windows has no POSIX mode bits; ``chmod`` there is a no-op beyond the
    read-only flag, and NTFS inheritance governs access instead. That is a
    documented limitation rather than something this function can fix.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")

    # Create with owner-only permissions from the very first byte.
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise

    with contextlib.suppress(OSError):  # unsupported on some filesystems
        temp.chmod(0o600)
    temp.replace(path)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path
