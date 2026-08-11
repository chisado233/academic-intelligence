"""FIX-AE (P50 round-32): write-path lock retry (AE-1), configurable
busy_timeout (AE-2), concurrency contract (AE-3).

Covers:
- F1: ``save_batch`` and the single-record write paths retry ``database
  is locked`` (SQLITE_BUSY) a bounded number of times (50ms delay, max 5)
  instead of hard-failing; a lock that outlives the retry budget surfaces
  a ``StorageError`` with no partial write (the failed transaction rolls
  back whole).
- F2: ``SQLiteStorage(busy_timeout=...)`` and
  ``Config.sqlite_busy_timeout`` tune the per-connection SQLite
  busy_timeout (short → fail fast, long → wait); the default stays 10s.
- F3: concurrency contract documentation only — no behavioral test beyond
  the suite staying green.

Lock simulation: a second aiosqlite connection takes SQLite's write lock
(``BEGIN IMMEDIATE``), so a concurrent write from the storage instance
busy-waits its busy_timeout and then raises ``database is locked`` —
exactly the WAL single-writer contention P50 observed at 32-way
concurrency (V1.4: 6/32 hard failures).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import text

from academic_intelligence import AcademicIntelligence, Config
from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(conf: float = 0.8) -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://e.com",
        confidence=conf,
    )


def _paper(**kwargs: object) -> Paper:
    defaults: dict[str, object] = {
        "title": "FIX-AE Paper",
        "authors": ["Ada"],
        "year": 2020,
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Paper(**defaults)  # type: ignore[arg-type]


def _author(**kwargs: object) -> Author:
    defaults: dict[str, object] = {"name": "Ada Lovelace"}
    defaults.update(kwargs)
    return Author(**defaults)  # type: ignore[arg-type]


def _citation(citing: str = "p-a", cited: str = "p-b") -> Citation:
    return Citation(
        citing_paper_id=citing,
        cited_paper_id=cited,
        evidence=_ev(),
    )


async def _hold_write_lock(db_path: str, release_after: float) -> None:
    """Take SQLite's write lock on its own connection for *release_after* s.

    ``BEGIN IMMEDIATE`` acquires the WAL write lock, making any concurrent
    writer busy-wait its busy_timeout and then raise ``database is
    locked`` until the lock is rolled back.
    """
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await asyncio.sleep(release_after)
        await conn.execute("ROLLBACK")
    finally:
        await conn.close()


async def _pragma_busy_timeout(store: SQLiteStorage) -> int:
    assert store._engine is not None
    async with store._engine.connect() as conn:
        return int((await conn.execute(text("PRAGMA busy_timeout"))).scalar())


# ---------------------------------------------------------------------------
# F1 (AE-1): write paths retry transient lock contention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_batch_retries_busy_then_succeeds(tmp_path: Path) -> None:
    """A write lock held across the first attempt is retried; the batch lands."""
    db = tmp_path / "ae1_batch.db"
    store = SQLiteStorage(str(db), busy_timeout=0.2)
    await store.connect()
    # The lock outlives attempt 0's busy window (0.2s) and attempt 1's too;
    # it is released before attempt 2 starts, so the retry succeeds.
    lock_task = asyncio.create_task(_hold_write_lock(str(db), release_after=0.55))
    await asyncio.sleep(0.05)  # let the lock land before the writer starts
    try:
        await store.save_batch(papers=[_paper(id="ae-1", title="Retried")])
        assert [p.id for p in await store.query_papers()] == ["ae-1"]
    finally:
        await lock_task
        await store.close()


@pytest.mark.asyncio
async def test_save_paper_retries_busy_then_succeeds(tmp_path: Path) -> None:
    """Single-record write paths get the same bounded lock retry."""
    db = tmp_path / "ae1_paper.db"
    store = SQLiteStorage(str(db), busy_timeout=0.2)
    await store.connect()
    lock_task = asyncio.create_task(_hold_write_lock(str(db), release_after=0.55))
    await asyncio.sleep(0.05)
    try:
        paper_id = await store.save_paper(_paper(id="p-ae", title="Single"))
        loaded = await store.get_paper(paper_id)
        assert loaded is not None and loaded.title == "Single"
    finally:
        await lock_task
        await store.close()


@pytest.mark.asyncio
async def test_save_author_retries_busy_then_succeeds(tmp_path: Path) -> None:
    """save_author is covered by the same retry as save_batch/save_paper."""
    db = tmp_path / "ae1_author.db"
    store = SQLiteStorage(str(db), busy_timeout=0.2)
    await store.connect()
    lock_task = asyncio.create_task(_hold_write_lock(str(db), release_after=0.55))
    await asyncio.sleep(0.05)
    try:
        author_id = await store.save_author(_author(name="Grace Hopper"))
        loaded = await store.get_author(author_id)
        assert loaded is not None and loaded.name == "Grace Hopper"
    finally:
        await lock_task
        await store.close()


@pytest.mark.asyncio
async def test_save_citation_retries_busy_then_succeeds(tmp_path: Path) -> None:
    """save_citation is covered by the same retry as the other write paths."""
    db = tmp_path / "ae1_citation.db"
    store = SQLiteStorage(str(db), busy_timeout=0.2)
    await store.connect()
    lock_task = asyncio.create_task(_hold_write_lock(str(db), release_after=0.55))
    await asyncio.sleep(0.05)
    try:
        await store.save_citation(_citation(citing="c1", cited="c2"))
        cites = await store.get_citations_by_paper("c1")
        assert [c.cited_paper_id for c in cites] == ["c2"]
    finally:
        await lock_task
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_busy_beyond_retries_raises_storage_error(
    tmp_path: Path,
) -> None:
    """A lock that outlives the retry budget surfaces a StorageError; the
    failed transaction rolls back whole — no partial batch is visible."""
    db = tmp_path / "ae1_fail.db"
    store = SQLiteStorage(str(db), busy_timeout=0.05)
    await store.connect()
    # 5 attempts × (0.05s busy wait + 0.05s sleep) ≈ 0.5s; the lock is held
    # well past that, so every attempt fails and the budget is exhausted.
    lock_task = asyncio.create_task(_hold_write_lock(str(db), release_after=2.0))
    await asyncio.sleep(0.05)
    start = time.monotonic()
    try:
        with pytest.raises(StorageError) as excinfo:
            await store.save_batch(
                papers=[_paper(id="ae-x", title="Never"), _paper(id="ae-y", title="Also")]
            )
        assert "database is locked" in str(excinfo.value)
        # short busy_timeout failed fast — nowhere near the 10s default
        assert time.monotonic() - start < 5.0
        # the failed transaction left no partial state behind
        assert (await store.get_stats())["total_papers"] == 0
    finally:
        await lock_task
        await store.close()


# ---------------------------------------------------------------------------
# F2 (AE-2): busy_timeout is configurable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_busy_timeout_default_remains_10s(tmp_path: Path) -> None:
    """Default busy_timeout is unchanged: 10s (10000ms PRAGMA)."""
    db = tmp_path / "ae2_default.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        assert store.busy_timeout == 10.0
        assert await _pragma_busy_timeout(store) == 10000
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_busy_timeout_custom_is_applied(tmp_path: Path) -> None:
    """A custom busy_timeout is reflected on the live connections."""
    db = tmp_path / "ae2_custom.db"
    store = SQLiteStorage(str(db), busy_timeout=0.25)
    await store.connect()
    try:
        assert store.busy_timeout == 0.25
        assert await _pragma_busy_timeout(store) == 250
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_busy_timeout_short_fails_fast_while_locked(tmp_path: Path) -> None:
    """A short busy_timeout makes a contended write fail fast instead of
    waiting the 10s default."""
    db = tmp_path / "ae2_short.db"
    store = SQLiteStorage(str(db), busy_timeout=0.05)
    await store.connect()
    lock_task = asyncio.create_task(_hold_write_lock(str(db), release_after=2.0))
    await asyncio.sleep(0.05)
    start = time.monotonic()
    try:
        with pytest.raises(StorageError):
            await store.save_batch(papers=[_paper(id="x", title="X")])
        assert time.monotonic() - start < 5.0
    finally:
        await lock_task
        await store.close()


@pytest.mark.asyncio
async def test_busy_timeout_long_waits_for_lock_release(tmp_path: Path) -> None:
    """A long busy_timeout absorbs a short-lived lock: the write waits on the
    connection (no retry needed) and lands once the lock is released."""
    db = tmp_path / "ae2_long.db"
    store = SQLiteStorage(str(db), busy_timeout=2.0)
    await store.connect()
    lock_task = asyncio.create_task(_hold_write_lock(str(db), release_after=0.5))
    await asyncio.sleep(0.05)
    start = time.monotonic()
    try:
        await store.save_batch(papers=[_paper(id="w", title="Waited")])
        assert time.monotonic() - start >= 0.3  # waited, did not fail fast
        assert [p.id for p in await store.query_papers()] == ["w"]
    finally:
        await lock_task
        await store.close()


@pytest.mark.asyncio
async def test_config_sqlite_busy_timeout_reaches_storage(tmp_path: Path) -> None:
    """Config.sqlite_busy_timeout is passed through to SQLiteStorage."""
    config = Config(
        sources=["openalex"],
        storage_type="sqlite",
        storage_path=str(tmp_path / "ae2_cfg.db"),
        sqlite_busy_timeout=0.3,
    )
    ai = AcademicIntelligence(config)
    await ai.connect()
    try:
        assert isinstance(ai.storage, SQLiteStorage)
        assert ai.storage.busy_timeout == 0.3
        assert await _pragma_busy_timeout(ai.storage) == 300
    finally:
        await ai.close()
