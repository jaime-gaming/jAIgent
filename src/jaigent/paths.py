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
