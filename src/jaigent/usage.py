"""Spend reporting over the sessions that are actually saved.

Every turn already records its token usage on the session file, and
:mod:`jaigent.pricing` already knows what those tokens cost. Nothing added
them up, so "what have I spent this week?" meant opening JSON files by hand.

This module only reads: the session store stays the source of truth, no
counter is kept anywhere else, and no network call is made. A row without a
known price reports its tokens and leaves the cost blank rather than
pretending it was free — :func:`jaigent.pricing.price_for` returns ``None``
for a model it has never heard of, and a made-up number would be worse than
no number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from jaigent.pricing import Cost, estimate
from jaigent.session import Session, list_sessions

_PERIODS = {"day": 1, "week": 7, "month": 30}


@dataclass(slots=True)
class UsageRow:
    """One session's contribution to the totals."""

    session: Session
    cost: Cost
    #: False when the model has no entry in the price table.
    priced: bool

    @property
    def tokens(self) -> int:
        return self.cost.total_tokens

    @property
    def updated(self) -> datetime:
        return datetime.fromtimestamp(self.session.updated, tz=timezone.utc).astimezone()


@dataclass(slots=True)
class UsageReport:
    """Totals for a period, plus the rows that produced them."""

    rows: list[UsageRow] = field(default_factory=list)
    #: Sessions inside the period whose model has no known price.
    unpriced: int = 0

    @property
    def input_tokens(self) -> int:
        return sum(row.cost.input_tokens for row in self.rows)

    @property
    def output_tokens(self) -> int:
        return sum(row.cost.output_tokens for row in self.rows)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def usd(self) -> float:
        """Known spend. Sessions without a price contribute nothing — see ``unpriced``."""
        return sum(row.cost.usd or 0.0 for row in self.rows)

    @property
    def turns(self) -> int:
        return sum(row.session.turns for row in self.rows)

    def by_model(self) -> list[tuple[str, int, float]]:
        """``(model, tokens, usd)`` per model, costliest first."""
        buckets: dict[str, list[float]] = {}
        for row in self.rows:
            bucket = buckets.setdefault(row.session.model or "unknown", [0.0, 0.0])
            bucket[0] += row.tokens
            bucket[1] += row.cost.usd or 0.0
        return [
            (model, int(tokens), usd)
            for model, (tokens, usd) in sorted(
                buckets.items(), key=lambda item: (item[1][1], item[1][0]), reverse=True
            )
        ]


def parse_period(value: str | None) -> timedelta | None:
    """Turn ``day``/``week``/``month`` into a window. ``all`` means no window.

    Raises:
        ValueError: for anything else, so a typo is refused instead of being
            silently treated as "everything" and reporting the wrong total.
    """
    if value in (None, "", "all"):
        return None
    if value in _PERIODS:
        return timedelta(days=_PERIODS[value])
    raise ValueError(f"Unknown period {value!r}. Use one of: {', '.join([*_PERIODS, 'all'])}.")


def build_report(
    period: str | None = None,
    *,
    now: float | None = None,
    sessions: list[Session] | None = None,
) -> UsageReport:
    """Aggregate saved usage inside ``period``.

    Args:
        period: ``day``, ``week``, ``month`` or ``all``/``None``.
        now: Wall-clock override for tests; defaults to :func:`time.time`.
        sessions: The sessions to consider; defaults to every saved one. The
            listing is injectable so this can be tested without a config home.
    """
    window = parse_period(period)
    moment = time.time() if now is None else now
    cutoff = moment - window.total_seconds() if window is not None else None

    report = UsageReport()
    for session in sessions if sessions is not None else list_sessions():
        # A session counts by when it was last touched: that is when the
        # tokens were spent, and `created` would attribute a long-running
        # chat entirely to the day it started.
        if cutoff is not None and session.updated < cutoff:
            continue
        cost = estimate(session.model, session.usage)
        priced = cost.usd is not None
        if not priced:
            report.unpriced += 1
        report.rows.append(UsageRow(session=session, cost=cost, priced=priced))

    report.rows.sort(key=lambda row: row.session.updated, reverse=True)
    return report


def day_starts(report: UsageReport, days: int = 7, *, now: float | None = None) -> list[tuple]:
    """Tokens per calendar day, oldest first, for a small sparkline or table.

    Returns ``(date, tokens, usd)`` tuples. Days with no activity are included
    as zero so the series has no gaps.
    """
    moment = datetime.fromtimestamp(time.time() if now is None else now).astimezone()
    first = (moment - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    buckets: dict[str, list[float]] = {
        (first + timedelta(days=offset)).strftime("%Y-%m-%d"): [0.0, 0.0, 0.0]
        for offset in range(days)
    }
    for row in report.rows:
        stamp = row.updated.strftime("%Y-%m-%d")
        if stamp in buckets:
            buckets[stamp][0] += row.tokens
            buckets[stamp][1] += row.cost.usd or 0.0
            buckets[stamp][2] += 1

    return [
        (stamp, int(tokens), usd, int(count))
        for stamp, (tokens, usd, count) in sorted(buckets.items())
    ]
