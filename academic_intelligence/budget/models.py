"""Budget domain models: specs, runtime state, decisions, and status.

Defines the vocabulary of the WP5 budget layer:

- :class:`BudgetSpec` — immutable per-source quota definition (limit, unit,
  period, semantics).
- :class:`Budget` — mutable per-source runtime state (used, current period
  bucket, circuit-breaker flag).
- :class:`BudgetDecision` — result of ``check`` / ``consume`` /
  ``report_failure`` (fail-soft: never raised).
- :class:`BudgetStatus` — snapshot for ``status()`` / CLI rendering.
- :class:`BudgetEvent` — bookkeeping event for fail-soft reporting.
- :func:`period_key_for` — period bucket key for ``"day"`` (UTC calendar
  day) and ``"<N>s"`` (aligned natural window) specs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "Budget",
    "BudgetDecision",
    "BudgetEvent",
    "BudgetKind",
    "BudgetSpec",
    "BudgetStatus",
    "period_key_for",
]

# Units that denote a cost-based (USD/credit) budget.  Anything else
# (``"req"``, ``"rps"``, ...) is a request-count budget.
_USD_UNITS = frozenset({"usd", "credit"})

# Period spec: either the literal ``"day"`` or ``"<N>s"`` (N seconds).
_PERIOD_RE = re.compile(r"^(\d+)s$")


class BudgetKind(StrEnum):
    """Semantics of a source budget (design §1.4, I6 decision).

    ``PRECHECK``: req/rps-class sources (s2 / crossref / arxiv) are gated
    *before* the request — ``check`` refuses once ``used >= limit``.

    ``METERED``: USD/credit-class sources (openalex) are metered *after* the
    response — a single request's cost is not knowable up front, so usage is
    accumulated locally and the source is tripped to ``quota_exhausted`` on
    a billing/rate-limit error signal or once accumulated usage reaches the
    limit (recovered at the next period bucket).
    """

    PRECHECK = "precheck"
    METERED = "metered"


class BudgetSpec(BaseModel):
    """Immutable definition of one source's budget.

    Attributes:
        source: Source identifier (e.g. ``"openalex"``,
            ``"semantic_scholar"``, ``"crossref"``, ``"arxiv"``).
        limit: Usage ceiling in ``unit`` units (e.g. 100 requests, 1.0 USD).
            ``0`` is allowed and means "disabled": the source is denied
            fail-soft at every ``check`` (no requests are sent) until the
            limit is raised or the source is reconfigured — a config-level
            kill switch (IM-3 acceptance: a source pinned to 0 is skipped
            and the collector falls back to the other sources).
        unit: Usage unit — ``"req"`` for request counts, ``"usd"`` /
            ``"credit"`` for cost-based budgets.
        period: Period bucket spec — ``"day"`` (UTC calendar day) or
            ``"<N>s"`` (N-second aligned natural window, e.g. ``"300s"``).
        semantics: :class:`BudgetKind`; when omitted it is derived from
            ``unit`` (USD/credit → metered, everything else → precheck).
    """

    source: str = Field(min_length=1)
    limit: float = Field(ge=0)
    unit: str = Field(min_length=1)
    period: str = Field(min_length=1)
    semantics: BudgetKind | None = None

    @field_validator("period")
    @classmethod
    def _validate_period(cls, v: str) -> str:
        if v == "day" or _PERIOD_RE.fullmatch(v):
            return v
        raise ValueError(f"period must be 'day' or '<N>s', got {v!r}")

    @property
    def effective_semantics(self) -> BudgetKind:
        """Resolve the semantics (explicit value or derived from unit)."""
        if self.semantics is not None:
            return self.semantics
        return BudgetKind.METERED if self.unit in _USD_UNITS else BudgetKind.PRECHECK


@dataclass
class Budget:
    """Mutable per-source runtime state.

    Not meant to be constructed by callers — :class:`BudgetManager` builds
    one per :class:`BudgetSpec`.  Fields are mutated only under the
    manager's per-source lock.
    """

    source: str
    limit: float
    unit: str
    period: str
    semantics: BudgetKind
    used: float = 0.0
    period_key: str = ""
    quota_exhausted: bool = False
    exhausted_at: datetime | None = None


@dataclass(frozen=True)
class BudgetDecision:
    """Result of a ``check`` / ``consume`` / ``report_failure`` call.

    Fail-soft contract: an over-limit source is reported through this object
    (``allowed=False``) — it is never raised as a fatal error, so collectors
    skip the source and fall back to the others.

    Attributes:
        source: Source identifier the decision applies to.
        allowed: Whether the source may send a request now.
        reason: ``"ok"`` | ``"no_budget"`` | ``"budget_exhausted"`` |
            ``"quota_exhausted"`` | ``"breaker_tripped"``.
        remaining: Remaining quota in ``unit`` units; ``inf`` for sources
            without a configured budget, ``0.0`` when denied.
        period_key: Current period bucket key (``""`` for no-budget).
        unit: Usage unit (``""`` for no-budget).
        period: Period spec (``""`` for no-budget).
    """

    source: str
    allowed: bool
    reason: str
    remaining: float
    period_key: str
    unit: str
    period: str

    @property
    def exhausted(self) -> bool:
        """True when the source was denied (fail-soft skip)."""
        return not self.allowed


@dataclass(frozen=True)
class BudgetStatus:
    """Snapshot of one source's budget for ``status()`` / CLI rendering.

    Only configured sources are listed; sources without a quota (web
    crawls, on-demand sources) are absent.
    """

    source: str
    limit: float
    used: float
    remaining: float
    unit: str
    period: str
    period_key: str
    semantics: BudgetKind
    quota_exhausted: bool


@dataclass(frozen=True)
class BudgetEvent:
    """A budget bookkeeping event, kept for fail-soft reporting.

    ``kind`` is one of ``denied`` / ``breaker_tripped`` /
    ``rate_limit_signal`` / ``period_rolled`` / ``store_error``.
    """

    source: str
    kind: str
    detail: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


def period_key_for(period: str, now: datetime) -> str:
    """Return the period bucket key covering *now* for a period spec.

    ``"day"`` budgets roll at the UTC calendar boundary (``YYYY-MM-DD``);
    ``"<N>s"`` budgets use wall-clock aligned natural windows whose bucket
    start time is formatted ``YYYY-MM-DDTHH:MM:SSZ`` (restart-stable and
    lexicographically sortable).  A naive *now* is treated as UTC.
    """
    utc = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    if period == "day":
        return utc.strftime("%Y-%m-%d")
    match = _PERIOD_RE.fullmatch(period)
    if match is None:
        raise ValueError(f"invalid period spec: {period!r}")
    seconds = int(match.group(1))
    epoch = int(utc.timestamp())
    bucket_start = datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)
    return bucket_start.strftime("%Y-%m-%dT%H:%M:%SZ")
