"""BudgetManager: per-source quota enforcement with fail-soft semantics.

Design baseline: ``docs/upgrade/technical-design.md`` §1.4 (tiered
semantics, I6 decision) and §2 (``budget_usage`` table).  Two independent
classes of enforcement:

- **req/rps-class** sources (s2 / crossref / arxiv): **pre-check** —
  :meth:`BudgetManager.check` refuses before a request is sent once
  ``used >= limit`` in the current period bucket;
- **USD/credit-class** sources (openalex): **post-metering + threshold
  circuit breaker** — a single request's cost is not knowable up front, so
  :meth:`BudgetManager.consume` accumulates a locally-estimated cost and a
  billing/rate-limit error signal fed through :meth:`BudgetManager.report_failure`
  (HTTP 402/429, or a billing/quota signal) trips the breaker
  (``quota_exhausted``) until the next UTC day boundary.

Periods roll lazily on first use of a new bucket (UTC day boundary for
USD-class, aligned natural windows for req-class).  Over-limit behaviour is
fail-soft: denied sources are reported through :class:`BudgetDecision`
(never raised) so the collector skips the source and falls back to the
others; the recent :class:`BudgetEvent` log is surfaced through
:meth:`BudgetManager.pop_events` for reporting.

This layer is independent from (and layered on top of) ``utils/RateLimiter``
(M16): the global rate layer paces requests, this layer enforces per-source
quotas.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from academic_intelligence.budget.models import (
    Budget,
    BudgetDecision,
    BudgetEvent,
    BudgetKind,
    BudgetSpec,
    BudgetStatus,
    period_key_for,
)
from academic_intelligence.budget.store import BudgetStore, MemoryBudgetStore

__all__ = [
    "BudgetManager",
    "DEFAULT_BUDGETS",
]

logger = logging.getLogger(__name__)

# Default per-source budgets (design §1.4).  ``semantic_scholar`` is the
# project's identifier for "s2"; web sources carry no quota (their polite
# constraints live inside the webcrawler).
DEFAULT_BUDGETS: tuple[BudgetSpec, ...] = (
    BudgetSpec(source="openalex", limit=1.0, unit="usd", period="day"),
    BudgetSpec(source="semantic_scholar", limit=100.0, unit="req", period="300s"),
    BudgetSpec(source="crossref", limit=3.0, unit="req", period="1s"),
    BudgetSpec(source="arxiv", limit=1.0, unit="req", period="3s"),
)

# Error signals that trip the metered-class circuit breaker (I6): HTTP 402
# Payment Required (OpenAlex free-tier exhaustion) and 429 (rate limit),
# plus explicit billing/quota signal strings.
_TRIP_HTTP_STATUSES = frozenset({402, 429})
_TRIP_SIGNALS = frozenset(
    {
        "rate_limited",
        "rate_limit",
        "billing",
        "billing_error",
        "quota_exceeded",
        "quota_exhausted",
    }
)

_REASON_OK = "ok"
_REASON_NO_BUDGET = "no_budget"
_REASON_BUDGET_EXHAUSTED = "budget_exhausted"
_REASON_QUOTA_EXHAUSTED = "quota_exhausted"
_REASON_BREAKER_TRIPPED = "breaker_tripped"

_MAX_EVENTS = 1000


class BudgetManager:
    """Per-source quota enforcement (WP5)."""

    def __init__(
        self,
        budgets: Sequence[BudgetSpec] | None = None,
        store: BudgetStore | None = None,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize the manager.

        Args:
            budgets: Per-source budget definitions; defaults to
                :data:`DEFAULT_BUDGETS` when omitted.  Sources not listed
                here are unquoted (allowed, ``no_budget``).
            store: Persistence backend for ``budget_usage`` rows
                (``SQLiteStorage`` or any :class:`BudgetStore`); an
                in-memory store is used when omitted.
            now_fn: Injectable clock for period rollover (tests); defaults
                to ``datetime.now(UTC)``.
        """
        self._store = store if store is not None else MemoryBudgetStore()
        self._now = now_fn if now_fn is not None else (lambda: datetime.now(UTC))
        specs = list(budgets) if budgets is not None else list(DEFAULT_BUDGETS)
        self._budgets: dict[str, Budget] = {
            spec.source: Budget(
                source=spec.source,
                limit=spec.limit,
                unit=spec.unit,
                period=spec.period,
                semantics=spec.effective_semantics,
            )
            for spec in specs
        }
        self._locks: dict[str, asyncio.Lock] = {}
        self._events: list[BudgetEvent] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(self, source: str) -> BudgetDecision:
        """Pre-flight check: may *source* send a request now?

        - Unconfigured sources (web crawls, on-demand sources): allowed,
          ``reason="no_budget"`` — no quota to enforce.
        - Zero-limit budgets (``limit <= 0``): always denied
          (``budget_exhausted``) for both semantics — a config-level kill
          switch that lets a source be skipped without removing it from the
          registry (IM-3).
        - req-class (precheck): allowed while ``used < limit`` in the
          current period; otherwise denied (``budget_exhausted``).
        - USD-class (metered): allowed unless the circuit breaker is tripped
          (``quota_exhausted``), which recovers at the next period bucket.
        """
        budget = self._budgets.get(source)
        if budget is None:
            return self._no_budget_decision(source)
        async with self._lock_for(source):
            key = await self._ensure_current_period(budget)
            if budget.limit <= 0:
                # A zero-limit budget is a config-level kill switch: the
                # source is denied fail-soft at every pre-flight check for
                # BOTH semantics (precheck and metered), so no request is
                # ever sent (IM-3 acceptance: a source pinned to 0 is
                # skipped and the collector falls back to the others).
                self._log(source, "denied", _REASON_BUDGET_EXHAUSTED)
                return self._decision(
                    source,
                    budget,
                    allowed=False,
                    reason=_REASON_BUDGET_EXHAUSTED,
                    period_key=key,
                )
            if budget.semantics is BudgetKind.METERED and budget.quota_exhausted:
                self._log(source, "denied", _REASON_QUOTA_EXHAUSTED)
                return self._decision(
                    source,
                    budget,
                    allowed=False,
                    reason=_REASON_QUOTA_EXHAUSTED,
                    period_key=key,
                )
            if budget.semantics is BudgetKind.PRECHECK and budget.used >= budget.limit:
                self._log(source, "denied", _REASON_BUDGET_EXHAUSTED)
                return self._decision(
                    source,
                    budget,
                    allowed=False,
                    reason=_REASON_BUDGET_EXHAUSTED,
                    period_key=key,
                )
            return self._decision(source, budget, allowed=True, reason=_REASON_OK, period_key=key)

    async def consume(self, source: str, cost: float = 1.0) -> BudgetDecision:
        """Record usage for *source* after a request (post-metering).

        For USD-class sources *cost* is the locally-estimated cost of the
        response (single-request cost is not knowable up front).  Accumulated
        usage reaching the limit trips the circuit breaker (fail-soft) until
        the next period bucket.  Unconfigured sources are a no-op.

        Raises:
            ValueError: When *cost* is negative.
        """
        if cost < 0:
            raise ValueError("cost must be >= 0")
        budget = self._budgets.get(source)
        if budget is None:
            return self._no_budget_decision(source)
        async with self._lock_for(source):
            key = await self._ensure_current_period(budget)
            budget.used += cost
            try:
                await self._store.save_budget_usage(source, key, budget.used, budget.unit)
            except Exception as exc:  # fail-soft: keep the local estimate
                logger.warning("budget store write failed for %s/%s: %s", source, key, exc)
                self._log(source, "store_error", str(exc))
            if (
                budget.semantics is BudgetKind.METERED
                and budget.used >= budget.limit
                and not budget.quota_exhausted
            ):
                budget.quota_exhausted = True
                budget.exhausted_at = self._now()
                self._log(source, "breaker_tripped", "used >= limit")
            if budget.quota_exhausted:
                return self._decision(
                    source,
                    budget,
                    allowed=False,
                    reason=_REASON_QUOTA_EXHAUSTED,
                    period_key=key,
                )
            return self._decision(source, budget, allowed=True, reason=_REASON_OK, period_key=key)

    async def report_failure(
        self,
        source: str,
        *,
        http_status: int | None = None,
        signal: str | None = None,
    ) -> BudgetDecision:
        """Feed a response error signal into the budget layer.

        USD-class sources trip the circuit breaker (``quota_exhausted``,
        fail-soft until the next UTC day boundary) on billing/rate-limit
        signals — HTTP 402/429 or a billing/quota signal string.  req-class
        sources only record the signal (the RateLimiter handles pacing); the
        circuit breaker is scoped to USD/credit semantics (I6).
        """
        budget = self._budgets.get(source)
        if budget is None:
            return self._no_budget_decision(source)
        async with self._lock_for(source):
            key = await self._ensure_current_period(budget)
            trips = budget.semantics is BudgetKind.METERED and (
                (http_status is not None and http_status in _TRIP_HTTP_STATUSES)
                or (signal is not None and signal.lower() in _TRIP_SIGNALS)
            )
            detail = f"http_status={http_status}, signal={signal!r}"
            if trips:
                budget.quota_exhausted = True
                budget.exhausted_at = self._now()
                self._log(source, "breaker_tripped", detail)
                return self._decision(
                    source,
                    budget,
                    allowed=False,
                    reason=_REASON_BREAKER_TRIPPED,
                    period_key=key,
                )
            if http_status is not None or signal is not None:
                self._log(source, "rate_limit_signal", detail)
            return self._decision(source, budget, allowed=True, reason=_REASON_OK, period_key=key)

    async def status(self) -> list[BudgetStatus]:
        """Per-source quota snapshot for the current period.

        Rendered by the ``paper budget`` / ``paper sources status`` CLI.
        Only configured sources are listed; sources without a quota are
        absent.
        """
        statuses: list[BudgetStatus] = []
        for source in sorted(self._budgets):
            budget = self._budgets[source]
            async with self._lock_for(source):
                key = await self._ensure_current_period(budget)
                statuses.append(
                    BudgetStatus(
                        source=source,
                        limit=budget.limit,
                        used=budget.used,
                        remaining=(
                            0.0 if budget.quota_exhausted else max(budget.limit - budget.used, 0.0)
                        ),
                        unit=budget.unit,
                        period=budget.period,
                        period_key=key,
                        semantics=budget.semantics,
                        quota_exhausted=budget.quota_exhausted,
                    )
                )
        return statuses

    def pop_events(self) -> list[BudgetEvent]:
        """Return and clear the recorded budget events (fail-soft reporting)."""
        events = self._events
        self._events = []
        return events

    def semantics_for(self, source: str) -> BudgetKind | None:
        """Return the enforcement semantics of *source*, or ``None``.

        ``None`` means the source carries no configured budget (unquoted —
        always allowed, ``no_budget``).  Synchronous: the semantics is a
        spec-derived constant that never changes with the period bucket.
        Collectors use it to pick the right post-request cost scale:
        req-class sources consume 1.0 per request, USD/credit-class sources
        consume a locally-estimated per-request cost (design §1.4).
        """
        budget = self._budgets.get(source)
        return budget.semantics if budget is not None else None

    @asynccontextmanager
    async def request_guard(self, source: str, cost: float = 1.0) -> AsyncIterator[BudgetDecision]:
        """Collector integration helper (layered over RateLimiter).

        Entry runs the pre-flight :meth:`check` and yields its decision; on
        exit (including exceptions) the default *cost* is recorded via
        :meth:`consume` when the request was allowed.  This is the natural
        shape for req-class sources (one request, cost 1).  For USD-class
        sources the estimated cost is only known after the response, so call
        :meth:`consume` with the estimate and :meth:`report_failure` on
        error signals explicitly instead of using the guard.
        """
        decision = await self.check(source)
        try:
            yield decision
        finally:
            if decision.allowed:
                await self.consume(source, cost)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lock_for(self, source: str) -> asyncio.Lock:
        """Return the per-source lock (atomic in the single-threaded loop)."""
        lock = self._locks.get(source)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[source] = lock
        return lock

    def _log(self, source: str, kind: str, detail: str = "") -> None:
        """Record an event, capping the log to ``_MAX_EVENTS`` entries."""
        self._events.append(BudgetEvent(source=source, kind=kind, detail=detail))
        if len(self._events) > _MAX_EVENTS:
            self._events = self._events[-_MAX_EVENTS:]

    def _no_budget_decision(self, source: str) -> BudgetDecision:
        """Decision for an unconfigured source: allowed, no quota (inf)."""
        return BudgetDecision(
            source=source,
            allowed=True,
            reason=_REASON_NO_BUDGET,
            remaining=float("inf"),
            period_key="",
            unit="",
            period="",
        )

    def _decision(
        self,
        source: str,
        budget: Budget,
        *,
        allowed: bool,
        reason: str,
        period_key: str,
    ) -> BudgetDecision:
        """Build a decision with the source's current remaining quota."""
        return BudgetDecision(
            source=source,
            allowed=allowed,
            reason=reason,
            remaining=(0.0 if budget.quota_exhausted else max(budget.limit - budget.used, 0.0)),
            period_key=period_key,
            unit=budget.unit,
            period=budget.period,
        )

    async def _ensure_current_period(self, budget: Budget) -> str:
        """Roll *budget* onto the current period bucket (lazy, first use).

        A changed bucket resets usage to the persisted value for that bucket
        (so multi-process usage accumulates) and clears the circuit breaker —
        USD-class budgets therefore recover automatically at the next UTC
        day boundary.  Store failures degrade to the in-memory state and are
        reported, never raised (fail-soft).
        """
        key = period_key_for(budget.period, self._now())
        if key == budget.period_key:
            return key
        budget.period_key = key
        budget.used = 0.0
        budget.quota_exhausted = False
        budget.exhausted_at = None
        try:
            stored = await self._store.get_budget_usage(budget.source, key)
        except Exception as exc:  # fail-soft: keep the local (reset) state
            logger.warning("budget store read failed for %s/%s: %s", budget.source, key, exc)
            self._log(budget.source, "store_error", str(exc))
            stored = None
        if stored is not None:
            budget.used = stored
        self._log(budget.source, "period_rolled", key)
        return key
