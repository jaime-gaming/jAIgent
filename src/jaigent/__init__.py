"""jaigent — all your agents in one place.

Public API::

    from jaigent import Agent, Settings, build_default_registry

    agent = Agent(settings=Settings.from_env())
    print(agent.run("Summarise the README in this folder"))
"""

from jaigent.agent import Agent, AgentResult
from jaigent.approval import Approver, Mode
from jaigent.config import Settings
from jaigent.errors import (
    ConfigurationError,
    JaigentError,
    ProviderError,
    SandboxViolation,
    ToolError,
)
from jaigent.pricing import Cost, estimate
from jaigent.session import Session
from jaigent.tools import Tool, ToolRegistry, build_default_registry

__version__ = "0.5.2"

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
