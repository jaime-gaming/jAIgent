"""Exception hierarchy for jaigent."""

from __future__ import annotations


class JaigentError(Exception):
    """Base class for every error raised by jaigent."""


class ConfigurationError(JaigentError):
    """Raised when the agent is misconfigured (missing API key, unknown provider, ...)."""


class ProviderError(JaigentError):
    """Raised when an LLM provider call fails or returns something unusable."""


class ToolError(JaigentError):
    """Raised when a tool fails in a way the model is expected to see and recover from."""


class SandboxViolation(ToolError):
    """Raised when a tool tries to touch a path outside the agent workspace."""


class MaxStepsExceeded(JaigentError):
    """Raised when the agent loop hits its step budget without producing an answer."""
