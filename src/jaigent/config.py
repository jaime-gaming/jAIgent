"""Configuration for jaigent.

Everything is configurable through environment variables so that no secret ever
has to live in the repository. ``Settings.from_env()`` is the single entry point
used by the CLI; the library API accepts an explicit ``Settings`` instance too.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from jaigent.errors import ConfigurationError

#: Providers understood by :func:`jaigent.llm.get_provider`.
KNOWN_PROVIDERS = ("openai", "anthropic")

#: Approval policies for mutating tools. See :mod:`jaigent.approval`.
APPROVAL_MODES = ("ask", "auto", "dry-run")

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-latest",
}

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}

API_KEY_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc


def load_dotenv(path: str | os.PathLike[str] = ".env", *, override: bool = False) -> dict[str, str]:
    """Load ``KEY=value`` pairs from a dotenv file into ``os.environ``.

    A tiny, dependency-free loader: blank lines and ``#`` comments are skipped,
    an optional ``export`` prefix is tolerated and surrounding quotes stripped.
    Existing environment variables win unless ``override`` is true.

    Returns the mapping that was applied.
    """
    file = Path(path)
    applied: dict[str, str] = {}
    if not file.is_file():
        return applied

    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


@dataclass(slots=True)
class Settings:
    """Runtime configuration for an :class:`~jaigent.agent.Agent`.

    Attributes:
        provider: LLM backend name, one of :data:`KNOWN_PROVIDERS`.
        model: Model identifier passed to the provider.
        api_key: Secret used to authenticate. **Never** committed; read from env.
        base_url: API root, override it to target a compatible gateway
            (OpenRouter, Groq, Ollama, vLLM, Azure-style proxies, ...).
        workspace: Directory the file tools are confined to.
        max_steps: Hard cap on tool-calling iterations per run.
        temperature: Sampling temperature forwarded to the provider.
        max_tokens: Upper bound on tokens generated per assistant turn.
        timeout: Per-request HTTP timeout in seconds.
        search_backend: Web search implementation (``duckduckgo`` or ``tavily``).
        search_api_key: Key for search backends that need one (Tavily).
        allow_shell: Enables the opt-in ``run_command`` tool.
        verbose: Print each tool call to stderr while running.
        stream: Print assistant text token by token as it arrives.
        show_cost: Print a token and cost estimate after each run.
        approval: How to handle mutating tools — ``ask``, ``auto`` or ``dry-run``.
    """

    provider: str = "openai"
    model: str = DEFAULT_MODELS["openai"]
    api_key: str | None = None
    base_url: str | None = None
    workspace: Path = field(default_factory=Path.cwd)
    max_steps: int = 12
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout: float = 60.0
    search_backend: str = "duckduckgo"
    search_api_key: str | None = None
    allow_shell: bool = False
    verbose: bool = False
    stream: bool = True
    show_cost: bool = True
    approval: str = "auto"

    def __post_init__(self) -> None:
        self.provider = self.provider.strip().lower()
        self.search_backend = self.search_backend.strip().lower()
        self.approval = self.approval.strip().lower()
        self.workspace = Path(self.workspace).expanduser().resolve()
        if self.max_steps < 1:
            raise ConfigurationError("max_steps must be >= 1")
        if self.approval not in APPROVAL_MODES:
            raise ConfigurationError(
                f"Unknown approval mode {self.approval!r}. "
                f"Expected one of: {', '.join(APPROVAL_MODES)}"
            )

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, *, dotenv: str | os.PathLike[str] | None = ".env") -> Settings:
        """Build settings from environment variables (and an optional ``.env``)."""
        if dotenv is not None:
            load_dotenv(dotenv)

        provider = (os.getenv("JAIGENT_PROVIDER") or "openai").strip().lower()
        if provider not in KNOWN_PROVIDERS:
            raise ConfigurationError(
                f"Unknown provider {provider!r}. Expected one of: {', '.join(KNOWN_PROVIDERS)}"
            )

        api_key = os.getenv("JAIGENT_API_KEY") or os.getenv(API_KEY_ENV_VARS[provider])
        model = os.getenv("JAIGENT_MODEL") or DEFAULT_MODELS[provider]
        base_url = os.getenv("JAIGENT_BASE_URL") or DEFAULT_BASE_URLS[provider]
        workspace = os.getenv("JAIGENT_WORKSPACE") or str(Path.cwd())

        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            workspace=Path(workspace),
            max_steps=_env_int("JAIGENT_MAX_STEPS", 12),
            temperature=_env_float("JAIGENT_TEMPERATURE", 0.2),
            max_tokens=_env_int("JAIGENT_MAX_TOKENS", 2048),
            timeout=_env_float("JAIGENT_TIMEOUT", 60.0),
            search_backend=(os.getenv("JAIGENT_SEARCH_BACKEND") or "duckduckgo"),
            search_api_key=os.getenv("TAVILY_API_KEY"),
            allow_shell=_env_flag("JAIGENT_ALLOW_SHELL", False),
            verbose=_env_flag("JAIGENT_VERBOSE", False),
            stream=_env_flag("JAIGENT_STREAM", True),
            show_cost=_env_flag("JAIGENT_SHOW_COST", True),
            approval=(os.getenv("JAIGENT_APPROVAL") or "auto"),
        )

    def merged_with(self, **overrides: object) -> Settings:
        """Return a copy with the non-``None`` ``overrides`` applied."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def require_api_key(self) -> str:
        """Return the API key or explain exactly how to provide one."""
        if self.api_key:
            return self.api_key
        env_var = API_KEY_ENV_VARS.get(self.provider, "JAIGENT_API_KEY")
        raise ConfigurationError(
            f"No API key found for provider {self.provider!r}.\n"
            f"  Set it with:  export {env_var}='sk-...'\n"
            f"  Or put it in a .env file next to your project (see .env.example).\n"
            f"  jaigent never ships with a key — you always bring your own."
        )

    def redacted(self) -> dict[str, object]:
        """A dict representation that is safe to print or log."""

        def mask(value: str | None) -> str:
            if not value:
                return "<unset>"
            return f"{value[:4]}…{value[-2:]}" if len(value) > 8 else "<set>"

        return {
            "provider": self.provider,
            "model": self.model,
            "api_key": mask(self.api_key),
            "base_url": self.base_url,
            "workspace": str(self.workspace),
            "max_steps": self.max_steps,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "search_backend": self.search_backend,
            "search_api_key": mask(self.search_api_key),
            "allow_shell": self.allow_shell,
            "verbose": self.verbose,
            "stream": self.stream,
            "show_cost": self.show_cost,
            "approval": self.approval,
        }
