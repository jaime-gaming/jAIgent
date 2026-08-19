"""Automatic model selection.

``--model auto`` sizes the model to the job: a one-line question does not need
the same engine as "refactor this package and write the tests". The router
scores a prompt, buckets it into a difficulty tier, and picks the cheapest model
in your configured provider that is strong enough.

The scoring is a transparent heuristic rather than a second LLM call, because
paying for a model to decide which model to use defeats the point. It is
deliberately easy to read, easy to override, and never fatal: an unroutable
prompt falls back to the provider's default model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from jaigent.models import CATALOGUE, ModelInfo


class Difficulty(str, Enum):
    """How much model a task appears to need."""

    #: Greetings, one-line factual questions, trivial lookups.
    SIMPLE = "simple"
    #: Ordinary work: summarise, search, edit a file.
    STANDARD = "standard"
    #: Multi-step reasoning, architecture, tricky debugging, long documents.
    COMPLEX = "complex"


#: Signals that a task is harder than it looks. Weighted; summed into a score.
COMPLEX_PATTERNS: tuple[tuple[str, int, str], ...] = (
    (r"\brefactor(ing|ed)?\b", 3, "refactoring"),
    (r"\barchitect(ure|ural)?\b", 3, "architecture"),
    (r"\bdesign\s+(a|an|the)\b", 2, "design work"),
    (r"\bdebug(ging)?\b", 2, "debugging"),
    (r"\bwhy\s+(is|does|isn'?t|doesn'?t|won'?t)\b", 2, "causal question"),
    (r"\bprove\b|\bderive\b|\bproof\b", 3, "proof"),
    (r"\boptimi[sz]e\b", 2, "optimisation"),
    (r"\bmigrat(e|ion)\b", 2, "migration"),
    (r"\bsecurity\s+(audit|review)\b", 4, "security review"),
    (r"\breview\s+(the|my|this)\b", 2, "code review"),
    (r"\bstep[- ]by[- ]step\b", 2, "step-by-step"),
    (r"\bcompare\s+.*\band\b", 2, "comparison"),
    (r"\btrade[- ]?offs?\b", 2, "trade-offs"),
    (r"\balgorithm\b|\bcomplexity\b", 2, "algorithmic"),
    (r"\bconcurren(t|cy)\b|\brace condition\b|\bdeadlock\b", 3, "concurrency"),
    (r"\bacross\s+(the\s+)?(whole|entire|all)\b", 2, "whole-codebase"),
    (r"\bevery\s+file\b|\ball\s+files\b", 2, "every file"),
    (r"\bthen\b.*\bthen\b", 2, "multi-step"),
    (r"\bplan\b.*\bimplement\b", 3, "plan and implement"),
    (r"\bwrite\s+(the\s+|all\s+|some\s+)*tests?\b", 2, "writing tests"),
    (r"\bfix\s+(the\s+)?(bug|failure|error)\b", 2, "bug fixing"),
)

#: Signals that a task is trivial.
SIMPLE_PATTERNS: tuple[tuple[str, int, str], ...] = (
    (r"^\s*(hi|hey|hello|yo|thanks|thank you|ok|okay)\b", -4, "greeting"),
    (r"^\s*what\s+(is|are)\s+\w+(\s+\w+)?\s*\??\s*$", -3, "short question"),
    (r"^\s*(list|show|print|cat|echo)\b", -2, "listing"),
    (r"^\s*(who|when|where)\s+(is|was|are)\b", -2, "factual lookup"),
    (r"\bhow many\b", -1, "counting"),
    (r"\bdefine\b|\bmeaning of\b", -2, "definition"),
)

#: Word-count thresholds contributing to the score.
LENGTH_STEPS: tuple[tuple[int, int], ...] = ((120, 3), (60, 2), (25, 1), (8, 0))

#: Score boundaries. Below the first is simple; at or above the second, complex.
SIMPLE_BELOW = 1
COMPLEX_AT = 4

#: Preferred models per provider and tier, cheapest-first within a tier.
#: Only ids that exist in :data:`jaigent.models.CATALOGUE` are used.
PREFERENCES: dict[str, dict[Difficulty, tuple[str, ...]]] = {
    "openai": {
        Difficulty.SIMPLE: ("gpt-4.1-nano", "gpt-4o-mini"),
        Difficulty.STANDARD: ("gpt-4o-mini", "gpt-4.1-mini"),
        Difficulty.COMPLEX: ("gpt-4o", "gpt-4.1", "o3-mini"),
    },
    "anthropic": {
        Difficulty.SIMPLE: ("claude-3-5-haiku-latest",),
        Difficulty.STANDARD: ("claude-3-5-sonnet-latest", "claude-3-7-sonnet-latest"),
        Difficulty.COMPLEX: ("claude-sonnet-4-20250514", "claude-opus-4-20250514"),
    },
    "gemini": {
        Difficulty.SIMPLE: ("gemini-2.0-flash-lite", "gemini-2.5-flash"),
        Difficulty.STANDARD: ("gemini-2.5-flash", "gemini-2.0-flash"),
        Difficulty.COMPLEX: ("gemini-2.5-pro",),
    },
    "deepseek": {
        Difficulty.SIMPLE: ("deepseek-chat",),
        Difficulty.STANDARD: ("deepseek-chat",),
        Difficulty.COMPLEX: ("deepseek-reasoner",),
    },
    "xai": {
        Difficulty.SIMPLE: ("grok-3-mini",),
        Difficulty.STANDARD: ("grok-3", "grok-2-latest"),
        Difficulty.COMPLEX: ("grok-4", "grok-3"),
    },
    "groq": {
        Difficulty.SIMPLE: ("llama-3.1-8b-instant",),
        Difficulty.STANDARD: ("llama-3.3-70b-versatile",),
        Difficulty.COMPLEX: ("llama-3.3-70b-versatile",),
    },
    "openrouter": {
        Difficulty.SIMPLE: ("openai/gpt-4o-mini",),
        Difficulty.STANDARD: ("openai/gpt-4o-mini", "deepseek/deepseek-chat"),
        Difficulty.COMPLEX: ("anthropic/claude-sonnet-4", "google/gemini-2.5-pro"),
    },
    "mistral": {
        Difficulty.SIMPLE: ("mistral-small-latest",),
        Difficulty.STANDARD: ("mistral-small-latest",),
        Difficulty.COMPLEX: ("mistral-large-latest",),
    },
    "together": {
        Difficulty.SIMPLE: ("meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",),
        Difficulty.STANDARD: ("meta-llama/Llama-3.3-70B-Instruct-Turbo",),
        Difficulty.COMPLEX: ("meta-llama/Llama-3.3-70B-Instruct-Turbo",),
    },
    "ollama": {
        Difficulty.SIMPLE: ("llama3.1:8b",),
        Difficulty.STANDARD: ("qwen2.5:14b",),
        Difficulty.COMPLEX: ("qwen2.5:14b",),
    },
}

#: Free-tier models per provider, cheapest-first within a tier. Used by
#: ``--model free`` to pick across every provider you actually have a key for.
FREE_PREFERENCES: dict[str, dict[Difficulty, tuple[str, ...]]] = {
    "ollama": {
        Difficulty.SIMPLE: ("llama3.1:8b",),
        Difficulty.STANDARD: ("qwen2.5:14b",),
        Difficulty.COMPLEX: ("qwen2.5:14b",),
    },
    "groq": {
        Difficulty.SIMPLE: ("llama-3.1-8b-instant",),
        Difficulty.STANDARD: ("llama-3.3-70b-versatile",),
        Difficulty.COMPLEX: ("llama-3.3-70b-versatile",),
    },
    "gemini": {
        Difficulty.SIMPLE: ("gemini-2.0-flash-lite", "gemini-2.5-flash"),
        Difficulty.STANDARD: ("gemini-2.5-flash", "gemini-2.0-flash"),
        Difficulty.COMPLEX: ("gemini-2.5-flash",),
    },
    "openrouter": {
        Difficulty.SIMPLE: ("qwen/qwen3-8b:free", "google/gemma-3-27b-it:free"),
        Difficulty.STANDARD: (
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemma-3-27b-it:free",
        ),
        Difficulty.COMPLEX: ("meta-llama/llama-3.3-70b-instruct:free",),
    },
}

#: Order ``--model free`` walks usable providers: local first, then free tiers.
FREE_PROVIDER_ORDER = ("ollama", "groq", "gemini", "openrouter")


@dataclass(slots=True, frozen=True)
class Routing:
    """The router's decision, with its reasoning."""

    model: str
    difficulty: Difficulty
    score: int
    reason: str
    #: Set when routing also picked the provider (``--model free``).
    provider: str = ""

    def summary(self) -> str:
        via = f" via {self.provider}" if self.provider else ""
        return f"auto -> {self.model}{via} ({self.difficulty.value}, score {self.score})"


def score_prompt(prompt: str) -> tuple[int, list[str]]:
    """Score a prompt's apparent difficulty, with the signals that fired."""
    text = (prompt or "").strip()
    if not text:
        return 0, []

    lowered = text.lower()
    score = 0
    reasons: list[str] = []

    words = len(text.split())
    for threshold, points in LENGTH_STEPS:
        if words >= threshold:
            if points:
                score += points
                reasons.append(f"{words} words")
            break

    for pattern, weight, label in COMPLEX_PATTERNS:
        if re.search(pattern, lowered):
            score += weight
            reasons.append(label)

    for pattern, weight, label in SIMPLE_PATTERNS:
        if re.search(pattern, lowered):
            score += weight
            reasons.append(label)
            break

    # A code fence or a stack trace almost always means real work.
    if "```" in text or "Traceback (most recent call last)" in text:
        score += 2
        reasons.append("contains code")

    # Several questions in one prompt is a multi-part task.
    questions = text.count("?")
    if questions >= 2:
        score += 1
        reasons.append(f"{questions} questions")

    return max(0, score), reasons


def classify(prompt: str) -> tuple[Difficulty, int, list[str]]:
    """Bucket a prompt into a difficulty tier."""
    score, reasons = score_prompt(prompt)
    if score < SIMPLE_BELOW:
        return Difficulty.SIMPLE, score, reasons
    if score >= COMPLEX_AT:
        return Difficulty.COMPLEX, score, reasons
    return Difficulty.STANDARD, score, reasons


def _known_ids() -> set[str]:
    return {model.id for model in CATALOGUE}


def choose_model(
    prompt: str,
    provider: str,
    *,
    fallback: str = "",
    available: set[str] | None = None,
) -> Routing:
    """Pick the best model in ``provider`` for ``prompt``.

    Falls back to ``fallback`` when the provider has no preference table or none
    of its preferred ids are known, so routing can never leave you without a
    model to call.
    """
    difficulty, score, reasons = classify(prompt)
    table = PREFERENCES.get(provider.strip().lower())
    catalogue = available if available is not None else _known_ids()

    chosen = ""
    if table:
        for candidate in table.get(difficulty, ()):  # cheapest acceptable first
            if candidate in catalogue:
                chosen = candidate
                break
        if not chosen:
            # The tier had nothing usable; try any tier for this provider.
            for tier in (Difficulty.STANDARD, Difficulty.COMPLEX, Difficulty.SIMPLE):
                for candidate in table.get(tier, ()):
                    if candidate in catalogue:
                        chosen = candidate
                        break
                if chosen:
                    break

    if not chosen:
        chosen = fallback
        reasons.append("no routing table for this provider")

    detail = ", ".join(reasons[:3]) if reasons else "short, simple request"
    return Routing(model=chosen, difficulty=difficulty, score=score, reason=detail)


def choose_free_model(
    prompt: str,
    *,
    usable: Sequence[str] | None = None,
    fallback_provider: str = "",
    fallback_model: str = "",
) -> Routing:
    """Pick a no-cost model from a provider the user can actually reach.

    Walks :data:`FREE_PROVIDER_ORDER`, skipping names not in ``usable``.
    Falls back to ``fallback_model`` on ``fallback_provider`` when nothing
    free is configured.
    """
    difficulty, score, reasons = classify(prompt)
    catalogue = _known_ids()
    wanted = {name.strip().lower() for name in (usable or ()) if name}

    for provider in FREE_PROVIDER_ORDER:
        if wanted and provider not in wanted:
            continue
        table = FREE_PREFERENCES.get(provider)
        if not table:
            continue
        chosen = ""
        for candidate in table.get(difficulty, ()):
            if candidate in catalogue:
                chosen = candidate
                break
        if not chosen:
            for tier in (Difficulty.STANDARD, Difficulty.COMPLEX, Difficulty.SIMPLE):
                for candidate in table.get(tier, ()):
                    if candidate in catalogue:
                        chosen = candidate
                        break
                if chosen:
                    break
        if chosen:
            detail = ", ".join(reasons[:3]) if reasons else "short, simple request"
            return Routing(
                model=chosen,
                difficulty=difficulty,
                score=score,
                reason=detail,
                provider=provider,
            )

    reasons.append("no free provider available")
    detail = ", ".join(reasons[:3]) if reasons else "no free provider available"
    return Routing(
        model=fallback_model,
        difficulty=difficulty,
        score=score,
        reason=detail,
        provider=fallback_provider,
    )


def explain(prompt: str, provider: str, *, fallback: str = "") -> str:
    """A human-readable account of what the router would do and why."""
    routing = choose_model(prompt, provider, fallback=fallback)
    return (
        f"{routing.difficulty.value} (score {routing.score}: {routing.reason}) -> {routing.model}"
    )


def models_for(provider: str) -> list[ModelInfo]:
    """Catalogue entries the router may pick for ``provider``."""
    table = PREFERENCES.get(provider.strip().lower(), {})
    wanted = {model_id for tier in table.values() for model_id in tier}
    return [model for model in CATALOGUE if model.id in wanted]
