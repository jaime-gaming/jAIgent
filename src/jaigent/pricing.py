"""Token accounting and cost estimation.

Agents make many model calls per task, and the bill is invisible until it
arrives. jaigent prints an estimate after every run so the number is never a
surprise.

Prices are USD per million tokens and are **estimates**: they are baked in at
release time, vary by region and tier, and change without notice. Override them
with a JSON file (``JAIGENT_PRICES``) mapping model names to
``{"input": <usd_per_mtok>, "output": <usd_per_mtok>}`` when you need exact
figures.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

#: USD per million tokens, as (input, output). Keys are matched as prefixes,
#: longest first, so "gpt-4o-mini" wins over "gpt-4o" for gpt-4o-mini-2024-07-18.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o3-mini": (1.10, 4.40),
    "o1-mini": (1.10, 4.40),
    "o1": (15.00, 60.00),
    # Google Gemini
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    # xAI
    "grok-4": (3.00, 15.00),
    "grok-3-mini": (0.30, 0.50),
    "grok-3": (3.00, 15.00),
    "grok-2": (2.00, 10.00),
    # DeepSeek
    "deepseek-reasoner": (0.55, 2.19),
    "deepseek-chat": (0.27, 1.10),
    # Anthropic
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3-opus": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-haiku-4": (1.00, 5.00),
}


@dataclass(slots=True, frozen=True)
class Cost:
    """A token count and its estimated price."""

    input_tokens: int = 0
    output_tokens: int = 0
    usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def format_tokens(self) -> str:
        """``1,240 tokens (980 in / 260 out)``."""
        if not self.total_tokens:
            return "no token usage reported"
        if self.input_tokens and self.output_tokens:
            return (
                f"{self.total_tokens:,} tokens "
                f"({self.input_tokens:,} in / {self.output_tokens:,} out)"
            )
        return f"{self.total_tokens:,} tokens"

    def format_usd(self) -> str:
        """Money, with enough decimals to be meaningful at agent scale."""
        if self.usd is None:
            return ""
        if self.usd == 0:
            return "$0.00"
        if self.usd < 0.01:
            return f"${self.usd:.4f}"
        return f"${self.usd:.2f}"

    def summary(self) -> str:
        """``1,240 tokens (980 in / 260 out) · ~$0.0043``."""
        parts = [self.format_tokens()]
        money = self.format_usd()
        if money:
            parts.append(f"~{money}")
        return " · ".join(parts)


def load_price_overrides(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, tuple[float, float]]:
    """Load a price table from JSON, if one is configured.

    The file maps model name prefixes to ``{"input": x, "output": y}`` in USD
    per million tokens. A malformed file is ignored rather than fatal — a wrong
    cost estimate must never stop the agent from running.
    """
    location = path or os.getenv("JAIGENT_PRICES")
    if not location:
        return {}

    file = Path(location).expanduser()
    if not file.is_file():
        return {}

    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    overrides: dict[str, tuple[float, float]] = {}
    if not isinstance(raw, dict):
        return {}
    for model, prices in raw.items():
        if isinstance(prices, dict) and "input" in prices and "output" in prices:
            try:
                overrides[str(model)] = (float(prices["input"]), float(prices["output"]))
            except (TypeError, ValueError):
                continue
    return overrides


def price_for(
    model: str, *, overrides: dict[str, tuple[float, float]] | None = None
) -> tuple[float, float] | None:
    """Return ``(input, output)`` USD per million tokens for ``model``.

    Matching is by longest prefix, so dated snapshots such as
    ``gpt-4o-mini-2024-07-18`` resolve to the ``gpt-4o-mini`` entry. Returns
    ``None`` for unknown models, in which case no price is shown.
    """
    if not model:
        return None
    table = {**DEFAULT_PRICES, **(overrides or {})}
    name = model.strip().lower()
    # Strip a provider prefix like "openai/" or "anthropic/" used by gateways.
    if "/" in name:
        name = name.split("/", 1)[1]

    best: tuple[float, float] | None = None
    best_len = -1
    for key, prices in table.items():
        if name.startswith(key.lower()) and len(key) > best_len:
            best, best_len = prices, len(key)
    return best


def estimate(
    model: str,
    usage: dict[str, int],
    *,
    overrides: dict[str, tuple[float, float]] | None = None,
) -> Cost:
    """Turn a provider usage dict into a :class:`Cost`.

    Handles both provider vocabularies: OpenAI reports ``prompt_tokens`` and
    ``completion_tokens``, Anthropic reports ``input_tokens`` and
    ``output_tokens``.
    """
    usage = usage or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    total = int(usage.get("total_tokens") or 0)
    if total and not (input_tokens or output_tokens):
        # Some gateways only report a total; attribute it all to input.
        input_tokens = total

    prices = price_for(model, overrides=overrides)
    usd = None
    if prices is not None:
        usd = (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000

    return Cost(input_tokens=input_tokens, output_tokens=output_tokens, usd=usd)
