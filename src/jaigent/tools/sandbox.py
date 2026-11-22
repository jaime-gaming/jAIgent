"""Workspace sandboxing helpers.

Every filesystem tool routes its paths through :func:`resolve_in_workspace`, so
a confused (or adversarial) model cannot read ``~/.ssh/id_rsa`` or write outside
the directory the user pointed the agent at.
"""

from __future__ import annotations

import os
from pathlib import Path

from jaigent.errors import SandboxViolation
from jaigent.paths import PROJECT_DIR

#: Files above this size are refused outright by the read tools.
MAX_READ_BYTES = 1_000_000

#: Filenames the model must never read or write. These live *inside* the
#: workspace (``.env`` next to the project, a copied private key) so the
#: sandbox containment check alone would allow them, and then the contents
#: would be sent to the LLM provider.
SECRET_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.staging",
        ".envrc",
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

#: Private-key filename stems. Matched as prefixes so `id_rsa_work` is caught,
#: but a bare `id_` prefix used to also block innocent files like `id_list.csv`.
_KEY_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")

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
    # ``id_rsa_work`` is a secret; ``id_list.csv`` is not, only real prefixes
    # like ``id_rsa``, ``id_ed25519`` with optional suffix count.
    return name.startswith(_KEY_PREFIXES) and not name.endswith(".pub")


def refuse_if_secret(path: Path) -> None:
    """Raise if ``path`` is a credential file the model must not touch."""
    if is_secret_path(path):
        raise SandboxViolation(
            f"Refusing to access {path.name!r}: it looks like a secret file. "
            "jAIgent will not send credentials to the model. Use the environment "
            "or a git-ignored file the agent cannot read."
        )


#: Directories inside the workspace whose *contents are code that runs*. The
#: file tools may not write there, because a prompt injection that can write a
#: plugin file gets code execution while ``--allow-shell`` is off, and the
#: sandbox plus the shell opt-in are the two things this product promises.
#: Reading is still allowed: a user may reasonably ask "what do my plugins do?".
EXECUTABLE_DIRS = frozenset({"plugins"})

#: Case-insensitive comparisons, because these paths are load-bearing on a
#: case-insensitive filesystem, which macOS and Windows are by default. A
#: ``.GIT/hooks/pre-commit`` written on a Mac *is* ``.git/hooks/pre-commit``,
#: so ``".git" in parts`` let a model plant a hook that runs on the user's next
#: commit. Comparing on the lowercased name closes it everywhere.
_PROTECTED_NAMES = frozenset({".git"})


def _fold(name: str) -> str:
    """Casefold a path component the way a case-insensitive filesystem would."""
    return name.casefold()


def refuse_if_blocked(workspace: Path, path: Path) -> None:
    """Refuse secrets, ``.git`` (any casing) and locations that are code.

    Two distinct protections live here:

    * ``.git`` in any casing, because writing a hook is running code later.
    * ``.jaigent/plugins`` for writes, because a plugin file is imported and
      executed by the next run. Prompt injection was an accepted risk; using
      it to escape the shell opt-in was not.
    """
    refuse_if_secret(path)
    try:
        parts = Path(path).resolve().relative_to(Path(workspace).resolve()).parts
    except ValueError:
        return
    folded = [_fold(part) for part in parts]
    for protected in _PROTECTED_NAMES:
        if _fold(protected) in folded:
            # Name the component the caller actually wrote, so a `.GIT` attempt
            # is told about `.GIT` rather than about a directory it never typed.
            actual = parts[folded.index(_fold(protected))]
            raise SandboxViolation(
                f"Refusing to access {actual!r}: version-control internals can run code "
                "(hooks) and must be handled with git itself, not the file tools."
            )


def refuse_if_writable(workspace: Path, path: Path) -> None:
    """Refuse a path the *file tools* may read but must never write.

    Call this from every mutating tool, next to :func:`refuse_if_blocked`.
    """
    refuse_if_blocked(workspace, path)
    try:
        parts = Path(path).resolve().relative_to(Path(workspace).resolve()).parts
    except ValueError:
        return
    folded = [_fold(part) for part in parts]
    if _fold(PROJECT_DIR) in folded:
        after = folded[folded.index(_fold(PROJECT_DIR)) + 1 :]
        if after and after[0] in {_fold(name) for name in EXECUTABLE_DIRS}:
            actual = parts[folded.index(_fold(PROJECT_DIR)) + 1]
            raise SandboxViolation(
                f"Refusing to write into {actual!r}: files in .jaigent/{actual} are "
                "imported and executed by the next run, so an agent-written file there "
                "would be a way around the shell restriction. Create plugins yourself, "
                "or run with --allow-shell if you trust the task."
            )


def resolve_in_workspace(workspace: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` relative to ``workspace`` and assert containment.

    Symlinks are resolved *before* the check, so a symlink pointing outside the
    workspace is rejected as well.

    Raises:
        SandboxViolation: if the resolved path escapes the workspace.
    """
    root = Path(workspace).expanduser().resolve()
    # An empty path is the workspace itself, not an error, but it must be
    # explicit. Some callers pass "" when no file is named; treat it as root
    # so the block checks still apply.
    if candidate == "" or candidate is None:
        return root
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
    writes uses ``/``, and these strings are also stored as keys in the
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


def line_ending_of(path: Path, limit: int = MAX_READ_BYTES) -> str:
    """The line ending a file already uses, so writing to it does not change it.

    Text handed to a writer arrives with ``\n`` between lines: a model writes
    ``\n`` because that is what JSON carries, and a browser's text box gives
    back ``\n`` whatever the file had. Writing that with :meth:`Path.write_text`
    re-ends every line to :data:`os.linesep`: on Windows an LF file comes back
    CRLF, and on Linux every CRLF line of a Windows file comes back LF, which in
    a repository whose ``.gitattributes`` asks for LF turns a one-line edit into
    a whole-file change. So the ending is read off the file being written rather
    than off the platform writing it.

    Only the first ``limit`` bytes are sampled: an ending is a convention, and a
    megabyte of a file states it as well as the whole of it.

    A file with no line breaks, one that mixes the two, and one that cannot be
    read all get the platform's own ending, which is what they got before and is
    the least surprising answer for a file with no convention to keep.
    """
    try:
        with path.open("rb") as handle:
            sample = handle.read(limit)
    except OSError:
        return os.linesep
    crlf = sample.count(b"\r\n")
    bare = sample.count(b"\n") - crlf
    if crlf and not bare:
        return "\r\n"
    if bare and not crlf:
        return "\n"
    return os.linesep


def normalise_endings(text: str, ending: str) -> str:
    """Rewrite every line break in ``text`` as ``ending``.

    Collapses ``\r\n`` first so that re-ending text that already carries one
    convention cannot produce ``\r\r\n``. A lone ``\r`` is left alone: it is
    not a line break this program understands, and guessing at it would change
    bytes nobody asked about.
    """
    return text.replace("\r\n", "\n").replace("\n", ending)


def write_text_keeping_endings(path: Path, text: str, *, ending: str | None = None) -> str:
    """Write ``text`` to ``path`` as UTF-8 bytes, keeping the file's own ending.

    Returns the ending that was written, so a caller can say what it did. A new
    file, which has no convention to keep, gets the platform's own.

    Bytes rather than text mode because text mode translates on the way out, and
    translating is exactly what this exists to avoid.
    """
    if ending is None:
        ending = line_ending_of(path) if path.exists() else os.linesep
    path.write_bytes(normalise_endings(text, ending).encode("utf-8"))
    return ending
