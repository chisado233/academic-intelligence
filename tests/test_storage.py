"""Tests for storage backends."""

from __future__ import annotations

import pytest

from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _evidence() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W1",
        confidence=0.8,
    )


def _sample_paper() -> Paper:
    return Paper(
        title="Deep Learning",
        authors=["Ian Goodfellow", "Yoshua Bengio"],
        year=2016,
        venue="MIT Press",
        doi="10.5555/3086952",
        evidence=_evidence(),
    )


def _sample_author() -> Author:
    return Author(
        name="Yoshua Bengio",
        affiliation="UdeM",
        interests=["ML"],
        evidence=_evidence(),
    )


@pytest.mark.asyncio
async def test_sqlite_crud(tmp_path) -> None:
    db = tmp_path / "test.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        paper_id = await store.save_paper(_sample_paper())
        assert paper_id
        paper = await store.get_paper(paper_id)
        assert paper is not None
        assert paper.title == "Deep Learning"
        assert paper.doi == "10.5555/3086952"

        author_id = await store.save_author(_sample_author())
        author = await store.get_author(author_id)
        assert author is not None
        assert author.name == "Yoshua Bengio"

        cite_id = await store.save_citation(
            Citation(
                citing_paper_id=paper_id,
                cited_paper_id="other",
                evidence=_evidence(),
            )
        )
        assert cite_id
        cites = await store.get_citations_by_paper(paper_id, direction="outgoing")
        assert len(cites) == 1

        found = await store.query_papers(author="Goodfellow", year=2016)
        assert len(found) == 1

        stats = await store.get_stats()
        assert stats["total_papers"] == 1
        assert stats["total_authors"] == 1
        assert stats["total_citations"] == 1

        assert await store.delete_paper(paper_id) is True
        assert await store.get_paper(paper_id) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_crud(tmp_path) -> None:
    store = JSONStorage(str(tmp_path / "data"))
    await store.connect()
    try:
        paper_id = await store.save_paper(_sample_paper())
        paper = await store.get_paper(paper_id)
        assert paper is not None
        author_id = await store.save_author(_sample_author())
        authors = await store.query_authors(name="Bengio")
        assert len(authors) == 1
        assert authors[0].id == author_id
        stats = await store.get_stats()
        assert stats["total_papers"] == 1
    finally:
        await store.close()


def _identity_author() -> Author:
    return Author(
        name="Wei Zhang",
        orcid="0000-0001-2345-6789",
        semantic_scholar_id="S2-123",
        openalex_id="A42",
        aliases=["W. Zhang", "Zhang Wei"],
        disambiguation_status="ambiguous",
        coauthors=["Alice", "Bob"],
        venues=["NeurIPS"],
        active_years=[2018, 2019, 2020],
        evidence=_evidence(),
    )


@pytest.mark.asyncio
async def test_save_batch_idempotent_upsert(tmp_path) -> None:
    """C-1: re-saving the same batch updates instead of crashing on UNIQUE.

    ``save_batch`` must behave like ``save_paper``/``save_author``: an entry
    whose id already exists updates the stored record instead of inserting a
    duplicate row (which raised IntegrityError → StorageError before).
    """
    db = tmp_path / "batch_idem.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        paper = _sample_paper().model_copy(update={"id": "p-1"})
        author = _sample_author().model_copy(update={"id": "a-1"})

        ids1 = await store.save_batch(papers=[paper], authors=[author], citations=[])
        assert ids1["papers"] == ["p-1"]
        assert ids1["authors"] == ["a-1"]

        # second persist of the same logical batch must not raise (idempotent)
        ids2 = await store.save_batch(papers=[paper], authors=[author], citations=[])
        assert ids2["papers"] == ["p-1"]
        assert ids2["authors"] == ["a-1"]

        stats = await store.get_stats()
        assert stats["total_papers"] == 1
        assert stats["total_authors"] == 1

        loaded = await store.get_paper("p-1")
        assert loaded is not None
        assert loaded.title == paper.title
        assert loaded.evidence_list  # evidence rows still attached
        loaded_author = await store.get_author("a-1")
        assert loaded_author is not None
        assert loaded_author.name == author.name
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_updates_existing_fields(tmp_path) -> None:
    """C-1: a batch entry with an existing id updates the stored record."""
    db = tmp_path / "batch_upd.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        first = _sample_paper().model_copy(
            update={"id": "p-1", "title": "First Title", "year": 2016}
        )
        await store.save_batch(papers=[first])

        updated = first.model_copy(update={"title": "Updated Title", "year": 2020})
        await store.save_batch(papers=[updated])

        loaded = await store.get_paper("p-1")
        assert loaded is not None
        assert loaded.title == "Updated Title"
        assert loaded.year == 2020
        stats = await store.get_stats()
        assert stats["total_papers"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_connect_enables_wal_and_busy_timeout(tmp_path) -> None:
    """I-12: WAL journal mode and busy_timeout are configured on connect."""
    from sqlalchemy import text

    db = tmp_path / "wal.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        assert store._engine is not None
        async with store._engine.connect() as conn:
            journal = (await conn.execute(text("PRAGMA journal_mode"))).fetchone()[0]
            timeout = (await conn.execute(text("PRAGMA busy_timeout"))).fetchone()[0]
        assert journal == "wal"
        assert timeout == 10000
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_author_identity_fields_roundtrip(tmp_path, backend: str) -> None:
    """v2 identity/disambiguation fields survive a save/load cycle (B4)."""
    if backend == "sqlite":
        store = SQLiteStorage(str(tmp_path / "identity.db"))
    else:
        store = JSONStorage(str(tmp_path / "identity"))
    await store.connect()
    try:
        author_id = await store.save_author(_identity_author())
        loaded = await store.get_author(author_id)
        assert loaded is not None
        assert loaded.orcid == "0000-0001-2345-6789"
        assert loaded.semantic_scholar_id == "S2-123"
        assert loaded.openalex_id == "A42"
        assert loaded.aliases == ["W. Zhang", "Zhang Wei"]
        assert loaded.disambiguation_status == "ambiguous"
        assert loaded.coauthors == ["Alice", "Bob"]
        assert loaded.venues == ["NeurIPS"]
        assert loaded.active_years == [2018, 2019, 2020]

        # update_author preserves them too
        updated = loaded.model_copy(update={"disambiguation_status": "confirmed"})
        assert await store.update_author(author_id, updated) is True
        again = await store.get_author(author_id)
        assert again is not None
        assert again.disambiguation_status == "confirmed"
        assert again.orcid == "0000-0001-2345-6789"
    finally:
        await store.close()
