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
