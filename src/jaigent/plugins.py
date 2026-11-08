"""Local tool plugins.

A plugin is a Python file in ``./.jaigent/plugins`` (project) or
``~/.jaigent/plugins`` (personal) that defines::

    def register(registry, settings):
        registry.register(Tool(name=..., description=..., parameters=..., func=...))

Project plugins shadow user plugins of the same name. Loading one runs local
code you put there — never anything from the network. A broken plugin is
skipped so it cannot take down a run.

Plugins are **tools**, not prompt text. For saved procedures use skills; for
slash-command templates use commands.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from jaigent.errors import ToolError
from jaigent.paths import scoped_dirs

if TYPE_CHECKING:
    from jaigent.config import Settings
    from jaigent.tools.base import ToolRegistry

PLUGINS_DIRNAME = "plugins"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

PLUGIN_TEMPLATE = '''\
"""A jAIgent plugin. Edit register() to add tools."""

from __future__ import annotations

from jaigent.tools import Tool


def register(registry, settings) -> None:
    """Register tools on ``registry``. ``settings.workspace`` is the sandbox."""

    def hello(name: str = "world") -> str:
        return f"hello, {{name}}"

    registry.register(
        Tool(
            name="{name}",
            description="Say hello. Replace this with a tool the model should call.",
            parameters={{
                "type": "object",
                "properties": {{
                    "name": {{"type": "string", "description": "Who to greet."}},
                }},
                "required": [],
            }},
            func=hello,
            # A tool that only reads declares it, so `ask` mode does not stop
            # to confirm a greeting. Leave it out (or False) if yours writes.
            read_only=True,
        )
    )
'''


@dataclass(slots=True, frozen=True)
class Plugin:
    """One discovered plugin file."""

    name: str
    path: Path
    scope: str = "project"
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return f"{self.name}: {self.error}"
        return self.name


def plugins_dirs(start: Path | None = None) -> list[tuple[str, Path]]:
    """Directories searched for plugins, lowest priority first."""
    return scoped_dirs(PLUGINS_DIRNAME, start)


def discover(start: Path | None = None) -> dict[str, Plugin]:
    """Find every plugin file, project copies shadowing user ones."""
    found: dict[str, Plugin] = {}
    for scope, directory in plugins_dirs(start):
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.py")):
            if file.name.startswith("_"):
                continue
            name = file.stem.strip().lower()
            if not NAME_RE.match(name):
                continue
            found[name] = Plugin(name=name, path=file, scope=scope)
    return found


def _load_module(plugin: Plugin) -> ModuleType:
    """Import ``plugin.path`` as a unique module name."""
    module_name = f"jaigent_plugin_{plugin.scope}_{plugin.name}"
    spec = importlib.util.spec_from_file_location(module_name, plugin.path)
    if spec is None or spec.loader is None:
        raise ToolError(f"Could not load plugin {plugin.name} from {plugin.path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the plugin can import itself if it wants to.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


#: Environment variable that opts a *project's* plugins into being imported.
#: A plugin file is executable Python, and a project directory is data that
#: arrived over the network, so importing it needs a decision the user made
#: about this workspace rather than a default.
TRUST_PROJECT_VAR = "JAIGENT_TRUST_PROJECT_PLUGINS"


def project_plugins_trusted() -> bool:
    """Whether project-scope plugins may be imported automatically."""
    return os.getenv(TRUST_PROJECT_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def skipped_project_plugins(start: Path | None = None) -> list[str]:
    """Project plugins that were found but not loaded.

    Exposed so the CLI can say *why* a plugin is not in the tool list instead of
    leaving the user to discover that their file was ignored.
    """
    if project_plugins_trusted():
        return []
    return sorted(
        plugin.name
        for plugin in discover(start).values()
        if plugin.scope == "project" and not plugin.error
    )


def apply(
    registry: ToolRegistry,
    settings: Settings,
    *,
    start: Path | None = None,
    trust_project: bool | None = None,
) -> list[Plugin]:
    """Load every plugin and call ``register(registry, settings)``.

    Project-scope plugins — the ones inside the repository you are working in —
    are only imported when the user has said so, with
    ``JAIGENT_TRUST_PROJECT_PLUGINS=1`` or ``trust_project=True``. Everything
    else would make cloning a repository equivalent to running its setup
    script, and a prompt injection could then use the file tools to write one.
    User-scope plugins are the user's own code and load as before.

    Returns the plugins that were attempted. Failures are recorded on
    ``Plugin.error`` rather than raised, so one bad file cannot break a run.
    """
    trusted = project_plugins_trusted() if trust_project is None else trust_project
    loaded: list[Plugin] = []
    for plugin in discover(start).values():
        if plugin.scope == "project" and not trusted:
            # Not an error: this is the safe default, and the CLI reports it.
            continue
        try:
            module = _load_module(plugin)
            register = getattr(module, "register", None)
            if not callable(register):
                loaded.append(
                    Plugin(
                        name=plugin.name,
                        path=plugin.path,
                        scope=plugin.scope,
                        error="no register(registry, settings) function",
                    )
                )
                continue
            # A cloned repo's plugins must not see live keys.
            register(registry, replace(settings, api_key=None, search_api_key=None))
            loaded.append(plugin)
        except Exception as exc:  # noqa: BLE001 - a plugin must never crash a run
            loaded.append(
                Plugin(
                    name=plugin.name,
                    path=plugin.path,
                    scope=plugin.scope,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return loaded


def create_plugin(name: str, *, scope: str = "project", start: Path | None = None) -> Path:
    """Write a starter plugin file and return its path."""
    clean = name.strip().lower().replace(" ", "-")
    if not NAME_RE.match(clean):
        raise ToolError(
            f"Invalid plugin name {name!r}. Use lowercase letters, digits, dots, "
            "dashes or underscores, starting with a letter or digit."
        )
    directory = dict(plugins_dirs(start))[scope]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{clean}.py"
    if path.exists():
        raise ToolError(f"A plugin named {clean!r} already exists at {path}")
    path.write_text(PLUGIN_TEMPLATE.format(name=clean), encoding="utf-8")
    return path
