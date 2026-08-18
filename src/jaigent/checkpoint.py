"""Checkpoints: undo anything the agent did to your files.

Every mutating tool call snapshots the files it is about to touch *before* it
touches them. If a run goes wrong — a bad edit, a deleted file, a refactor that
made things worse — you rewind:

    jaigent undo                 # revert the last tool call
    jaigent checkpoints          # see the timeline
    jaigent rewind <id>          # go back to any point

This is the safety net that makes an autonomous agent comfortable to run. Other
agents give you a diff to approve; jaigent also lets you change your mind after
the fact, which matters because the damage from an agent is rarely visible until
several steps later.

Snapshots are content-addressed: a file that does not change between checkpoints
is stored once. Storage lives under ``.jaigent/checkpoints`` in the workspace, so
it travels with the project and can be git-ignored.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jaigent.errors import JaigentError
from jaigent.paths import project_home

CHECKPOINT_DIRNAME = "checkpoints"
OBJECTS_DIRNAME = "objects"
INDEX_FILE = "index.json"
CHECKPOINT_VERSION = 1

#: Keep this many checkpoints; older ones are pruned with their unique objects.
MAX_CHECKPOINTS = 100

#: Files above this size are noted but not snapshotted, to keep the store small.
MAX_SNAPSHOT_BYTES = 5_000_000


def checkpoint_dir(workspace: Path) -> Path:
    """Where checkpoints for ``workspace`` are kept."""
    return project_home(workspace) / CHECKPOINT_DIRNAME


class AmbiguousCheckpoint(JaigentError):
    """A checkpoint id prefix matched more than one checkpoint."""

    def __init__(self, prefix: str, matches: list[str]) -> None:
        self.prefix = prefix
        self.matches = matches
        shown = ", ".join(matches[:5])
        more = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
        super().__init__(
            f"{prefix!r} matches {len(matches)} checkpoints: {shown}{more}. "
            "Use more characters of the id."
        )


@dataclass(slots=True)
class FileState:
    """One file's condition at checkpoint time."""

    path: str
    #: SHA-256 of the contents, or ``None`` when the file did not exist.
    digest: str | None = None
    size: int = 0
    #: True when the file was too large to snapshot; it cannot be restored.
    skipped: bool = False

    @property
    def existed(self) -> bool:
        return self.digest is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "digest": self.digest,
            "size": self.size,
            "skipped": self.skipped,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileState:
        if not isinstance(data, dict):
            raise TypeError("file state must be an object")
        return cls(
            path=str(data.get("path", "")),
            digest=data.get("digest"),
            size=int(data.get("size", 0)),
            skipped=bool(data.get("skipped", False)),
        )


@dataclass(slots=True)
class Checkpoint:
    """A restorable point in the workspace's history."""

    id: str
    label: str
    tool: str = ""
    created: float = field(default_factory=time.time)
    files: list[FileState] = field(default_factory=list)

    @property
    def restorable(self) -> list[FileState]:
        return [state for state in self.files if not state.skipped]

    def age(self) -> str:
        seconds = max(0.0, time.time() - self.created)
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)}m ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h ago"
        return f"{int(seconds // 86400)}d ago"

    def summary(self) -> str:
        names = [Path(state.path).name for state in self.files[:3]]
        rest = f" +{len(self.files) - 3}" if len(self.files) > 3 else ""
        return ", ".join(names) + rest if names else "(no files)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "tool": self.tool,
            "created": self.created,
            "files": [state.to_dict() for state in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        return cls(
            id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            tool=str(data.get("tool", "")),
            created=float(data.get("created", 0.0)),
            files=[FileState.from_dict(f) for f in data.get("files", [])],
        )


class CheckpointStore:
    """Content-addressed snapshots for one workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = checkpoint_dir(self.workspace)
        self.objects = self.root / OBJECTS_DIRNAME
        self.index = self.root / INDEX_FILE

    # ------------------------------------------------------------------
    def _load(self) -> list[Checkpoint]:
        if not self.index.is_file():
            return []
        try:
            data = json.loads(self.index.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        raw = data.get("checkpoints", []) if isinstance(data, dict) else []
        result: list[Checkpoint] = []
        for item in raw:
            if not isinstance(item, dict):
                continue  # a hand-edited or truncated index should not be fatal
            try:
                result.append(Checkpoint.from_dict(item))
            except (AttributeError, TypeError, ValueError):
                continue
        return result

    def _save(self, checkpoints: list[Checkpoint]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CHECKPOINT_VERSION,
            "checkpoints": [c.to_dict() for c in checkpoints],
        }
        temp = self.index.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(self.index)

    def _store_object(self, data: bytes) -> str:
        """Write ``data`` into the object store and return its digest."""
        digest = hashlib.sha256(data).hexdigest()
        self.objects.mkdir(parents=True, exist_ok=True)
        target = self.objects / digest
        if not target.exists():  # identical content is stored once
            temp = target.with_suffix(".tmp")
            temp.write_bytes(data)
            temp.replace(target)
        return digest

    # ------------------------------------------------------------------
    def history(self, limit: int = 50) -> list[Checkpoint]:
        """Checkpoints, newest first."""
        return sorted(self._load(), key=lambda c: c.created, reverse=True)[:limit]

    def get(self, identifier: str) -> Checkpoint | None:
        """Find a checkpoint by exact id, then by unique prefix.

        Raises:
            AmbiguousCheckpoint: if the prefix matches more than one. Restoring
                is destructive, so guessing which one was meant is not safe.
        """
        checkpoints = self._load()
        for checkpoint in checkpoints:
            if checkpoint.id == identifier:
                return checkpoint

        matches = [c for c in checkpoints if c.id.startswith(identifier)]
        if len(matches) > 1:
            raise AmbiguousCheckpoint(identifier, [c.id for c in matches])
        return matches[0] if matches else None

    def latest(self) -> Checkpoint | None:
        found = self.history(limit=1)
        return found[0] if found else None

    def capture(self, paths: list[Path], *, label: str, tool: str = "") -> Checkpoint | None:
        """Snapshot ``paths`` as they are right now.

        Returns ``None`` when there is nothing worth recording, so a read-only
        tool call does not clutter the timeline.
        """
        states: list[FileState] = []
        for path in paths:
            resolved = Path(path)
            if not resolved.is_absolute():
                resolved = self.workspace / resolved
            try:
                # as_posix, not str: the separator becomes part of the index key.
                relative = resolved.resolve().relative_to(self.workspace).as_posix()
            except ValueError:
                continue  # outside the workspace; the sandbox will reject it anyway

            if not resolved.is_file():
                states.append(FileState(path=relative))  # records "did not exist"
                continue

            size = resolved.stat().st_size
            if size > MAX_SNAPSHOT_BYTES:
                states.append(FileState(path=relative, size=size, skipped=True))
                continue

            try:
                data = resolved.read_bytes()
            except OSError:
                continue
            states.append(FileState(path=relative, digest=self._store_object(data), size=size))

        if not states:
            return None

        checkpoints = self._load()
        checkpoint = Checkpoint(id=self._next_id(checkpoints), label=label, tool=tool, files=states)
        checkpoints.append(checkpoint)
        self._save(self._prune(checkpoints))
        return checkpoint

    def _next_id(self, existing: list[Checkpoint]) -> str:
        """A short, unique, time-ordered id.

        Two tool calls inside the same millisecond must not collide, or `undo`
        would rewind the wrong step.
        """
        taken = {checkpoint.id for checkpoint in existing}
        stamp = f"{int(time.time() * 1000):x}"[-8:]
        candidate = stamp
        suffix = 1
        while candidate in taken:
            candidate = f"{stamp}-{suffix}"
            suffix += 1
        return candidate

    def restore(self, checkpoint: Checkpoint) -> list[str]:
        """Put the workspace back to how ``checkpoint`` found it.

        Returns the paths that changed. Files that did not exist at checkpoint
        time are deleted again; files that did are rewritten byte for byte.
        """
        changed: list[str] = []
        for state in checkpoint.files:
            if state.skipped:
                continue
            target = self.workspace / state.path

            if not state.existed:
                if target.is_file():
                    target.unlink()
                    changed.append(state.path)
                continue

            source = self.objects / str(state.digest)
            if not source.is_file():
                continue  # object was pruned; nothing we can do
            current = target.read_bytes() if target.is_file() else None
            data = source.read_bytes()
            if current == data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            changed.append(state.path)
        return changed

    def diff_summary(self, checkpoint: Checkpoint) -> list[tuple[str, str]]:
        """What restoring would do, as ``(path, action)`` pairs."""
        rows: list[tuple[str, str]] = []
        for state in checkpoint.files:
            target = self.workspace / state.path
            if state.skipped:
                rows.append((state.path, "skipped (too large)"))
            elif not state.existed:
                rows.append((state.path, "delete" if target.is_file() else "unchanged"))
            elif not target.is_file():
                rows.append((state.path, "recreate"))
            else:
                source = self.objects / str(state.digest)
                same = source.is_file() and target.read_bytes() == source.read_bytes()
                rows.append((state.path, "unchanged" if same else "revert"))
        return rows

    # ------------------------------------------------------------------
    def _prune(self, checkpoints: list[Checkpoint]) -> list[Checkpoint]:
        """Drop the oldest checkpoints and any objects nothing references."""
        if len(checkpoints) <= MAX_CHECKPOINTS:
            return checkpoints

        checkpoints = sorted(checkpoints, key=lambda c: c.created)[-MAX_CHECKPOINTS:]
        live = {
            state.digest for checkpoint in checkpoints for state in checkpoint.files if state.digest
        }
        if self.objects.is_dir():
            for blob in self.objects.iterdir():
                if blob.is_file() and blob.name not in live:
                    blob.unlink(missing_ok=True)
        return checkpoints

    def discard(self, checkpoint: Checkpoint) -> bool:
        """Drop ``checkpoint`` from the history, keeping its objects if shared.

        ``undo`` calls this after restoring, so undoing twice steps back two
        changes instead of re-applying the same one.
        """
        checkpoints = self._load()
        remaining = [c for c in checkpoints if c.id != checkpoint.id]
        if len(remaining) == len(checkpoints):
            return False

        live = {state.digest for c in remaining for state in c.files if state.digest}
        if self.objects.is_dir():
            for blob in self.objects.iterdir():
                if blob.is_file() and blob.name not in live:
                    blob.unlink(missing_ok=True)
        self._save(remaining)
        return True

    def clear(self) -> int:
        """Delete every checkpoint. Returns how many were removed."""
        count = len(self._load())
        if self.root.is_dir():
            shutil.rmtree(self.root, ignore_errors=True)
        return count

    def size(self) -> int:
        """Total bytes held in the object store."""
        if not self.objects.is_dir():
            return 0
        return sum(f.stat().st_size for f in self.objects.iterdir() if f.is_file())


#: Tool arguments that name a file, in the order we look for one.
_PATH_KEYS = ("path", "file", "filename", "target")


def paths_for_tool(tool: str, arguments: dict[str, Any]) -> list[Path]:
    """Which files a tool call is about to affect."""
    if tool == "run_command":
        return []  # a command could touch anything; snapshotting is not meaningful
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return [Path(value)]
    return []
