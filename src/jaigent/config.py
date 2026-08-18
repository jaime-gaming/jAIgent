"""Configuration for jaigent.

Everything is configurable through environment variables so that no secret ever
has to live in the repository. ``Settings.from_env()`` is the single entry point
used by the CLI; the library API accepts an explicit ``Settings`` instance too.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from jaigent.errors import ConfigurationError

#: Providers understood by :func:`jaigent.llm.get_provider`.
#: Everything except ``anthropic`` speaks the OpenAI chat-completions shape and
#: is served by the same adapter with a different base URL.
KNOWN_PROVIDERS = (
    "openai",
    "anthropic",
    "gemini",
    "omniroute",
    "openrouter",
    "groq",
    "deepseek",
    "mistral",
    "xai",
    "together",
    "ollama",
)

#: Approval policies for mutating tools. See :mod:`jaigent.approval`.
APPROVAL_MODES = ("ask", "auto", "dry-run")

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-latest",
    "gemini": "gemini-2.5-flash",
    "omniroute": "auto",
    "openrouter": "anthropic/claude-sonnet-4",
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
    "mistral": "mistral-small-latest",
    "xai": "grok-4",
    "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "ollama": "qwen2.5:14b",
}

#: OmniRoute runs on your own machine by default; override with JAIGENT_BASE_URL
#: (or OMNIROUTE_BASE_URL) to point at a remote gateway.
OMNIROUTE_DEFAULT_URL = "http://localhost:20128/v1"

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "omniroute": OMNIROUTE_DEFAULT_URL,
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "together": "https://api.together.xyz/v1",
    "ollama": "http://localhost:11434/v1",
}

API_KEY_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "omniroute": "OMNIROUTE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
    "together": "TOGETHER_API_KEY",
    "ollama": "OLLAMA_API_KEY",
}

#: Providers that run locally and therefore accept any placeholder key.
LOCAL_PROVIDERS = frozenset({"ollama", "omniroute"})


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
        skills_enabled: Load skills from ``.jaigent/skills`` and offer ``load_skill``.
        checkpoints: Snapshot files before mutating them so runs can be undone.
        failover: Retry, then fall through to another configured provider.
        retries: Attempts per provider before failing over.
    """

    provider: str = "openai"
    model: str = ""
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
    skills_enabled: bool = True
    checkpoints: bool = True
    failover: bool = True
    retries: int = 3

    def __post_init__(self) -> None:
        self.provider = self.provider.strip().lower()
        self.search_backend = self.search_backend.strip().lower()
        self.approval = self.approval.strip().lower()
        self.workspace = Path(self.workspace).expanduser().resolve()

        # Fall back to each provider's own defaults rather than OpenAI's, so
        # Settings(provider="omniroute") is usable without naming a model.
        if not self.model:
            self.model = DEFAULT_MODELS.get(self.provider, DEFAULT_MODELS["openai"])
        if not self.base_url:
            self.base_url = DEFAULT_BASE_URLS.get(self.provider)

        if self.max_steps < 1:
            raise ConfigurationError("max_steps must be >= 1")
        if self.retries < 1:
            raise ConfigurationError("retries must be >= 1 (1 means no retrying)")
        if self.approval not in APPROVAL_MODES:
            raise ConfigurationError(
                f"Unknown approval mode {self.approval!r}. "
                f"Expected one of: {', '.join(APPROVAL_MODES)}"
            )

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------
    @classmethod
    def from_env(
        cls,
        *,
        dotenv: str | os.PathLike[str] | None = ".env",
        use_settings_files: bool = True,
    ) -> Settings:
        """Build settings from the configuration layers.

        Lowest to highest precedence: built-in defaults, the user settings file,
        the project settings file, environment variables (including ``.env``).
        CLI flags are applied on top of the result by the caller.
        """
        if dotenv is not None:
            load_dotenv(dotenv)

        stored: dict[str, Any] = {}
        if use_settings_files:
            from jaigent.settings_store import load_layers

            stored = load_layers()

        def pick(env_var: str, key: str, fallback: str) -> str:
            """Environment beats the settings files, which beat the default."""
            raw = os.getenv(env_var)
            if raw not in (None, ""):
                return str(raw)
            return str(stored.get(key, fallback))

        def pick_int(env_var: str, key: str, fallback: int) -> int:
            return _env_int(env_var, int(stored.get(key, fallback)))

        def pick_float(env_var: str, key: str, fallback: float) -> float:
            return _env_float(env_var, float(stored.get(key, fallback)))

        def pick_flag(env_var: str, key: str, fallback: bool) -> bool:
            return _env_flag(env_var, bool(stored.get(key, fallback)))

        provider = str(pick("JAIGENT_PROVIDER", "provider", "openai")).strip().lower()
        if provider not in KNOWN_PROVIDERS:
            raise ConfigurationError(
                f"Unknown provider {provider!r}. Expected one of: {', '.join(KNOWN_PROVIDERS)}"
            )

        api_key = os.getenv("JAIGENT_API_KEY") or os.getenv(API_KEY_ENV_VARS[provider])
        # OmniRoute is commonly run locally with no auth at all.
        if not api_key and provider in LOCAL_PROVIDERS:
            api_key = os.getenv("OMNIROUTE_API_KEY") or "jaigent-local"

        base_url = (
            os.getenv("JAIGENT_BASE_URL")
            or (os.getenv("OMNIROUTE_BASE_URL") if provider == "omniroute" else None)
            or stored.get("base_url")
            or DEFAULT_BASE_URLS[provider]
        )
        workspace = os.getenv("JAIGENT_WORKSPACE") or str(Path.cwd())

        return cls(
            provider=provider,
            model=str(pick("JAIGENT_MODEL", "model", DEFAULT_MODELS[provider])),
            api_key=api_key,
            base_url=str(base_url),
            workspace=Path(workspace),
            max_steps=pick_int("JAIGENT_MAX_STEPS", "max_steps", 12),
            temperature=pick_float("JAIGENT_TEMPERATURE", "temperature", 0.2),
            max_tokens=pick_int("JAIGENT_MAX_TOKENS", "max_tokens", 2048),
            timeout=pick_float("JAIGENT_TIMEOUT", "timeout", 60.0),
            search_backend=str(pick("JAIGENT_SEARCH_BACKEND", "search_backend", "duckduckgo")),
            search_api_key=os.getenv("TAVILY_API_KEY"),
            allow_shell=pick_flag("JAIGENT_ALLOW_SHELL", "allow_shell", False),
            verbose=pick_flag("JAIGENT_VERBOSE", "verbose", False),
            stream=pick_flag("JAIGENT_STREAM", "stream", True),
            show_cost=pick_flag("JAIGENT_SHOW_COST", "show_cost", True),
            approval=str(pick("JAIGENT_APPROVAL", "approval", "auto")),
            skills_enabled=_env_flag("JAIGENT_SKILLS", bool(stored.get("skills_enabled", True))),
            checkpoints=pick_flag("JAIGENT_CHECKPOINTS", "checkpoints", True),
            failover=pick_flag("JAIGENT_FAILOVER", "failover", True),
            retries=pick_int("JAIGENT_RETRIES", "retries", 3),
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
        if self.provider in LOCAL_PROVIDERS:
            # A local gateway accepts anything; don't make the user invent one.
            return "jaigent-local"
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
            "skills_enabled": self.skills_enabled,
            "checkpoints": self.checkpoints,
            "failover": self.failover,
            "retries": self.retries,
        }
