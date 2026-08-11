"""FIX-G: author-filter pushdown (G2), save_batch batch writes (G1),
collect-persist entity sync (G6).

Covers:
- F1: ``query_papers(author=...)`` must not leak matches past the SQL
  ``LIMIT`` window (first 100 rows can shadow a matching author); normalized
  matching ("Geoffrey Hinton" -> "Geoffrey E. Hinton") is preserved on both
  backends.
- F2: ``save_batch`` keeps upsert / atomic / edge semantics on the batched
  write path (idempotent re-save, evidence replacement, authorship +
  coauthorship edges, duplicate ids inside one batch).
- F3: ``collect_author_papers`` / ``collect_paper`` with ``persist=True``
  record the (entity, source) sync timestamp so an immediate ``update_*``
  call is short-circuited by the stale gate instead of re-pulling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    CollectionResult,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.processors.incremental import author_entity_key
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(source: SourceType = SourceType.OPENALEX, conf: float = 0.8) -> Evidence:
    return Evidence(source=source, source_url="https://e.com", confidence=conf)


def _paper(**kwargs: object) -> Paper:
    defaults: dict[str, object] = {
        "title": "FIX-G Paper",
        "authors": ["Ada"],
        "year": 2020,
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Paper(**defaults)  # type: ignore[arg-type]


def _make_store(tmp_path: Path, backend: str) -> SQLiteStorage | JSONStorage:
    if backend == "sqlite":
        return SQLiteStorage(str(tmp_path / "g.db"))
    return JSONStorage(str(tmp_path / "g"))


# ---------------------------------------------------------------------------
# F1 (G2): query_papers(author=...) filter pushdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_query_papers_author_filter_no_leak_past_limit(
    tmp_path: Path, backend: str
) -> None:
    """G2: an author whose papers sort beyond the SQL LIMIT window must not be
    missed.  The first 100 stored papers belong to "Author Alpha" and the last
    100 to "Author Beta"; the pre-fix path returned 0 rows for Beta."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        alpha = [
            _paper(
                id=f"a-{i:03d}",
                title=f"Alpha Paper {i}",
                authors=[AuthorRef(name="Author Alpha", position=1)],
                year=2020,
            )
            for i in range(100)
        ]
        beta = [
            _paper(
                id=f"b-{i:03d}",
                title=f"Beta Paper {i}",
                authors=[AuthorRef(name="Author Beta", position=1)],
                year=2021,
            )
            for i in range(100)
        ]
        await store.save_batch(papers=alpha + beta)

        got = await store.query_papers(author="Author Beta", limit=100)
        assert len(got) == 100
        assert all(any(a.name == "Author Beta" for a in p.authors) for p in got)

        got50 = await store.query_papers(author="Author Alpha", limit=50)
        assert len(got50) == 50
        assert all(any(a.name == "Author Alpha" for a in p.authors) for p in got50)

        # pagination is applied over the filtered set, not the raw rows
        page2 = await store.query_papers(author="Author Beta", limit=50, offset=50)
        assert len(page2) == 50
        assert all(any(a.name == "Author Beta" for a in p.authors) for p in page2)

        # author filter composes with other SQL filters
        yr = await store.query_papers(author="Author Beta", year=2021, limit=100)
        assert len(yr) == 100
        none = await store.query_papers(author="Author Beta", year=1999, limit=100)
        assert none == []
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_query_papers_author_normalized_match_preserved(
    tmp_path: Path, backend: str
) -> None:
    """Normalized matching ("Geoffrey Hinton" -> "Geoffrey E. Hinton") still
    works after the SQL pushdown, and unrelated names do not match."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _paper(
                    id="w1",
                    title="Deep Learning",
                    authors=[AuthorRef(name="Geoffrey E. Hinton", position=1)],
                ),
                _paper(
                    id="w2",
                    title="Other",
                    authors=[AuthorRef(name="Drew Dennett", position=1)],
                ),
            ]
        )
        assert [p.id for p in await store.query_papers(author="Geoffrey Hinton")] == ["w1"]
        assert [p.id for p in await store.query_papers(author="Hinton")] == ["w1"]
        assert [p.id for p in await store.query_papers(author="Drew Dennett")] == ["w2"]
        assert await store.query_papers(author="Ada Lovelace") == []
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F2 (G1): save_batch batch write path semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_batch_batched_upsert_keeps_semantics(tmp_path: Path) -> None:
    """F2: the batched path preserves upsert / evidence / authorship behavior."""
    store = SQLiteStorage(str(tmp_path / "f2.db"))
    await store.connect()
    try:
        papers = [
            Paper(
                id=f"p-{i}",
                title=f"Title {i}",
                authors=[
                    AuthorRef(name=f"Author {i}", position=1),
                    AuthorRef(name="Coauthor", position=2),
                ],
                year=2000 + (i % 25),
                evidence=_ev(),
            )
            for i in range(50)
        ]
        ids = await store.save_batch(papers=papers)
        assert ids["papers"] == [f"p-{i}" for i in range(50)]

        loaded = await store.get_paper("p-0")
        assert loaded is not None
        assert loaded.evidence_list
        assert [a.name for a in loaded.authors] == ["Author 0", "Coauthor"]

        # re-saving the same ids updates in place without duplicates
        changed = [
            p.model_copy(update={"title": f"New Title {i}"}) for i, p in enumerate(papers)
        ]
        ids2 = await store.save_batch(papers=changed)
        assert ids2["papers"] == [f"p-{i}" for i in range(50)]
        stats = await store.get_stats()
        assert stats["total_papers"] == 50
        loaded = await store.get_paper("p-10")
        assert loaded is not None
        assert loaded.title == "New Title 10"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_authors_upsert_and_evidence(tmp_path: Path) -> None:
    """F2: author batch writes persist evidence and upsert in place."""
    store = SQLiteStorage(str(tmp_path / "f2a.db"))
    await store.connect()
    try:
        authors = [
            Author(
                id=f"au-{i}",
                name=f"Name {i}",
                affiliation="UdeM",
                evidence=_ev(),
            )
            for i in range(20)
        ]
        ids = await store.save_batch(authors=authors)
        assert ids["authors"] == [f"au-{i}" for i in range(20)]

        got = await store.get_author("au-5")
        assert got is not None
        assert got.affiliation == "UdeM"
        assert got.evidence_list

        await store.save_batch(
            authors=[authors[5].model_copy(update={"affiliation": "MILA"})]
        )
        got = await store.get_author("au-5")
        assert got is not None
        assert got.affiliation == "MILA"
        stats = await store.get_stats()
        assert stats["total_authors"] == 20
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_coauthorships_counted_once_per_new_paper(
    tmp_path: Path,
) -> None:
    """F2: coauthorship edges are created for new papers and NOT re-counted
    on idempotent re-saves of the same id."""
    from sqlalchemy import select

    from academic_intelligence.storage.sqlite_store import CoauthorshipRow

    store = SQLiteStorage(str(tmp_path / "f2c.db"))
    await store.connect()
    try:

        async def _counts() -> dict[tuple[str, str], int]:
            async with store._session() as s:
                rows = (await s.execute(select(CoauthorshipRow))).scalars().all()
                return {
                    (r.author_a_id, r.author_b_id): r.paper_count for r in rows
                }

        paper = Paper(
            id="p-co",
            title="Coauth",
            authors=[
                AuthorRef(author_id="A1", name="Alice", position=1),
                AuthorRef(author_id="A2", name="Bob", position=2),
                AuthorRef(author_id="A3", name="Carol", position=3),
            ],
            year=2020,
            evidence=_ev(),
        )
        await store.save_batch(papers=[paper])
        assert await _counts() == {("A1", "A2"): 1, ("A1", "A3"): 1, ("A2", "A3"): 1}

        # idempotent re-save must not double-count
        await store.save_batch(papers=[paper])
        assert await _counts() == {("A1", "A2"): 1, ("A1", "A3"): 1, ("A2", "A3"): 1}

        # a second distinct paper sharing a pair counts once more
        paper2 = paper.model_copy(
            update={
                "id": "p-co2",
                "authors": [
                    AuthorRef(author_id="A1", name="Alice", position=1),
                    AuthorRef(author_id="A2", name="Bob", position=2),
                ],
            }
        )
        await store.save_batch(papers=[paper2])
        assert await _counts() == {("A1", "A2"): 2, ("A1", "A3"): 1, ("A2", "A3"): 1}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_duplicate_id_in_single_batch(tmp_path: Path) -> None:
    """F2: the same id twice inside one batch upserts (last wins) without
    crashing on the primary key."""
    store = SQLiteStorage(str(tmp_path / "f2d.db"))
    await store.connect()
    try:
        ids = await store.save_batch(
            papers=[
                _paper(id="dup", title="First"),
                _paper(id="dup", title="Second"),
            ]
        )
        assert ids["papers"] == ["dup", "dup"]
        got = await store.get_paper("dup")
        assert got is not None
        assert got.title == "Second"
        stats = await store.get_stats()
        assert stats["total_papers"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_evidence_replaced_not_duplicated(tmp_path: Path) -> None:
    """F2: re-saving a paper replaces its evidence rows instead of stacking."""
    store = SQLiteStorage(str(tmp_path / "f2e.db"))
    await store.connect()
    try:
        await store.save_batch(papers=[_paper(id="p-ev", title="T", evidence=_ev())])
        first = await store.get_paper("p-ev")
        assert first is not None
        assert len(first.evidence_list) == 1

        await store.save_batch(
            papers=[_paper(id="p-ev", title="T", evidence=_ev(SourceType.ARXIV, 0.5))]
        )
        again = await store.get_paper("p-ev")
        assert again is not None
        assert [e.source for e in again.evidence_list] == [SourceType.ARXIV]
        assert len(again.evidence_list) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_atomic_rollback_on_failure(tmp_path: Path) -> None:
    """F2: a mid-batch failure rolls the whole batch back (atomicity)."""
    import academic_intelligence.storage.sqlite_store as store_mod

    from academic_intelligence.core.exceptions import StorageError

    store = SQLiteStorage(str(tmp_path / "f2at.db"))
    await store.connect()
    try:
        original = store_mod._apply_coauthorship_deltas

        async def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("boom")

        store_mod._apply_coauthorship_deltas = boom  # type: ignore[assignment]
        try:
            with pytest.raises(StorageError):
                await store.save_batch(papers=[_paper(id="p-atomic", title="T")])
        finally:
            store_mod._apply_coauthorship_deltas = original

        stats = await store.get_stats()
        assert stats["total_papers"] == 0
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F3 (G6): collect persist -> entity_sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_collect_author_papers_persist_syncs_entity_and_short_circuits(
    tmp_path: Path, backend: str
) -> None:
    """G6: collect_author_papers(persist=True) records the author entity sync
    so an immediate update_author_papers is short-circuited (total_checked=0)."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type=backend,
            storage_path=(
                str(tmp_path / "f3a.db") if backend == "sqlite" else str(tmp_path / "f3a")
            ),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:

        async def fake_collect(name: str, **kwargs: object) -> CollectionResult:
            return CollectionResult(
                papers=[
                    _paper(
                        title=f"Paper of {name}",
                        authors=[AuthorRef(name=name, position=1)],
                    )
                ]
            )

        assert ai._collector is not None
        ai._collector.collect_author_papers = fake_collect  # type: ignore[method-assign]
        result = await ai.collect_author_papers(
            "Ada Lovelace", sources=["openalex"], persist=True
        )
        assert result.papers

        last = await ai.storage.get_entity_sync(
            "author", author_entity_key("Ada Lovelace"), "openalex"
        )
        assert last is not None

        update = await ai.update_author_papers("Ada Lovelace", sources=["openalex"])
        assert update.total_checked == 0
        assert update.new == []
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_collect_paper_persist_syncs_entity_and_short_circuits(
    tmp_path: Path,
) -> None:
    """G6: collect_paper(persist=True) records the paper entity sync so an
    immediate update_paper is short-circuited."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="sqlite",
            storage_path=str(tmp_path / "f3p.db"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:

        async def fake_collect(query: str, **kwargs: object) -> CollectionResult:
            return CollectionResult(
                papers=[
                    _paper(
                        id="w-f3",
                        title="Collected",
                        authors=[AuthorRef(name="Ada", position=1)],
                    )
                ]
            )

        assert ai._collector is not None
        ai._collector.collect_paper = fake_collect  # type: ignore[method-assign]
        await ai.collect_paper("10.1234/abc", sources=["openalex"], persist=True)

        last = await ai.storage.get_entity_sync("paper", "w-f3", "openalex")
        assert last is not None

        update = await ai.update_paper("w-f3", sources=["openalex"])
        assert update.total_checked == 0
    finally:
        await ai.close()
