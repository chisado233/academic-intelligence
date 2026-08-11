"""Budget / quota management (WP5).

Per-source budget enforcement layered independently on top of
``utils/RateLimiter`` (design ``docs/upgrade/technical-design.md`` §1.4,
I6 decision):

- req/rps-class sources (s2 / crossref / arxiv): pre-flight
  :meth:`BudgetManager.check` before a request, :meth:`BudgetManager.consume`
  after;
- USD/credit-class sources (openalex): post-metering
  :meth:`BudgetManager.consume` with a locally-estimated cost plus a
  threshold circuit breaker tripped by billing/rate-limit error signals
  (:meth:`BudgetManager.report_failure`), recovering at the next UTC day
  boundary;
- over-limit behaviour is fail-soft: sources are skipped via
  :class:`BudgetDecision` (never raised) and events are reported through
  :meth:`BudgetManager.pop_events` / :meth:`BudgetManager.status`.

Usage::

    from academic_intelligence.budget import BudgetManager
    from academic_intelligence.storage.sqlite_store import SQLiteStorage

    store = SQLiteStorage("./academic_intelligence.db")
    await store.connect()
    manager = BudgetManager(store=store)  # SQLiteStorage satisfies BudgetStore
    decision = await manager.check("semantic_scholar")
    if decision.allowed:
        ...
        await manager.consume("semantic_scholar")
"""

from academic_intelligence.budget.manager import DEFAULT_BUDGETS, BudgetManager
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
    "Budget",
    "BudgetDecision",
    "BudgetEvent",
    "BudgetKind",
    "BudgetManager",
    "BudgetSpec",
    "BudgetStatus",
    "BudgetStore",
    "DEFAULT_BUDGETS",
    "MemoryBudgetStore",
    "period_key_for",
]
