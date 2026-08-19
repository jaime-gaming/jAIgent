"""Persistent settings files.

jaigent resolves configuration from five layers, each overriding the one below:

1. CLI flags
2. Environment variables (and ``.env``)
3. Project settings — ``./.jaigent/settings.json``
4. User settings — ``~/.jaigent/settings.json``
5. Built-in defaults

The two JSON layers are managed by this module and edited with ``jaigent
settings``. Project settings are meant to be committed so a team shares the same
model and approval policy; user settings hold personal preferences.

API keys are deliberately **not** written here — they belong in the environment
or a git-ignored ``.env``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jaigent.config import (
    APPROVAL_MODES,
    KNOWN_PROVIDERS,
    SEARCH_BACKENDS,
    TEMPERATURE_RANGE,
)
from jaigent.errors import ConfigurationError
from jaigent.paths import PROJECT_DIR, user_home

#: Settings that may be stored in a settings file, with their parsers.
#: Anything not listed here is rejected, which catches typos early.
ALLOWED_KEYS: dict[str, str] = {
    "provider": "str",
    "model": "str",
    "base_url": "str",
    "max_steps": "int",
    "temperature": "float",
    "max_tokens": "int",
    "timeout": "float",
    "search_backend": "str",
    "allow_shell": "bool",
    "verbose": "bool",
    "stream": "bool",
    "show_cost": "bool",
    "approval": "str",
    "skills_enabled": "bool",
    "plugins_enabled": "bool",
    "checkpoints": "bool",
    "failover": "bool",
    "retries": "int",
    "budget": "float",
    "memory": "bool",
    "auto_compact": "bool",
}

#: Keys that must never be persisted, even if a user tries.
FORBIDDEN_KEYS = frozenset({"api_key", "search_api_key", "jaigent_api_key", "openai_api_key"})

SETTINGS_FILE = "settings.json"


def user_settings_path() -> Path:
    """The per-user settings file. See :mod:`jaigent.paths` for the location."""
    return user_home() / SETTINGS_FILE


def project_settings_path(start: Path | None = None) -> Path:
    """``./.jaigent/settings.json`` for the current (or given) directory."""
    return Path(start or Path.cwd()) / PROJECT_DIR / SETTINGS_FILE


def _coerce(key: str, value: Any) -> Any:
    """Convert a raw string or JSON value into the type the key expects."""
    kind = ALLOWED_KEYS[key]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"{key} must be an integer, got {value!r}") from exc
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"{key} must be a number, got {value!r}") from exc
    return str(value)


#: Settings whose value must be one of a fixed set. A settings file is read at
#: every startup, so writing an unusable value here breaks every later command.
CHOICES: dict[str, tuple[str, ...]] = {
    "provider": KNOWN_PROVIDERS,
    "approval": APPROVAL_MODES,
    "search_backend": SEARCH_BACKENDS,
}

#: Settings that must be greater than zero to mean anything.
POSITIVE_KEYS = frozenset({"max_steps", "retries", "max_tokens", "timeout"})


def validate_value(key: str, value: Any) -> Any:
    """Check a coerced value is one this CLI can actually run with.

    Type-checking alone is not enough: ``provider`` is a string, but a string
    naming no known provider makes every subsequent command fail at startup —
    including the ones needed to put it right.
    """
    if key in CHOICES:
        allowed = CHOICES[key]
        normalised = str(value).strip().lower()
        if normalised not in allowed:
            raise ConfigurationError(
                f"Unknown {key} {str(value).strip()!r}. Expected one of: {', '.join(allowed)}"
            )
        return normalised

    if ALLOWED_KEYS[key] == "str":
        text = str(value).strip()
        if not text:
            raise ConfigurationError(
                f"{key} must not be empty. Use `jaigent settings unset {key}` "
                "to fall back to the default."
            )
        return text

    if key in POSITIVE_KEYS and value <= 0:
        raise ConfigurationError(f"{key} must be greater than 0, got {value!r}")

    if key == "temperature":
        low, high = TEMPERATURE_RANGE
        if not low <= value <= high:
            raise ConfigurationError(f"temperature must be between {low} and {high}, got {value!r}")

    if key == "budget" and value < 0:
        raise ConfigurationError(f"budget must be >= 0, got {value!r}")

    return value


def validate_key(key: str) -> str:
    """Normalise and check a setting name."""
    name = key.strip().lower().replace("-", "_")
    if name in FORBIDDEN_KEYS:
        raise ConfigurationError(
            f"{name} must never be stored in a settings file. "
            "Put secrets in the environment or a git-ignored .env instead."
        )
    if name not in ALLOWED_KEYS:
        close = ", ".join(sorted(ALLOWED_KEYS))
        raise ConfigurationError(f"Unknown setting {key!r}. Valid settings are: {close}")
    return name


def read(path: Path) -> dict[str, Any]:
    """Load one settings file. A missing file is an empty layer.

    A malformed file raises, because silently ignoring a settings file the user
    wrote is far more confusing than a clear error.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:  # pragma: no cover - defensive
        raise ConfigurationError(f"Could not read {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigurationError(f"{path} must contain a JSON object")

    clean: dict[str, Any] = {}
    for key, value in data.items():
        name = key.strip().lower().replace("-", "_")
        if name in FORBIDDEN_KEYS:
            continue  # never honour a secret from a settings file
        if name in ALLOWED_KEYS:
            clean[name] = _coerce(name, value)
    return clean


def write(path: Path, values: dict[str, Any]) -> Path:
    """Write a settings file, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(values, indent=2, sort_keys=True) + "\n"
    temp = path.with_suffix(".tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)
    return path


def load_layers(start: Path | None = None) -> dict[str, Any]:
    """Merge the user layer and then the project layer over it."""
    merged = dict(read(user_settings_path()))
    merged.update(read(project_settings_path(start)))
    return merged


def set_value(key: str, value: Any, *, scope: str = "user", start: Path | None = None) -> Path:
    """Set one setting in the user or project file."""
    name = validate_key(key)
    path = user_settings_path() if scope == "user" else project_settings_path(start)
    values = read(path)
    values[name] = validate_value(name, _coerce(name, value))
    return write(path, values)


def unset_value(key: str, *, scope: str = "user", start: Path | None = None) -> bool:
    """Remove one setting. Returns whether it was present."""
    name = validate_key(key)
    path = user_settings_path() if scope == "user" else project_settings_path(start)
    values = read(path)
    if name not in values:
        return False
    del values[name]
    write(path, values)
    return True


def describe(start: Path | None = None) -> list[tuple[str, Any, str]]:
    """Every stored setting as ``(key, value, source)``, project layer winning."""
    rows: dict[str, tuple[Any, str]] = {}
    for value_source, path in (
        ("user", user_settings_path()),
        ("project", project_settings_path(start)),
    ):
        for key, value in read(path).items():
            rows[key] = (value, value_source)
    return sorted((key, value, source) for key, (value, source) in rows.items())
