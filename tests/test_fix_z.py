"""FIX-Z: super-old database migration (Z-1) and NOT NULL server defaults (Z-2).

Covers:
- Z-1: ``connect()`` adds every model-expected base column (keywords, url,
  pdf_url, evidence, ...) to pre-v1 databases — not just the v2 columns —
  so a super-old DB missing base columns reads fine after connect and its
  data is preserved; a database missing an un-addable required column
  (``papers.title`` / ``authors.name`` / ``citations.id``) fails connect with
  a clear "schema too old" :class:`StorageError` instead of a confusing
  ``no such column`` error on the first read (the citations check lives in
  ``_migrate_citation_index``, FIX-Z2 F1).
- Z-2: NOT NULL JSON/status columns carry server defaults (fresh databases
  and columns added by migration), so writes that bypass the ORM (old-code
  bare INSERT) no longer hard-fail with ``IntegrityError NOT NULL
  constraint failed``.
"""

from __future__ import annotations

import aiosqlite
import pytest
from sqlalchemy import create_engine, text

from academic_intelligence.core.models import Paper
from academic_intelligence.storage.base import StorageError
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _build_super_old_db(db) -> None:
    """Pre-v1 database: papers/authors tables missing most base columns.

    ``papers`` keeps only the oldest core columns (no keywords, url,
    pdf_url, evidence, ...); ``authors`` keeps only id/name.
    """
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE papers ("
            "id VARCHAR(64) PRIMARY KEY, "
            "title TEXT NOT NULL, "
            "authors JSON, "
            "year INTEGER, "
            "venue TEXT, "
            "abstract TEXT, "
            "doi VARCHAR(255), "
            "citations INTEGER)"
        )
        conn.exec_driver_sql(
            "INSERT INTO papers (id, title, authors, year, doi) "
            "VALUES ('super-old-1', 'Super Old Paper', '[\"Ada\"]', 1999, "
            "'10.1000/super')"
        )
        conn.exec_driver_sql(
            "CREATE TABLE authors (id VARCHAR(64) PRIMARY KEY, name TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO authors (id, name) VALUES ('au-super', 'Ada Lovelace')"
        )
    engine.dispose()


@pytest.mark.asyncio
async def test_super_old_db_missing_base_columns_migrates_and_preserves_data(
    tmp_path,
) -> None:
    """Z-1: pre-v1 DB missing base columns gets them added on connect; the old
    row stays readable with its data preserved; reconnect is idempotent."""
    db = tmp_path / "super_old.db"
    _build_super_old_db(db)

    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        old = await store.get_paper("super-old-1")
        assert old is not None
        assert old.title == "Super Old Paper"
        assert old.year == 1999
        assert old.doi == "10.1000/super"
        assert [a.name for a in old.authors] == ["Ada"]
        # base columns were added; defaulted to empty containers
        assert old.keywords == []
        assert old.fields_of_study == []
        assert old.evidence_list == []

        # newly added columns are writeable through the API
        paper = Paper(id="z-new", title="Z New", year=2024, keywords=["ai"])
        await store.save_paper(paper)
        got = await store.get_paper("z-new")
        assert got is not None and got.title == "Z New"
        assert got.keywords == ["ai"]

        au = await store.get_author("au-super")
        assert au is not None
        assert au.name == "Ada Lovelace"
        assert au.aliases == []
        assert au.coauthors == []
        assert au.disambiguation_status == "auto"
    finally:
        await store.close()

    store2 = SQLiteStorage(str(db))
    await store2.connect()
    try:
        again = await store2.get_paper("super-old-1")
        assert again is not None and again.title == "Super Old Paper"
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_too_old_db_missing_required_column_fails_with_clear_error(
    tmp_path,
) -> None:
    """Z-1: a database missing an un-addable required column (title) surfaces
    a diagnosable "schema too old" StorageError at connect."""
    db = tmp_path / "too_old.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE papers ("
            "id VARCHAR(64) PRIMARY KEY, "
            "year INTEGER, "
            "authors JSON)"
        )
    engine.dispose()

    store = SQLiteStorage(str(db))
    with pytest.raises(StorageError, match="too old"):
        await store.connect()


@pytest.mark.asyncio
async def test_bare_insert_without_not_null_json_columns_uses_server_defaults(
    tmp_path,
) -> None:
    """Z-2: on a fresh database, NOT NULL JSON/status columns carry server
    defaults, so an old-code bare INSERT that omits them does not raise
    IntegrityError and the row reads back with empty containers."""
    db = tmp_path / "fresh.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    await store.close()

    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "INSERT INTO papers (id, title) VALUES (?, ?)",
            ("raw-1", "Raw Paper"),
        )
        await conn.execute(
            "INSERT INTO authors (id, name) VALUES (?, ?)",
            ("raw-a", "Raw Author"),
        )
        await conn.commit()

    store2 = SQLiteStorage(str(db))
    await store2.connect()
    try:
        got = await store2.get_paper("raw-1")
        assert got is not None
        assert got.title == "Raw Paper"
        assert got.authors == []
        assert got.keywords == []
        assert got.fields_of_study == []
        assert got.evidence_list == []

        au = await store2.get_author("raw-a")
        assert au is not None
        assert au.name == "Raw Author"
        assert au.aliases == []
        assert au.coauthors == []
        assert au.venues == []
        assert au.interests == []
        assert au.disambiguation_status == "auto"
        assert au.evidence_list == []
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_migrated_legacy_db_added_columns_carry_server_defaults(
    tmp_path,
) -> None:
    """Z-2: columns added by migration to a super-old DB are NOT NULL with a
    server default, so old-code bare INSERTs that omit them do not raise."""
    db = tmp_path / "legacy_defaults.db"
    _build_super_old_db(db)

    store = SQLiteStorage(str(db))
    await store.connect()
    await store.close()

    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "INSERT INTO papers (id, title) VALUES (?, ?)",
            ("raw-old-1", "Raw Legacy"),
        )
        await conn.execute(
            "INSERT INTO authors (id, name) VALUES (?, ?)",
            ("raw-old-a", "Raw Legacy Author"),
        )
        await conn.commit()

    store2 = SQLiteStorage(str(db))
    await store2.connect()
    try:
        got = await store2.get_paper("raw-old-1")
        assert got is not None
        assert got.keywords == []
        assert got.evidence_list == []

        au = await store2.get_author("raw-old-a")
        assert au is not None
        assert au.aliases == []
        assert au.disambiguation_status == "auto"
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_too_old_db_citations_missing_id_fails_with_clear_error(
    tmp_path,
) -> None:
    """Z-1 / FIX-Z2 F1: a database whose citations table predates the id
    column (older than the real v1 schema) surfaces a diagnosable "schema
    too old" StorageError at connect — not a raw ``no such column: id`` from
    the dedup migration."""
    db = tmp_path / "too_old_citations.db"
    _build_super_old_db(db)
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE citations ("
            "citing_paper_id VARCHAR(64) NOT NULL, "
            "cited_paper_id VARCHAR(64) NOT NULL, "
            "evidence JSON NOT NULL)"
        )
    engine.dispose()

    store = SQLiteStorage(str(db))
    with pytest.raises(StorageError, match="too old"):
        await store.connect()


@pytest.mark.asyncio
async def test_super_old_db_citations_with_id_migrates_and_dedupes(
    tmp_path,
) -> None:
    """Z-1 / FIX-Z2 F1: a super-old database whose citations table already
    has the id column migrates normally — duplicate pairs are collapsed and
    the unique index is installed, so the id check does not false-positive on
    any valid old DB."""
    db = tmp_path / "old_citations_with_id.db"
    _build_super_old_db(db)
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE citations ("
            "id VARCHAR(64) PRIMARY KEY, "
            "citing_paper_id VARCHAR(64) NOT NULL, "
            "cited_paper_id VARCHAR(64) NOT NULL, "
            "evidence JSON NOT NULL)"
        )
        conn.exec_driver_sql("INSERT INTO citations VALUES ('a', 'p1', 'p2', '{}')")
        conn.exec_driver_sql("INSERT INTO citations VALUES ('b', 'p1', 'p2', '{}')")
        conn.exec_driver_sql("INSERT INTO citations VALUES ('c', 'p1', 'p3', '{}')")
    engine.dispose()

    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        stats = await store.get_stats()
        assert stats["total_citations"] == 2  # duplicate pair collapsed
        async with store._engine.connect() as engine_conn:
            result = await engine_conn.execute(text("PRAGMA index_list('citations')"))
            names = {row[1] for row in result.fetchall()}
        assert "uq_citations_citing_cited" in names
    finally:
        await store.close()
