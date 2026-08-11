"""Cross-backend correctness regressions found during dogfooding."""

from __future__ import annotations

from pathlib import Path

import pytest

from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import Author, AuthorRef, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _evidence() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W-hardening",
    )


def _store(tmp_path: Path, backend: str) -> JSONStorage | SQLiteStorage:
    if backend == "json":
        return JSONStorage(str(tmp_path / "json-store"))
    return SQLiteStorage(str(tmp_path / "store.db"))


@pytest.mark.asyncio
async def test_json_store_rejects_two_live_writers_for_same_directory(
    tmp_path: Path,
) -> None:
    first = JSONStorage(str(tmp_path / "shared"))
    second = JSONStorage(str(tmp_path / "shared" / "."))
    await first.connect()
    try:
        with pytest.raises(StorageError, match="already open"):
            await second.connect()
    finally:
        await first.close()

    await second.connect()
    await second.close()


@pytest.mark.asyncio
async def test_json_store_rejects_writes_after_close(tmp_path: Path) -> None:
    store = JSONStorage(str(tmp_path / "closed"))
    await store.connect()
    await store.close()

    with pytest.raises(StorageError, match="not connected"):
        await store.save_paper(Paper(title="must not persist"))


def test_author_interests_are_normalized_to_nfc() -> None:
    author = Author(name="Ada", interests=["Cafe\u0301 computing"])
    assert author.interests == ["Café computing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["json", "sqlite"])
async def test_interest_query_matches_canonically_equivalent_unicode(
    tmp_path: Path,
    backend: str,
) -> None:
    store = _store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_author(
            Author(
                id="author-1",
                name="Ada",
                interests=["Café computing"],
                evidence=_evidence(),
            )
        )
        matches = await store.query_authors(interest="Cafe\u0301")
        assert [author.id for author in matches] == ["author-1"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["json", "sqlite"])
async def test_keyword_query_includes_structured_keyword_values(
    tmp_path: Path,
    backend: str,
) -> None:
    store = _store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_paper(
            Paper(
                id="paper-keyword",
                title="A neutral title",
                abstract="No matching phrase here",
                keywords=["graph-neural-network"],
                evidence=_evidence(),
            )
        )
        matches = await store.query_papers(keyword="neural")
        assert [paper.id for paper in matches] == ["paper-keyword"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_citation_upsert_returns_persisted_id_for_single_and_batch(
    tmp_path: Path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "citations.db"))
    citation = Citation(
        citing_paper_id="paper-a",
        cited_paper_id="paper-b",
        evidence=_evidence(),
    )
    await store.connect()
    try:
        first = await store.save_citation(citation)
        second = await store.save_citation(citation)
        batch = await store.save_batch(citations=[citation, citation])

        assert second == first
        assert batch["citations"] == [first, first]
        assert (await store.get_stats())["total_citations"] == 1
    finally:
        await store.close()


def _byline(*author_ids: str) -> list[AuthorRef]:
    return [
        AuthorRef(author_id=author_id, name=author_id.title(), position=position)
        for position, author_id in enumerate(author_ids, start=1)
    ]


@pytest.mark.asyncio
async def test_sqlite_coauthorships_converge_after_update_delete_and_batch(
    tmp_path: Path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "coauthors.db"))
    await store.connect()
    try:
        for author_id in ("alice", "bob", "carol"):
            await store.save_author(Author(id=author_id, name=author_id.title()))

        await store.save_paper(
            Paper(id="paper-one", title="One", authors=_byline("alice", "bob"))
        )
        assert await store.get_coauthors("alice") == ["bob"]

        await store.update_paper(
            "paper-one",
            Paper(id="paper-one", title="One", authors=_byline("alice", "carol")),
        )
        assert await store.get_coauthors("alice") == ["carol"]
        assert await store.get_coauthors("bob") == []

        await store.delete_paper("paper-one")
        assert await store.get_coauthors("alice") == []
        assert await store.get_coauthors("carol") == []

        await store.save_paper(
            Paper(id="paper-two", title="Two", authors=_byline("alice", "bob"))
        )
        await store.save_batch(
            papers=[
                Paper(
                    id="paper-two",
                    title="Two revised",
                    authors=_byline("alice", "carol"),
                )
            ]
        )
        assert await store.get_coauthors("alice") == ["carol"]
        assert await store.get_coauthors("bob") == []
    finally:
        await store.close()
