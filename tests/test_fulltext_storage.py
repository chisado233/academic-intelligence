"""Tests for the full_text storage table (save/get + T10-m migration)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from academic_intelligence.fulltext.models import FullText, Segment
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _sample_fulltext() -> FullText:
    return FullText(
        paper_id="p1",
        source="arxiv",
        oa_license=None,
        file_path="tmp/fulltext/p1.pdf",
        paragraph_count=2,
        segments=[
            Segment(heading="Abstract", text="Hello world.", page=1),
            Segment(heading=None, text="Second paragraph.", page=2),
        ],
        collected_at=datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC),
    )


async def test_full_text_save_get_roundtrip(tmp_path) -> None:
    db = tmp_path / "fulltext.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        fulltext = _sample_fulltext()
        await store.save_full_text(fulltext)

        loaded = await store.get_full_text("p1")
        assert loaded is not None
        assert loaded.paper_id == "p1"
        assert loaded.source == "arxiv"
        assert loaded.file_path == "tmp/fulltext/p1.pdf"
        assert loaded.paragraph_count == 2
        assert len(loaded.segments) == 2
        assert loaded.segments[0].heading == "Abstract"
        assert loaded.segments[0].text == "Hello world."
        assert loaded.segments[0].page == 1
        assert loaded.segments[1].page == 2
        assert loaded.collected_at == fulltext.collected_at

        # Missing paper -> None.
        assert await store.get_full_text("missing") is None

        # Upsert semantics (C-1): re-saving the same paper_id updates the row.
        updated = fulltext.model_copy(
            update={
                "source": "core",
                "paragraph_count": 1,
                "segments": [Segment(text="Only paragraph.", page=1)],
            }
        )
        await store.save_full_text(updated)
        reloaded = await store.get_full_text("p1")
        assert reloaded is not None
        assert reloaded.source == "core"
        assert reloaded.paragraph_count == 1
        assert len(reloaded.segments) == 1
    finally:
        await store.close()


async def test_full_text_table_persists_across_reconnect(tmp_path) -> None:
    db = tmp_path / "persist.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    await store.save_full_text(_sample_fulltext())
    await store.close()

    store2 = SQLiteStorage(str(db))
    await store2.connect()
    try:
        loaded = await store2.get_full_text("p1")
        assert loaded is not None
        assert loaded.source == "arxiv"
    finally:
        await store2.close()


async def test_migration_old_db_full_text_created_old_data_intact(tmp_path) -> None:
    """T10-m simulation: a pre-upgrade database gets full_text, loses nothing.

    A legacy database (papers/authors with the old v1 column set, one paper
    row) is opened with the new storage: ``create_all`` must add only the
    missing ``full_text`` table (CREATE TABLE IF NOT EXISTS semantics), the
    old rows must stay queryable, and stats must not lose counts.
    """
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE papers ("
        "  id TEXT PRIMARY KEY,"
        "  title TEXT NOT NULL,"
        "  authors TEXT,"
        "  year INTEGER,"
        "  venue TEXT,"
        "  abstract TEXT,"
        "  doi TEXT,"
        "  url TEXT,"
        "  pdf_url TEXT,"
        "  citations INTEGER,"
        "  keywords TEXT,"
        "  evidence TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE authors (id TEXT PRIMARY KEY, name TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO papers (id, title, authors, doi) VALUES (?, ?, ?, ?)",
        ("legacy1", "Legacy Paper", "[]", "10.1000/legacy"),
    )
    conn.commit()
    conn.close()

    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        # New table created...
        with sqlite3.connect(db) as check:
            tables = {
                row[0]
                for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "full_text" in tables

        # ...and old data is still queryable.
        paper = await store.get_paper("legacy1")
        assert paper is not None
        assert paper.title == "Legacy Paper"
        assert paper.doi == "10.1000/legacy"

        stats = await store.get_stats()
        assert stats["total_papers"] >= 1

        # The new table is fully usable on the migrated database.
        await store.save_full_text(_sample_fulltext())
        loaded = await store.get_full_text("p1")
        assert loaded is not None
        assert loaded.paragraph_count == 2
    finally:
        await store.close()
