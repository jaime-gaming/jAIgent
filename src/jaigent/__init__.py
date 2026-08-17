"""jaigent — a small, hackable AI agent that searches the web and works with local files.

Public API::

    from jaigent import Agent, Settings, build_default_registry

    agent = Agent(settings=Settings.from_env())
    print(agent.run("Summarise the README in this folder"))
"""

from jaigent.agent import Agent, AgentResult
from jaigent.config import Settings
from jaigent.errors import (
    ConfigurationError,
    JaigentError,
    ProviderError,
    SandboxViolation,
    ToolError,
)
from jaigent.tools import Tool, ToolRegistry, build_default_registry

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "AgentResult",
    "ConfigurationError",
    "JaigentError",
    "ProviderError",
    "SandboxViolation",
    "Settings",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "__version__",
    "build_default_registry",
]
