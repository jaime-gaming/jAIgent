"""Saving and resuming conversations.

Sessions are plain JSON under ``~/.jaigent/sessions`` (override with
``JAIGENT_SESSION_DIR``). Each file holds the message history plus enough
metadata to show a useful listing, so ``jaigent chat --resume`` can pick up
where you left off.

Nothing secret is written: the API key never enters a session file.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jaigent.paths import user_home, write_private

SESSION_VERSION = 1
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def session_dir() -> Path:
    """Where sessions live. Created on demand."""
    raw = os.getenv("JAIGENT_SESSION_DIR")
    return Path(raw).expanduser() if raw else user_home() / "sessions"


def _slugify(text: str, limit: int = 40) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug[:limit] or "session"


@dataclass(slots=True)
class Session:
    """A saved conversation."""

    id: str
    title: str = ""
    provider: str = ""
    model: str = ""
    workspace: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @classmethod
    def new(cls, *, provider: str = "", model: str = "", workspace: str = "") -> Session:
        """Start a fresh session with a timestamp-based id."""
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        candidate = stamp
        suffix = 1
        while (session_dir() / f"{candidate}.json").exists():
            candidate = f"{stamp}-{suffix}"
            suffix += 1
        return cls(id=candidate, provider=provider, model=model, workspace=workspace)

    @property
    def path(self) -> Path:
        return session_dir() / f"{self.id}.json"

    @property
    def turns(self) -> int:
        """How many user messages the conversation contains."""
        return sum(1 for m in self.messages if m.get("role") == "user")

    def touch(self, messages: list[dict[str, Any]], usage: dict[str, int] | None = None) -> None:
        """Update the stored history and timestamps."""
        self.messages = messages
        self.updated = time.time()
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                self.usage[key] = self.usage.get(key, 0) + value

    def set_title_from(self, prompt: str) -> None:
        """Derive a human-readable title from the first thing the user asked."""
        if not self.title:
            clean = " ".join(prompt.split())
            self.title = clean[:70] + ("…" if len(clean) > 70 else "")

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SESSION_VERSION,
            "id": self.id,
            "title": self.title,
            "provider": self.provider,
            "model": self.model,
            "workspace": self.workspace,
            "created": self.created,
            "updated": self.updated,
            "usage": self.usage,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=str(data.get("id", "unknown")),
            title=str(data.get("title", "")),
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            workspace=str(data.get("workspace", "")),
            created=float(data.get("created", 0.0)),
            updated=float(data.get("updated", 0.0)),
            messages=list(data.get("messages", [])),
            usage=dict(data.get("usage", {})),
        )

    def save(self) -> Path:
        """Write the session to disk atomically, owner-only."""
        return write_private(self.path, json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    def delete(self) -> bool:
        """Remove the session file. Returns whether anything was deleted."""
        if self.path.is_file():
            self.path.unlink()
            return True
        return False

    def age(self) -> str:
        """``3m ago``, ``2h ago``, ``5d ago``."""
        seconds = max(0.0, time.time() - self.updated)
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)}m ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h ago"
        return f"{int(seconds // 86400)}d ago"


# ----------------------------------------------------------------------
def load(session_id: str) -> Session | None:
    """Load one session by id, or ``None`` if it does not exist or is corrupt."""
    file = session_dir() / f"{session_id}.json"
    if not file.is_file():
        return None
    try:
        return Session.from_dict(json.loads(file.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def list_sessions(limit: int = 20) -> list[Session]:
    """All saved sessions, newest first. Unreadable files are skipped."""
    directory = session_dir()
    if not directory.is_dir():
        return []

    sessions: list[Session] = []
    for file in directory.glob("*.json"):
        try:
            sessions.append(Session.from_dict(json.loads(file.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue

    # Id is the tie-break: Windows time.time() often matches for two saves,
    # and glob order is not "newest first".
    sessions.sort(key=lambda s: (s.updated, s.id), reverse=True)
    return sessions[:limit]


def latest() -> Session | None:
    """The most recently updated session, if any."""
    found = list_sessions(limit=1)
    return found[0] if found else None


def resolve(reference: str | None) -> Session | None:
    """Resolve a user-supplied session reference.

    ``None`` or ``"last"`` means the most recent session; otherwise the id is
    matched exactly, then by prefix.
    """
    if reference in (None, "", "last", "latest"):
        return latest()

    assert reference is not None
    exact = load(reference)
    if exact is not None:
        return exact

    for session in list_sessions(limit=1000):
        if session.id.startswith(reference):
            return session
    return None
