"""Private per-user store for provider API keys.

Settings files are meant to be committed, so they refuse secrets. Keys still
need somewhere to live that is not a project ``.env`` (those only work from
that directory) and not the process environment (which is awkward to edit
from the CLI). This module writes ``~/.jaigent/secrets.env`` with owner-only
permissions and loads it into ``os.environ`` on startup.
"""

from __future__ import annotations

import os
from pathlib import Path

from jaigent.config import API_KEY_ENV_VARS, KNOWN_PROVIDERS, LOCAL_PROVIDERS, load_dotenv
from jaigent.errors import ConfigurationError
from jaigent.paths import user_home, write_private

SECRETS_FILE = "secrets.env"


def secrets_path() -> Path:
    """Where provider keys are stored for this user."""
    return user_home() / SECRETS_FILE


def load_user_secrets() -> dict[str, str]:
    """Load the user secrets file into ``os.environ`` without overriding it."""
    return load_dotenv(secrets_path(), override=False)


def read_secrets() -> dict[str, str]:
    """Return the mapping stored in the secrets file (does not touch environ)."""
    path = secrets_path()
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _write_secrets(values: dict[str, str]) -> Path:
    lines = [
        "# Written by `jaigent auth`. Owner-only. Never commit this file.",
        *[f"{key}={values[key]}" for key in sorted(values) if values[key]],
        "",
    ]
    return write_private(secrets_path(), "\n".join(lines))


def env_var_for(provider: str) -> str:
    """The environment variable that holds ``provider``'s key."""
    name = provider.strip().lower()
    if name not in KNOWN_PROVIDERS:
        raise ConfigurationError(
            f"Unknown provider {provider!r}. Expected one of: {', '.join(KNOWN_PROVIDERS)}"
        )
    return API_KEY_ENV_VARS[name]


def set_key(provider: str, key: str) -> Path:
    """Store a provider key. Returns the secrets file path."""
    name = provider.strip().lower()
    secret = key.strip()
    if name in LOCAL_PROVIDERS:
        raise ConfigurationError(f"{name} runs locally and does not need an API key.")
    if not secret:
        raise ConfigurationError("API key must not be empty.")
    env_var = env_var_for(name)
    values = read_secrets()
    values[env_var] = secret
    path = _write_secrets(values)
    # Make the rest of this process see it immediately.
    os.environ.setdefault(env_var, secret)
    return path


def unset_key(provider: str) -> bool:
    """Remove a stored key. Returns whether one was present."""
    env_var = env_var_for(provider)
    values = read_secrets()
    if env_var not in values:
        return False
    del values[env_var]
    _write_secrets(values)
    return True


def listed_keys() -> list[tuple[str, str, str]]:
    """``(provider, env_var, masked)`` for every stored key."""
    values = read_secrets()
    rows: list[tuple[str, str, str]] = []
    for provider in KNOWN_PROVIDERS:
        env_var = API_KEY_ENV_VARS[provider]
        secret = values.get(env_var, "")
        if not secret:
            continue
        rows.append((provider, env_var, mask_key(secret)))
    return rows


def mask_key(value: str) -> str:
    """Show enough of a key to recognise it, never enough to use it."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"
