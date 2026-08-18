"""Scheduled tasks: prompts jaigent runs on a timer.

A schedule pairs a prompt with an interval ("every 30m", "hourly", "daily at
09:00") and a workspace. ``jaigent schedule run`` executes whatever is due;
``--watch`` keeps a loop alive and runs each task as its turn arrives.

The design is deliberately dependency-free and crash-safe: state lives in one
JSON file, ``next_run`` is recomputed after every execution, and a task that
raises is recorded as failed rather than killing the loop. For unattended use,
point cron or a systemd timer at ``jaigent schedule run`` — the due-check makes
that idempotent.

Scheduled runs are non-interactive, so they use the ``auto`` approval policy and
can write files. Keep their workspaces somewhere you are happy to see change.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jaigent.errors import ConfigurationError
from jaigent.paths import user_home

SCHEDULE_VERSION = 1

#: "every 30m", "30m", "hourly", "daily", "daily at 09:00", "weekly"
_EVERY_RE = re.compile(r"^(?:every\s+)?(\d+)\s*([smhdw])[a-z]*$", re.I)
_AT_RE = re.compile(r"^(daily|hourly|weekly)(?:\s+at\s+(\d{1,2}):(\d{2}))?$", re.I)

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def schedules_path() -> Path:
    """Where the schedule file lives."""
    raw = os.getenv("JAIGENT_SCHEDULE_FILE")
    if raw:
        return Path(raw).expanduser()
    return user_home() / "schedules.json"


def parse_interval(text: str) -> tuple[int, str]:
    """Turn a human interval into ``(seconds, canonical_text)``.

    Accepts ``30m``, ``every 2h``, ``hourly``, ``daily``, ``daily at 09:00``,
    ``weekly``.

    Raises:
        ConfigurationError: if the interval cannot be understood.
    """
    value = (text or "").strip().lower()
    if not value:
        raise ConfigurationError("An interval is required, e.g. '30m' or 'daily at 09:00'")

    match = _EVERY_RE.match(value)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        if amount < 1:
            raise ConfigurationError("Interval must be at least 1")
        seconds = amount * _UNIT_SECONDS[unit]
        if seconds < 60:
            raise ConfigurationError("The shortest supported interval is 1 minute")
        return seconds, f"every {amount}{unit}"

    match = _AT_RE.match(value)
    if match:
        period = match.group(1).lower()
        base = {"hourly": 3600, "daily": 86400, "weekly": 604800}[period]
        if match.group(2) is not None:
            hour, minute = int(match.group(2)), int(match.group(3))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ConfigurationError(f"{hour:02d}:{minute:02d} is not a valid time of day")
            return base, f"{period} at {hour:02d}:{minute:02d}"
        return base, period

    raise ConfigurationError(
        f"Could not understand the interval {text!r}. Try '30m', 'every 2h', "
        "'hourly', 'daily', 'daily at 09:00' or 'weekly'."
    )


def next_occurrence(interval: str, *, after: float | None = None) -> float:
    """When a task with this interval should next run, as a unix timestamp."""
    now = after if after is not None else time.time()
    seconds, canonical = parse_interval(interval)

    at_match = _AT_RE.match(canonical)
    if at_match and at_match.group(2) is not None:
        hour, minute = int(at_match.group(2)), int(at_match.group(3))
        current = datetime.fromtimestamp(now)
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(seconds=seconds)
        return target.timestamp()

    return now + seconds


@dataclass(slots=True)
class Task:
    """One scheduled prompt."""

    id: str
    prompt: str
    interval: str
    workspace: str = ""
    model: str = ""
    enabled: bool = True
    created: float = field(default_factory=time.time)
    #: Unix timestamp of the next run. ``-1`` means "not scheduled yet"; ``0``
    #: is a legitimate value meaning "overdue", so it must not be treated as unset.
    next_run: float = -1.0
    last_run: float = 0.0
    last_status: str = ""
    last_output: str = ""
    runs: int = 0
    failures: int = 0

    def __post_init__(self) -> None:
        if self.next_run < 0:
            self.next_run = next_occurrence(self.interval)

    def is_due(self, *, now: float | None = None) -> bool:
        return self.enabled and self.next_run <= (now if now is not None else time.time())

    def reschedule(self, *, now: float | None = None) -> None:
        self.next_run = next_occurrence(self.interval, after=now or time.time())

    def record(self, status: str, output: str, *, now: float | None = None) -> None:
        """Note the outcome of a run and move the clock forward."""
        moment = now if now is not None else time.time()
        self.last_run = moment
        self.last_status = status
        self.last_output = output[:2000]
        self.runs += 1
        if status != "ok":
            self.failures += 1
        self.reschedule(now=moment)

    def due_in(self) -> str:
        """``in 12m``, ``overdue``, ``paused``."""
        if not self.enabled:
            return "paused"
        remaining = self.next_run - time.time()
        if remaining <= 0:
            return "due now"
        # Round up: a task 119 minutes away reads better as "in 2h" than "in 1h",
        # and floor division would otherwise show 0 for anything under a unit.
        if remaining < 3600:
            return f"in {math.ceil(remaining / 60)}m"
        if remaining < 86400:
            return f"in {math.ceil(remaining / 3600)}h"
        return f"in {math.ceil(remaining / 86400)}d"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "interval": self.interval,
            "workspace": self.workspace,
            "model": self.model,
            "enabled": self.enabled,
            "created": self.created,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "last_output": self.last_output,
            "runs": self.runs,
            "failures": self.failures,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=str(data.get("id", "")),
            prompt=str(data.get("prompt", "")),
            interval=str(data.get("interval", "daily")),
            workspace=str(data.get("workspace", "")),
            model=str(data.get("model", "")),
            enabled=bool(data.get("enabled", True)),
            created=float(data.get("created", 0.0)),
            next_run=float(data.get("next_run", -1.0)),
            last_run=float(data.get("last_run", 0.0)),
            last_status=str(data.get("last_status", "")),
            last_output=str(data.get("last_output", "")),
            runs=int(data.get("runs", 0)),
            failures=int(data.get("failures", 0)),
        )


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------
def load_all() -> list[Task]:
    """Every saved task. A corrupt file yields an empty list, never a crash."""
    path = schedules_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    raw = data.get("tasks", []) if isinstance(data, dict) else data
    tasks: list[Task] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            tasks.append(Task.from_dict(item))
        except (TypeError, ValueError):
            continue
    return tasks


def save_all(tasks: list[Task]) -> Path:
    """Persist the task list atomically."""
    path = schedules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": SCHEDULE_VERSION, "tasks": [t.to_dict() for t in tasks]}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return path


def _next_id(tasks: list[Task]) -> str:
    used = {task.id for task in tasks}
    index = 1
    while f"task-{index}" in used:
        index += 1
    return f"task-{index}"


def add(prompt: str, interval: str, *, workspace: str = "", model: str = "") -> Task:
    """Create a task. Validates the interval before saving."""
    if not prompt.strip():
        raise ConfigurationError("A scheduled task needs a prompt")
    parse_interval(interval)  # raises if unusable

    tasks = load_all()
    task = Task(
        id=_next_id(tasks),
        prompt=prompt.strip(),
        interval=interval.strip().lower(),
        workspace=workspace or str(Path.cwd()),
        model=model,
    )
    tasks.append(task)
    save_all(tasks)
    return task


def get(task_id: str) -> Task | None:
    """Find a task by exact id, then by prefix."""
    tasks = load_all()
    for task in tasks:
        if task.id == task_id:
            return task
    for task in tasks:
        if task.id.startswith(task_id):
            return task
    return None


def update(task: Task) -> None:
    """Write one task back into the store."""
    tasks = load_all()
    for index, existing in enumerate(tasks):
        if existing.id == task.id:
            tasks[index] = task
            save_all(tasks)
            return
    tasks.append(task)
    save_all(tasks)


def remove(task_id: str) -> bool:
    """Delete a task. Returns whether it existed."""
    tasks = load_all()
    remaining = [t for t in tasks if t.id != task_id]
    if len(remaining) == len(tasks):
        return False
    save_all(remaining)
    return True


def set_enabled(task_id: str, enabled: bool) -> Task | None:
    """Pause or resume a task."""
    task = get(task_id)
    if task is None:
        return None
    task.enabled = enabled
    if enabled:
        task.reschedule()
    update(task)
    return task


def due_tasks(*, now: float | None = None) -> list[Task]:
    """Every enabled task whose time has come."""
    return [task for task in load_all() if task.is_due(now=now)]
