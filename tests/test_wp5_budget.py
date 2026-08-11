"""WP5 budget management tests.

Covers the tiered semantics from ``docs/upgrade/technical-design.md`` §1.4
(I6 decision):

- pre-check for req/rps-class sources (s2 / crossref / arxiv);
- post-metering + threshold circuit breaker for USD/credit-class sources
  (openalex), with UTC-day-boundary recovery;
- period rollover (UTC day / aligned natural windows), lazy on first use;
- fail-soft over-limit behaviour (skip source, never raise);
- persistence through the ``budget_usage`` store (in-memory mock and the
  real SQLiteStorage migration).

All tests are offline / mock-only.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from academic_intelligence.budget import (
    DEFAULT_BUDGETS,
    BudgetKind,
    BudgetManager,
    BudgetSpec,
    MemoryBudgetStore,
    period_key_for,
)
from academic_intelligence.storage.sqlite_store import SQLiteStorage


class _Clock:
    """Mutable fake clock for period-rollover tests."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


class _FlakyStore:
    """BudgetStore that fails reads/saves to exercise fail-soft persistence."""

    def __init__(self, *, fail_reads: bool = False, fail_saves: bool = False) -> None:
        self.fail_reads = fail_reads
        self.fail_saves = fail_saves
        self.data: dict[tuple[str, str], float] = {}

    async def get_budget_usage(self, source: str, period: str) -> float | None:
        if self.fail_reads:
            raise RuntimeError("read boom")
        return self.data.get((source, period))

    async def save_budget_usage(self, source: str, period: str, used: float, unit: str) -> None:
        if self.fail_saves:
            raise RuntimeError("write boom")
        self.data[(source, period)] = used


# ----------------------------------------------------------------------
# Defaults / spec model
# ----------------------------------------------------------------------


def test_default_budgets_match_design() -> None:
    specs = {spec.source: spec for spec in DEFAULT_BUDGETS}
    # USD/credit class: openalex 1.0 USD per UTC day, metered
    oa = specs["openalex"]
    assert oa.limit == 1.0 and oa.unit == "usd" and oa.period == "day"
    assert oa.effective_semantics is BudgetKind.METERED
    # req/rps class: s2 100 req / 5min, crossref polite 3 req/s, arxiv 1 req/3s
    ss = specs["semantic_scholar"]
    assert ss.limit == 100.0 and ss.unit == "req" and ss.period == "300s"
    assert ss.effective_semantics is BudgetKind.PRECHECK
    assert specs["crossref"].limit == 3.0 and specs["crossref"].period == "1s"
    assert specs["arxiv"].limit == 1.0 and specs["arxiv"].period == "3s"


def test_budget_spec_period_validation() -> None:
    BudgetSpec(source="x", limit=1, unit="req", period="300s")
    BudgetSpec(source="x", limit=1, unit="req", period="day")
    with pytest.raises(ValueError):
        BudgetSpec(source="x", limit=1, unit="req", period="weekly")
    # zero-limit budgets are valid since IM-3: a config-level kill switch
    # that denies the source fail-soft at every check (negative still fails).
    BudgetSpec(source="x", limit=0, unit="req", period="day")
    with pytest.raises(ValueError):
        BudgetSpec(source="x", limit=-1, unit="req", period="day")


def test_budget_spec_semantics_derivation() -> None:
    assert (
        BudgetSpec(source="openalex", limit=1, unit="usd", period="day").effective_semantics
        is BudgetKind.METERED
    )
    assert (
        BudgetSpec(source="x", limit=1, unit="credit", period="day").effective_semantics
        is BudgetKind.METERED
    )
    assert (
        BudgetSpec(source="s2", limit=100, unit="req", period="300s").effective_semantics
        is BudgetKind.PRECHECK
    )
    assert (
        BudgetSpec(
            source="x", limit=1, unit="req", period="day", semantics=BudgetKind.METERED
        ).effective_semantics
        is BudgetKind.METERED
    )


def test_period_key_formats() -> None:
    now = datetime(2026, 8, 10, 16, 7, 12, tzinfo=UTC)
    assert period_key_for("day", now) == "2026-08-10"
    assert period_key_for("300s", now) == "2026-08-10T16:05:00Z"
    assert period_key_for("1s", now) == "2026-08-10T16:07:12Z"
    # naive datetimes are treated as UTC
    assert period_key_for("day", datetime(2026, 8, 10, 16, 7)) == "2026-08-10"
    with pytest.raises(ValueError):
        period_key_for("week", now)


# ----------------------------------------------------------------------
# req/rps class: pre-check
# ----------------------------------------------------------------------


async def test_precheck_req_class_allows_below_limit_then_denies() -> None:
    manager = BudgetManager(budgets=[BudgetSpec(source="s2", limit=3, unit="req", period="300s")])
    for i in range(3):
        decision = await manager.check("s2")
        assert decision.allowed
        assert decision.reason == "ok"
        assert decision.remaining == 3 - i
        await manager.consume("s2")
    decision = await manager.check("s2")
    assert not decision.allowed
    assert decision.exhausted
    assert decision.reason == "budget_exhausted"
    assert decision.remaining == 0.0


async def test_precheck_consume_after_limit_still_records() -> None:
    """A stale consume records usage; the *next* check denies (pre-check)."""
    manager = BudgetManager(budgets=[BudgetSpec(source="s2", limit=1, unit="req", period="300s")])
    await manager.consume("s2")
    assert not (await manager.check("s2")).allowed
    await manager.consume("s2")  # overshoot: no raise, usage still counted
    assert not (await manager.check("s2")).allowed
    status = next(s for s in await manager.status() if s.source == "s2")
    assert status.used == 2.0


# ----------------------------------------------------------------------
# USD/credit class: post-metering + threshold circuit breaker
# ----------------------------------------------------------------------


async def test_metered_has_no_precheck_but_trips_at_limit() -> None:
    manager = BudgetManager(
        budgets=[BudgetSpec(source="openalex", limit=1.0, unit="usd", period="day")]
    )
    # single-request cost is unknown up front: no pre-check
    assert (await manager.check("openalex")).allowed
    await manager.consume("openalex", 0.5)
    assert (await manager.check("openalex")).allowed  # still under 1.0
    decision = await manager.consume("openalex", 0.5)  # reaches limit -> breaker
    assert not decision.allowed
    assert decision.reason == "quota_exhausted"
    assert (await manager.check("openalex")).exhausted


@pytest.mark.parametrize(
    ("http_status", "signal"),
    [
        (402, None),
        (429, None),
        (None, "billing_error"),
        (None, "quota_exceeded"),
        (None, "rate_limited"),
    ],
)
async def test_metered_breaker_trips_on_billing_signals(
    http_status: int | None, signal: str | None
) -> None:
    manager = BudgetManager(
        budgets=[BudgetSpec(source="openalex", limit=1.0, unit="usd", period="day")]
    )
    decision = await manager.report_failure("openalex", http_status=http_status, signal=signal)
    assert not decision.allowed
    assert decision.reason == "breaker_tripped"
    assert (await manager.check("openalex")).exhausted


async def test_metered_non_billing_signals_do_not_trip() -> None:
    manager = BudgetManager(
        budgets=[BudgetSpec(source="openalex", limit=1.0, unit="usd", period="day")]
    )
    for http_status in (500, 503):
        assert (await manager.report_failure("openalex", http_status=http_status)).allowed
    assert (await manager.report_failure("openalex", signal="timeout")).allowed
    assert (await manager.check("openalex")).allowed


async def test_precheck_source_signal_does_not_trip_breaker() -> None:
    """The circuit breaker is scoped to USD/credit semantics (I6)."""
    manager = BudgetManager(budgets=[BudgetSpec(source="s2", limit=3, unit="req", period="300s")])
    assert (await manager.report_failure("s2", http_status=429)).allowed
    assert (await manager.check("s2")).allowed
    assert "rate_limit_signal" in [e.kind for e in manager.pop_events()]


# ----------------------------------------------------------------------
# period rollover
# ----------------------------------------------------------------------


async def test_usd_breaker_recovers_at_utc_day_boundary() -> None:
    clock = _Clock(datetime(2026, 8, 10, 23, 59, tzinfo=UTC))
    manager = BudgetManager(
        budgets=[BudgetSpec(source="openalex", limit=1.0, unit="usd", period="day")],
        now_fn=clock,
    )
    await manager.report_failure("openalex", http_status=402)
    assert (await manager.check("openalex")).exhausted
    # the next UTC day rolls the period: breaker clears, usage resets
    clock.advance(seconds=61)
    decision = await manager.check("openalex")
    assert decision.allowed
    assert decision.reason == "ok"
    assert decision.period_key == "2026-08-11"
    status = next(s for s in await manager.status() if s.source == "openalex")
    assert status.used == 0.0 and not status.quota_exhausted


async def test_req_period_rolls_at_natural_window_boundary() -> None:
    clock = _Clock(datetime(2026, 8, 10, 16, 7, tzinfo=UTC))
    manager = BudgetManager(
        budgets=[BudgetSpec(source="s2", limit=100, unit="req", period="300s")],
        now_fn=clock,
    )
    await manager.consume("s2", 100)
    assert not (await manager.check("s2")).allowed
    # 16:07 UTC -> next aligned 5-minute bucket starts 16:10:00Z
    clock.now = datetime(2026, 8, 10, 16, 10, tzinfo=UTC)
    decision = await manager.check("s2")
    assert decision.allowed
    assert decision.period_key == "2026-08-10T16:10:00Z"
    assert decision.remaining == 100.0


async def test_rollover_loads_persisted_usage_for_new_bucket() -> None:
    """A new bucket reloads its stored usage (multi-process accumulation)."""
    store = MemoryBudgetStore()
    clock = _Clock(datetime(2026, 8, 10, 16, 7, tzinfo=UTC))
    first = BudgetManager(store=store, now_fn=clock)
    await first.consume("semantic_scholar", 3)
    # a second process consumed more into the same bucket
    await store.save_budget_usage(
        "semantic_scholar", period_key_for("300s", clock.now), 40.0, "req"
    )
    second = BudgetManager(store=store, now_fn=clock)
    decision = await second.check("semantic_scholar")
    assert decision.remaining == 60.0  # 100 - (3 + 37) -> bucket now holds 40


# ----------------------------------------------------------------------
# fail-soft: skip the source, fall back to others, never raise
# ----------------------------------------------------------------------


async def test_fail_soft_source_fallback() -> None:
    manager = BudgetManager(
        budgets=[
            BudgetSpec(source="s2", limit=1, unit="req", period="300s"),
            BudgetSpec(source="arxiv", limit=5, unit="req", period="300s"),
        ]
    )
    await manager.consume("s2")  # s2 exhausted (used == limit)
    used_sources: list[str] = []
    for source in ("s2", "arxiv"):
        decision = await manager.check(source)
        if not decision.allowed:
            continue  # fail-soft: skip this source, try the next one
        used_sources.append(source)
    assert used_sources == ["arxiv"]
    # repeated checks on the exhausted source never raise
    for _ in range(3):
        assert not (await manager.check("s2")).allowed


async def test_unconfigured_source_is_always_allowed() -> None:
    manager = BudgetManager()  # defaults: pubmed / web carry no quota
    for source in ("pubmed", "web", "ieee"):
        decision = await manager.check(source)
        assert decision.allowed
        assert decision.reason == "no_budget"
        assert decision.remaining == float("inf")
        assert (await manager.consume(source, 5)).allowed
    assert "pubmed" not in [s.source for s in await manager.status()]


async def test_custom_budgets_replace_defaults() -> None:
    manager = BudgetManager(budgets=[BudgetSpec(source="ieee", limit=10, unit="req", period="60s")])
    assert (await manager.check("openalex")).reason == "no_budget"
    assert (await manager.check("ieee")).allowed
    assert [s.source for s in await manager.status()] == ["ieee"]


async def test_negative_cost_rejected() -> None:
    manager = BudgetManager()
    with pytest.raises(ValueError):
        await manager.consume("openalex", -1)


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------


async def test_persistence_across_manager_instances() -> None:
    store = MemoryBudgetStore()
    clock = _Clock(datetime(2026, 8, 10, tzinfo=UTC))
    first = BudgetManager(store=store, now_fn=clock)
    await first.consume("semantic_scholar", 7)
    second = BudgetManager(store=store, now_fn=clock)
    status = next(s for s in await second.status() if s.source == "semantic_scholar")
    assert status.used == 7.0
    # the second manager shares the quota: 93 more requests exhaust it
    await second.consume("semantic_scholar", 93)
    assert not (await second.check("semantic_scholar")).allowed
    third = BudgetManager(store=store, now_fn=clock)
    assert not (await third.check("semantic_scholar")).allowed


async def test_sqlite_budget_usage_migration_and_persistence(
    tmp_path: Path,
) -> None:
    """connect() creates the budget_usage table; usage survives reconnects."""
    db = tmp_path / "budget.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        # fresh bucket -> None (table exists because reads/writes succeed)
        assert await store.get_budget_usage("openalex", "2026-08-10") is None
        await store.save_budget_usage("openalex", "2026-08-10", 0.5, "usd")
        assert await store.get_budget_usage("openalex", "2026-08-10") == 0.5
        # upsert: same (source, period) key overwrites
        await store.save_budget_usage("openalex", "2026-08-10", 0.7, "usd")
        assert await store.get_budget_usage("openalex", "2026-08-10") == 0.7
    finally:
        await store.close()
    # persistence across a reconnect (multi-process story)
    store2 = SQLiteStorage(str(db))
    await store2.connect()
    try:
        assert await store2.get_budget_usage("openalex", "2026-08-10") == 0.7
        assert await store2.get_budget_usage("openalex", "2026-08-11") is None
    finally:
        await store2.close()


async def test_budget_usage_table_created_on_legacy_db(tmp_path: Path) -> None:
    """Incremental migration (design §8 / T10): a pre-upgrade database that
    predates the budget_usage table gets it created on connect, with the
    legacy tables untouched.
    """
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT)")
    con.execute("CREATE TABLE authors (id TEXT PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()

    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        assert await store.get_budget_usage("openalex", "2026-08-10") is None
        await store.save_budget_usage("openalex", "2026-08-10", 0.25, "usd")
        assert await store.get_budget_usage("openalex", "2026-08-10") == 0.25
    finally:
        await store.close()
    # legacy data is still readable after the migration
    con = sqlite3.connect(str(db))
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        con.close()
    assert "budget_usage" in tables
    assert "papers" in tables and "authors" in tables


async def test_manager_with_sqlite_store(tmp_path: Path) -> None:
    db = str(tmp_path / "budget-manager.db")
    store = SQLiteStorage(db)
    await store.connect()
    try:
        clock = _Clock(datetime(2026, 8, 10, tzinfo=UTC))
        manager = BudgetManager(store=store, now_fn=clock)
        await manager.consume("semantic_scholar", 3)
        status = next(s for s in await manager.status() if s.source == "semantic_scholar")
        assert status.used == 3.0
        # a fresh manager on the same database sees the same usage
        fresh = BudgetManager(store=store, now_fn=clock)
        assert (await fresh.check("semantic_scholar")).remaining == 97.0
    finally:
        await store.close()


async def test_store_failure_is_fail_soft() -> None:
    manager = BudgetManager(
        budgets=[BudgetSpec(source="s2", limit=3, unit="req", period="300s")],
        store=_FlakyStore(fail_saves=True),
    )
    for _ in range(3):
        assert (await manager.consume("s2")).allowed  # writes fail, no raise
    assert not (await manager.check("s2")).allowed  # local accounting still holds
    assert "store_error" in [e.kind for e in manager.pop_events()]


# ----------------------------------------------------------------------
# status / events / integration helper
# ----------------------------------------------------------------------


async def test_status_shape() -> None:
    manager = BudgetManager()  # defaults
    statuses = await manager.status()
    by_source = {s.source: s for s in statuses}
    assert set(by_source) == {"openalex", "semantic_scholar", "crossref", "arxiv"}
    oa = by_source["openalex"]
    assert oa.limit == 1.0 and oa.unit == "usd" and oa.period == "day"
    assert oa.semantics is BudgetKind.METERED
    assert oa.used == 0.0 and oa.remaining == 1.0 and not oa.quota_exhausted
    ss = by_source["semantic_scholar"]
    assert ss.semantics is BudgetKind.PRECHECK and ss.period == "300s"
    assert ss.period_key.endswith("Z")


async def test_request_guard_checks_then_consumes() -> None:
    manager = BudgetManager(budgets=[BudgetSpec(source="s2", limit=3, unit="req", period="300s")])
    async with manager.request_guard("s2") as decision:
        assert decision.allowed
    assert (await manager.check("s2")).remaining == 2.0  # consumed on exit
    # a denied guard does not consume
    await manager.consume("s2", 2)
    async with manager.request_guard("s2") as decision:
        assert not decision.allowed
    assert (await manager.check("s2")).remaining == 0.0
