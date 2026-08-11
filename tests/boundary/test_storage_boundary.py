"""Boundary tests for storage backends."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


pytestmark = [pytest.mark.boundary]


def _evidence() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W1",
        confidence=0.8,
    )


def _paper(i: int, *, with_id: bool = False) -> Paper:
    return Paper(
        id=f"paper-{i}" if with_id else None,
        title=f"Paper {i}",
        authors=["A"],
        year=2000 + (i % 20),
        evidence=_evidence(),
    )


class TestStorageBoundary:
    """Storage boundary tests."""

    @pytest.mark.asyncio
    async def test_sqlite_concurrent_writes(self, tmp_path: Path) -> None:
        """Concurrent save_paper calls should all succeed with unique IDs."""
        db = tmp_path / "concurrent.db"
        store = SQLiteStorage(str(db))
        await store.connect()
        try:
            papers = [_paper(i) for i in range(100)]
            tasks = [store.save_paper(p) for p in papers]
            ids = await asyncio.gather(*tasks)
            assert len(ids) == 100
            assert len(set(ids)) == 100
            stats = await store.get_stats()
            assert stats["total_papers"] == 100
        finally:
            await store.close()

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_sqlite_large_dataset(self, tmp_path: Path) -> None:
        """Write a large number of papers and verify stats.

        Uses 1_000 records (representative large set) with explicit IDs for
        predictable uniqueness; full 10k would dominate CI wall-clock.
        """
        db = tmp_path / "large.db"
        store = SQLiteStorage(str(db))
        await store.connect()
        try:
            n = 1000
            batch_size = 100
            for start in range(0, n, batch_size):
                chunk = [_paper(i, with_id=True) for i in range(start, start + batch_size)]
                await asyncio.gather(*[store.save_paper(p) for p in chunk])
            stats = await store.get_stats()
            assert stats["total_papers"] == n
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_json_store_corrupted_file(self, tmp_path: Path) -> None:
        """Corrupted JSON file should raise StorageError, not crash process."""
        base = tmp_path / "corrupted_store"
        base.mkdir(parents=True, exist_ok=True)
        papers_file = base / "papers.json"
        papers_file.write_text("{invalid json", encoding="utf-8")

        store = JSONStorage(str(base))
        with pytest.raises(StorageError):
            await store.connect()

    @pytest.mark.asyncio
    async def test_json_store_empty_files(self, tmp_path: Path) -> None:
        """Empty directory (no files yet) should connect cleanly."""
        store = JSONStorage(str(tmp_path / "empty_json"))
        await store.connect()
        try:
            stats = await store.get_stats()
            assert stats["total_papers"] == 0
            pid = await store.save_paper(_paper(0))
            assert pid
            assert await store.get_paper(pid) is not None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sqlite_get_missing_paper(self, tmp_path: Path) -> None:
        """Lookup of unknown id returns None."""
        store = SQLiteStorage(str(tmp_path / "miss.db"))
        await store.connect()
        try:
            assert await store.get_paper("does-not-exist") is None
            assert await store.delete_paper("does-not-exist") is False
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sqlite_update_nonexistent(self, tmp_path: Path) -> None:
        """Updating a missing paper returns False."""
        store = SQLiteStorage(str(tmp_path / "upd.db"))
        await store.connect()
        try:
            ok = await store.update_paper("missing", _paper(1))
            assert ok is False
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_json_concurrent_writes(self, tmp_path: Path) -> None:
        """JSON storage serializes concurrent writes via lock."""
        store = JSONStorage(str(tmp_path / "json_conc"))
        await store.connect()
        try:
            papers = [_paper(i) for i in range(50)]
            ids = await asyncio.gather(*[store.save_paper(p) for p in papers])
            assert len(ids) == 50
            assert len(set(ids)) == 50
            stats = await store.get_stats()
            assert stats["total_papers"] == 50
        finally:
            await store.close()


# ---------------------------------------------------------------------------
# FIX-H F5 (H6): typed errors on the read path + connect() rebuild warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_dropped_table_read_raises_storage_error(
    tmp_path: Path,
) -> None:
    """H6: querying after the ``papers`` table was dropped must raise a typed
    :class:`StorageError`, not a raw ``sqlite3.OperationalError``."""
    import sqlite3

    db = tmp_path / "h6.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        await store.save_paper(_paper(1, with_id=True))
        conn = sqlite3.connect(str(db))
        conn.execute("DROP TABLE papers")
        conn.commit()
        conn.close()

        with pytest.raises(StorageError) as excinfo:
            await store.query_papers()
        # not the raw DBAPI exception leaking out
        assert not isinstance(excinfo.value, sqlite3.OperationalError)
        assert excinfo.value.backend == "sqlite"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_corrupt_json_cell_read_raises_storage_error(
    tmp_path: Path,
) -> None:
    """H6: a corrupt JSON cell must raise a typed :class:`StorageError`
    instead of a bare ``json.JSONDecodeError`` poisoning get_paper."""
    import json
    import sqlite3

    db = tmp_path / "h6b.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        await store.save_paper(_paper(1, with_id=True))
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE papers SET authors = 'not-json' WHERE id = 'paper-1'")
        conn.commit()
        conn.close()

        with pytest.raises(StorageError) as excinfo:
            await store.get_paper("paper-1")
        assert not isinstance(excinfo.value, json.JSONDecodeError)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_connect_warns_when_core_table_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """H6: reconnecting to an existing db file that lost its core tables must
    log a warning instead of silently rebuilding empty tables."""
    import logging
    import sqlite3

    db = tmp_path / "h6c.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    await store.save_paper(_paper(1, with_id=True))
    await store.close()

    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE papers")
    conn.commit()
    conn.close()

    store2 = SQLiteStorage(str(db))
    with caplog.at_level(
        logging.WARNING, logger="academic_intelligence.storage.sqlite_store"
    ):
        await store2.connect()
    try:
        assert any("core tables" in record.message for record in caplog.records)
        # the rebuild still works (empty tables recreated)
        assert await store2.query_papers() == []
    finally:
        await store2.close()
