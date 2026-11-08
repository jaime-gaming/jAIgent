"""jAIgent — all your agents in one place.

Public API::

    from jaigent import Agent, Settings, build_default_registry

    agent = Agent(settings=Settings.from_env())
    print(agent.run("Summarise the README in this folder"))

The names below are resolved on first access (PEP 562 ``__getattr__``) instead
of at import time. That is not micro-optimisation: importing the agent pulls in
the tool registry, ``httpx``, ``urllib.request`` and the web tools, which is
~90 ms — and ``jaigent --version``, the landing page and ``jaigent help`` need
none of it. ``from jaigent import Agent`` still works exactly as before; it just
does the work at the moment it is asked for.

Everything exported here is unchanged. If you add a name, add it to both the
map and ``__all__``.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from jaigent.errors import (
    ConfigurationError,
    JaigentError,
    ProviderError,
    SandboxViolation,
    ToolError,
)

__version__ = "0.5.8"

#: name -> (module, attribute). Kept as a table so a typo is a test failure
#: (``tests/test_public_api.py`` walks it) rather than a missing import.
_LAZY: dict[str, tuple[str, str]] = {
    "Agent": ("jaigent.agent", "Agent"),
    "AgentResult": ("jaigent.agent", "AgentResult"),
    "Approver": ("jaigent.approval", "Approver"),
    "Mode": ("jaigent.approval", "Mode"),
    "Settings": ("jaigent.config", "Settings"),
    "Cost": ("jaigent.pricing", "Cost"),
    "estimate": ("jaigent.pricing", "estimate"),
    "Session": ("jaigent.session", "Session"),
    "Tool": ("jaigent.tools", "Tool"),
    "ToolRegistry": ("jaigent.tools", "ToolRegistry"),
    "build_default_registry": ("jaigent.tools", "build_default_registry"),
}

if TYPE_CHECKING:  # pragma: no cover - for type checkers and editors only
    from jaigent.agent import Agent, AgentResult
    from jaigent.approval import Approver, Mode
    from jaigent.config import Settings
    from jaigent.pricing import Cost, estimate
    from jaigent.session import Session
    from jaigent.tools import Tool, ToolRegistry, build_default_registry

__all__ = [
    "Agent",
    "AgentResult",
    "Approver",
    "ConfigurationError",
    "Cost",
    "JaigentError",
    "Mode",
    "ProviderError",
    "SandboxViolation",
    "Session",
    "Settings",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "__version__",
    "build_default_registry",
    "estimate",
]


def __getattr__(name: str) -> Any:
    """Resolve a public name on first use."""
    try:
        module_name, attribute = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name), attribute)
    # Cache it, so repeated access is a plain dict lookup after the first call.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
