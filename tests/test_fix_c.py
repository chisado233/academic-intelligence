"""FIX-C tests: save_batch duplicate-author dedup (F1) + graph closure fixture (F2).

F1 (P19 regression): real OpenAlex payloads can list the same author id more
than once in one work's ``authorships`` (e.g. ``A5006539124`` appears twice in
the stroke-paper payload); persisting that paper crashed on the
``UNIQUE(authorships.paper_id, author_id)`` constraint.  The pre-FIX-B shape
(unresolved byline, two same-name ``~Name`` keys) hit the same UNIQUE crash.

F2: the real-payload closure flows verified in B7-P19 are frozen as offline
cassette tests (no network): real payload parse, author-id linkage,
``expand(A-id, ["papers"])`` loaded nodes, W-id backfill, and
``collect_citations`` persisting citing papers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.models import AuthorRef, Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.storage.sqlite_store import (
    AuthorshipRow,
    CoauthorshipRow,
    SQLiteStorage,
)
from tests.cassette_replay import install_cassette, load_cassette

_OPENALEX = "https://openalex.org"


def _ev(conf: float = 0.9) -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url=f"{_OPENALEX}/W1",
        confidence=conf,
    )


def _dup_paper(paper_id: str = "p-dup") -> Paper:
    """Byline with the same author id twice + two same-name unresolved refs."""
    return Paper(
        id=paper_id,
        title="Duplicate Byline Authors",
        authors=[
            AuthorRef(author_id="A1", name="X", position=1),
            AuthorRef(author_id="A1", name="X", position=2),
            AuthorRef(author_id=None, name="Y", position=3),
            AuthorRef(author_id=None, name="Y", position=4),
        ],
        year=2020,
        evidence_list=[_ev()],
    )


async def _authorship_keys(store: SQLiteStorage, paper_id: str) -> list[str]:
    async with store._session() as session:
        stmt = (
            select(AuthorshipRow.author_id)
            .where(AuthorshipRow.paper_id == paper_id)
            .order_by(AuthorshipRow.position)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def _coauthorship_rows(store: SQLiteStorage) -> list[tuple[str, str, int]]:
    async with store._session() as session:
        result = await session.execute(
            select(
                CoauthorshipRow.author_a_id,
                CoauthorshipRow.author_b_id,
                CoauthorshipRow.paper_count,
            )
        )
        return [(a, b, c) for a, b, c in result.all()]


# ---------------------------------------------------------------------------
# F1: save_batch / save_paper dedup duplicate (paper_id, author_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_batch_dedups_duplicate_author_ids(tmp_path: Path) -> None:
    """F1: a byline with a duplicated author id persists without UNIQUE crash.

    ``save_batch`` must keep one authorship edge per (paper_id, author_id)
    instead of raising StorageError on the UNIQUE constraint.  The paper is
    still queryable via ``get_author_papers`` exactly once.
    """
    db = tmp_path / "dup.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        ids = await store.save_batch(papers=[_dup_paper()])
        assert ids["papers"] == ["p-dup"]

        assert await store.get_author_papers("A1") == ["p-dup"]
        stats = await store.get_stats()
        assert stats["total_papers"] == 1

        # authorship edges: A1 + ~Y only, no duplicates
        keys = await _authorship_keys(store, "p-dup")
        assert sorted(keys) == ["A1", "~Y"]
        assert len(keys) == len(set(keys))

        # only resolved authors enter coauthorships; a single resolved author
        # means no coauthorship rows are written
        assert await _coauthorship_rows(store) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_paper_dedups_duplicate_author_ids(tmp_path: Path) -> None:
    """F1: the single-record save path dedups too (no UNIQUE crash)."""
    db = tmp_path / "dup_single.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        paper_id = await store.save_paper(_dup_paper())
        assert paper_id == "p-dup"
        assert await store.get_author_papers("A1") == ["p-dup"]
        keys = await _authorship_keys(store, "p-dup")
        assert sorted(keys) == ["A1", "~Y"]

        # in-place update (count_coauthorships=False) must not crash either
        updated = _dup_paper().model_copy(update={"title": "Updated Title"})
        await store.save_paper(updated)
        assert await store.get_author_papers("A1") == ["p-dup"]
        keys = await _authorship_keys(store, "p-dup")
        assert sorted(keys) == ["A1", "~Y"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_dedups_duplicate_unresolved_names(tmp_path: Path) -> None:
    """F1: two same-name unresolved authors (``~Name`` keys) don't collide.

    This is the pre-FIX-B byline shape: no author_id, same name twice — the
    ``~Name`` pseudo keys used to hit the same UNIQUE constraint.
    """
    db = tmp_path / "dup_name.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        paper = Paper(
            id="p-name",
            title="Unresolved Dup",
            authors=[
                AuthorRef(author_id=None, name="Y", position=1),
                AuthorRef(author_id=None, name="Y", position=2),
                AuthorRef(author_id="A2", name="Z", position=3),
            ],
            evidence_list=[_ev()],
        )
        await store.save_batch(papers=[paper])
        keys = await _authorship_keys(store, "p-name")
        assert sorted(keys) == ["A2", "~Y"]
        assert len(keys) == len(set(keys))
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_dedup_coauthorship_not_self_doubled(tmp_path: Path) -> None:
    """F1: duplicate author ids never create self-pairs or double counts.

    Byline ``[A1, A1, A2]`` dedups to ``[A1, A2]``: exactly one
    ``(A1, A2, 1)`` coauthorship row, and no ``(A1, A1)`` self edge.
    """
    db = tmp_path / "dup_coauth.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        paper = Paper(
            id="p-co",
            title="Coauthorship Dup",
            authors=[
                AuthorRef(author_id="A1", name="X", position=1),
                AuthorRef(author_id="A1", name="X", position=2),
                AuthorRef(author_id="A2", name="Z", position=3),
            ],
            year=2020,
            evidence_list=[_ev()],
        )
        await store.save_batch(papers=[paper])
        co = await _coauthorship_rows(store)
        assert co == [("A1", "A2", 1)]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F2: graph closure frozen as offline fixture tests (B7-P19 real payloads)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_closure() -> dict[str, Any]:
    """Real OpenAlex payloads recorded 2026-08-07 (cassette fixture)."""
    return load_cassette("openalex_real_closure")


def _parse_works(
    fixture: dict[str, Any], work_ids: list[str]
) -> list[Paper]:
    src = OpenAlexSource(http_client=MagicMock())
    return [src._parse_paper(fixture["raw_works"][wid]) for wid in work_ids]


def test_real_payload_parse_fills_author_ids_and_references(
    real_closure: dict[str, Any],
) -> None:
    """F2a: the real "Deep learning" payload parses to non-None author ids."""
    src = OpenAlexSource(http_client=MagicMock())
    raw = real_closure["raw_works"]["W2919115771"]
    paper = src._parse_paper(raw)
    assert paper.id == "W2919115771"
    assert paper.title == "Deep learning"
    assert paper.authors, "real payload must carry a byline"
    assert all(a.author_id for a in paper.authors), "author ids must resolve"
    assert {a.author_id for a in paper.authors} == {
        "A5001226970",
        "A5086198262",
        "A5108093963",
    }
    assert paper.references, "real payload must carry referenced works"
    assert len(paper.references) == 53
    assert "W146900863" in paper.references


def test_real_payload_duplicate_author_id_survives_parse(
    real_closure: dict[str, Any],
) -> None:
    """F2a: the stroke paper really repeats A5006539124 in its byline."""
    src = OpenAlexSource(http_client=MagicMock())
    paper = src._parse_paper(real_closure["raw_works"]["W2923418412"])
    ids = [a.author_id for a in paper.authors]
    assert ids.count("A5006539124") == 2
    assert sum(1 for a in paper.authors if a.author_id is None) == 2


@pytest.mark.asyncio
async def test_real_dup_author_payload_saves_cleanly(
    tmp_path: Path,
    real_closure: dict[str, Any],
) -> None:
    """F1+F2: the real duplicate-author payload persists without crashing."""
    db = tmp_path / "real_dup.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        paper = _parse_works(real_closure, ["W2923418412"])[0]
        await store.save_batch(papers=[paper])
        assert await store.get_author_papers("A5006539124") == ["W2923418412"]
        keys = await _authorship_keys(store, "W2923418412")
        assert keys.count("A5006539124") == 1
        assert len(keys) == len(set(keys))
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_author_linkage_after_real_payload_persist(
    tmp_path: Path,
    real_closure: dict[str, Any],
) -> None:
    """F2b: real A-id bylines make ``get_author_papers(A-id)`` answer."""
    db = tmp_path / "linkage.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        src = OpenAlexSource(http_client=MagicMock())
        papers = _parse_works(real_closure, ["W2919115771", "W1498436455"])
        author = src._parse_author(real_closure["raw_author"]["A5108093963"])
        await store.save_batch(authors=[author], papers=papers)
        found = await store.get_author_papers("A5108093963")
        assert set(found) == {"W2919115771", "W1498436455"}
        assert found == list(dict.fromkeys(found)), "no duplicate paper ids"
    finally:
        await store.close()


def _offline_ai(tmp_path: Path, name: str) -> tuple[AcademicIntelligence, str]:
    """AI instance wired to a throwaway SQLite DB (no HTTP cache)."""
    db = tmp_path / name
    ai = AcademicIntelligence(
        config=Config(
            sources=["openalex"],
            storage_type="sqlite",
            storage_path=str(db),
            cache_enabled=False,
        )
    )
    return ai, str(db)


@pytest.mark.asyncio
async def test_expand_author_papers_returns_loaded_nodes(
    tmp_path: Path,
    real_closure: dict[str, Any],
) -> None:
    """F2c: ``expand(A-id, ["papers"])`` returns loaded paper nodes."""
    ai, _ = _offline_ai(tmp_path, "expand_author.db")
    await ai.connect()
    try:
        src = OpenAlexSource(http_client=MagicMock())
        papers = _parse_works(real_closure, ["W2919115771", "W1498436455"])
        author = src._parse_author(real_closure["raw_author"]["A5108093963"])
        await ai.storage.save_batch(authors=[author], papers=papers)

        result = await ai.expand("A5108093963", relations=["papers"], depth=1)
        assert result.stats.failed == 0
        assert result.stats.nodes_found == 2
        assert result.nodes, "author expansion must discover paper nodes"
        assert all(node.get("loaded") is True for node in result.nodes)
        assert all(node.get("title") for node in result.nodes)
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_work_id_backfill_via_expand_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_closure: dict[str, Any],
) -> None:
    """F2d: a deleted citing paper is backfilled by W-id through expand."""
    install_cassette(monkeypatch, "openalex_real_closure")
    ai, _ = _offline_ai(tmp_path, "backfill.db")
    await ai.connect()
    try:
        victim = "W1498436455"
        paper = _parse_works(real_closure, [victim])[0]
        await ai.storage.save_batch(papers=[paper])
        assert await ai.storage.delete_paper(victim) is True

        result = await ai.expand(victim, relations=["references"], depth=1)
        assert result.stats.failed == 0
        # FIX-D-3/D-5: once the center record is restored (with its persisted
        # references column) the expansion is served from storage; fetched_new
        # is 0 instead of the previous redundant re-fetch count of 4.
        assert result.stats.fetched_new == 0

        backfilled = await ai.storage.get_paper(victim)
        assert backfilled is not None
        assert backfilled.title == "Learning representations by back-propagating errors"
        assert backfilled.year == 1986
        assert len(backfilled.references or []) == 4
        assert set(backfilled.references or []) == {
            "W102612133",
            "W2322002063",
            "W3207342693",
            "W4300402905",
        }
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_collect_citations_persists_citing_papers_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_closure: dict[str, Any],
) -> None:
    """F2e: ``collect_citations`` persists real citing works (loaded=True)."""
    install_cassette(monkeypatch, "openalex_real_closure")
    ai, _ = _offline_ai(tmp_path, "collect_citations.db")
    await ai.connect()
    try:
        result = await ai.collect_citations(
            "W2919115771", sources=["openalex"], persist=True
        )
        citing_ids = {p.id for p in result.papers if p.id}
        assert len(result.citations) == len(citing_ids) == 3
        assert citing_ids == {"W2473156356", "W2752849906", "W2781738013"}

        for cid in citing_ids:
            stored = await ai.storage.get_paper(cid)
            assert stored is not None, f"{cid} must be persisted"
            assert stored.title, f"{cid} must be loaded with a title"
        assert set(await ai.storage.get_citations("W2919115771")) == citing_ids
    finally:
        await ai.close()
