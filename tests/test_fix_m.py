"""FIX-M ticket tests (B7-P30 round 12 defects).

- F1 (M1): author→paper persistence linkage for sources without a stable
  author id (pubmed / arxiv bylines). ``save_batch`` / ``save_paper`` must
  re-key ``~name`` authorship edges to the same-name ``Author`` record, so
  ``get_author_papers(<author id>)`` and ``expand(author, ["papers"])`` serve
  from storage instead of returning nothing.
- F2 (M2): arXiv compact ``journal_ref`` noise (``42:60-88``,
  ``33(4):1234-1245``) is stripped so the venue keeps the bare journal name
  and no fake venue-conflict warning fires.
- F3 (M3): expand stops materializing neighbors once the node budget is
  exhausted instead of ``storage.get_paper``-ing every candidate first.
- F4 (M4): per-source evidence confidence matches the scorer baseline table.
- M6: the sqlite author prefilter treats ``%`` / ``_`` literally (same
  semantics as venue / keyword, same results as the JSON backend).
- M7: ``collect`` entry points reject negative ``limit`` with ``ValueError``
  (matching ``query_papers``).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from academic_intelligence import AcademicIntelligence
from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.models import Author, AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.graph import KnowledgeGraph
from academic_intelligence.graph.traversal import expand_from_graph
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.processors.scorer import SOURCE_BASELINE_CONFIDENCE
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.ieee import IEEESource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.sources.pubmed import PubMedSource
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(source: SourceType, conf: float, sid: str) -> Evidence:
    return Evidence(
        source=source,
        source_url=f"https://{source.value}/record",
        source_id=sid,
        confidence=conf,
        collected_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# F1 (M1): author→paper persistence linkage
# ---------------------------------------------------------------------------


def _alice_author() -> Author:
    return Author(
        name="Alice Smith",
        evidence_list=[_ev(SourceType.PUBMED, 0.92, "pm-alice")],
    )


def _alice_paper(paper_id: str = "p-alice") -> Paper:
    return Paper(
        id=paper_id,
        title="A Paper by Alice",
        year=2024,
        authors=[AuthorRef(name="Alice Smith", position=1)],
        evidence_list=[_ev(SourceType.PUBMED, 0.92, "pm-p1")],
    )


class _MinimalCollector:
    """Collector stub; storage-hit expands never touch it."""

    async def collect_citations(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("storage hit must not fetch")

    async def collect_paper(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("storage hit must not fetch")

    async def collect_author_papers(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("storage hit must not fetch")


@pytest.mark.asyncio
async def test_fix_m_f1_save_batch_links_name_only_author(tmp_path: Path) -> None:
    """M1: persisting a pubmed-style author (no stable id) together with her
    papers must create author→paper edges keyed to the author record id —
    ``get_author_papers`` serves the paper instead of returning nothing."""
    store = SQLiteStorage(str(tmp_path / "f1.db"))
    await store.connect()
    try:
        ids = await store.save_batch(
            authors=[_alice_author()],
            papers=[_alice_paper()],
        )
        author_id = ids["authors"][0]
        assert author_id
        # The authorship edge must point at the author record, not ~name.
        assert await store.get_author_papers(author_id) == ["p-alice"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_m_f1_expand_author_papers_served_from_storage(
    tmp_path: Path,
) -> None:
    """M1: after collect + persist, ``expand(author, ["papers"])`` with
    ``fetch_missing=False`` returns the paper node via the storage path."""
    store = SQLiteStorage(str(tmp_path / "f1b.db"))
    await store.connect()
    try:
        ids = await store.save_batch(
            authors=[_alice_author()],
            papers=[_alice_paper()],
        )
        author_id = ids["authors"][0]
        graph = KnowledgeGraph()
        result = await expand_from_graph(
            graph,
            store,
            _MinimalCollector(),
            author_id,
            relations=["papers"],
            fetch_missing=False,
        )
        assert {n["id"] for n in result.nodes} == {"p-alice"}
        assert result.stats.nodes_found == 1
        node = graph.get_node("p-alice")
        assert node is not None
        assert node["loaded"] is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_m_f1_pre_existing_author_linked_from_later_batch(
    tmp_path: Path,
) -> None:
    """M1: an author persisted in an earlier batch is matched by name when a
    later batch persists papers carrying her byline."""
    store = SQLiteStorage(str(tmp_path / "f1c.db"))
    await store.connect()
    try:
        first = await store.save_batch(authors=[_alice_author()], papers=[])
        author_id = first["authors"][0]
        await store.save_batch(papers=[_alice_paper()])
        assert await store.get_author_papers(author_id) == ["p-alice"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_m_f1_unmatched_name_keeps_pseudo_key(tmp_path: Path) -> None:
    """M1: a byline name with no matching Author record keeps the ``~name``
    pseudo-key fallback (no resolution, no crash)."""
    store = SQLiteStorage(str(tmp_path / "f1d.db"))
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                Paper(
                    id="p-ghost",
                    title="Ghost",
                    authors=[AuthorRef(name="Nobody Else", position=1)],
                    evidence_list=[_ev(SourceType.PUBMED, 0.92, "pm-g")],
                )
            ]
        )
        assert await store.get_author_papers("~Nobody Else") == ["p-ghost"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_m_f1_save_paper_links_name_only_author(tmp_path: Path) -> None:
    """M1: the single-record ``save_paper`` path links name-only bylines to a
    persisted same-name author too."""
    store = SQLiteStorage(str(tmp_path / "f1e.db"))
    await store.connect()
    try:
        author_id = await store.save_author(_alice_author())
        await store.save_paper(_alice_paper())
        assert await store.get_author_papers(author_id) == ["p-alice"]
    finally:
        await store.close()


class _PubmedLikeSource:
    """A pubmed-style source: no stable author ids, name-only bylines."""

    name = "pubmed_like"
    source_type = SourceType.PUBMED

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return []

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        first = _alice_paper()
        second = _alice_paper("p-alice2").model_copy(
            update={"title": "Another Paper by Alice"}
        )
        return [first, second]

    async def get_author_profile(self, author_name: str) -> Author | None:
        return _alice_author()

    async def get_paper_by_id(self, work_id: str) -> Paper | None:
        return None

    async def get_citations(self, paper_id: str) -> list[Any]:
        return []

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_fix_m_f1_collect_persist_expand_end_to_end(tmp_path: Path) -> None:
    """M1 end-to-end (dispatch acceptance): collect a pubmed-style author,
    persist, then ``get_author_papers`` and ``expand(author, ["papers"])``
    with ``fetch_missing=False`` return the papers from storage."""
    from academic_intelligence.core.types import Config

    store = SQLiteStorage(str(tmp_path / "f1e2e.db"))
    await store.connect()
    try:
        collector = MultiSourceCollector(config=Config(), sources=[_PubmedLikeSource()])
        result = await collector.collect_author_papers("Alice Smith")
        assert result.authors, "author profile must be collected"
        ids = await store.save_batch(authors=result.authors, papers=result.papers)
        author_id = ids["authors"][0]
        assert author_id
        assert await store.get_author_papers(author_id) == ["p-alice", "p-alice2"]

        graph = KnowledgeGraph()
        exp = await expand_from_graph(
            graph,
            store,
            _MinimalCollector(),
            author_id,
            relations=["papers"],
            fetch_missing=False,
        )
        assert {n["id"] for n in exp.nodes} == {"p-alice", "p-alice2"}
        assert exp.stats.nodes_found == 2
        assert exp.stats.failed == 0
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F2 (M2): arXiv compact journal_ref cleanup
# ---------------------------------------------------------------------------


def _arxiv_feed(journal_ref: str | None = None) -> str:
    jr = f"<arxiv:journal_ref>{journal_ref}</arxiv:journal_ref>" if journal_ref else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <published>2023-01-01T00:00:00Z</published>
    <title>A Test Paper Title</title>
    <summary>A short abstract.</summary>
    <author><name>Jane Doe</name></author>
    <link href="http://arxiv.org/abs/2301.00001v1" rel="alternate" type="text/html"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    {jr}
  </entry>
</feed>"""


async def _parse_arxiv_single(journal_ref: str | None = None) -> Paper:
    http = MagicMock()
    http.get = AsyncMock(
        return_value=MagicMock(status_code=200, text=_arxiv_feed(journal_ref))
    )
    source = ArxivSource(http_client=http, min_interval_seconds=0.01)
    papers = await source.search_papers("test", limit=10)
    assert len(papers) == 1
    return papers[0]


@pytest.mark.asyncio
async def test_fix_m_f2_compact_volume_pages_cleaned() -> None:
    """"Med Image Anal. 42:60-88" keeps only the bare journal name."""
    paper = await _parse_arxiv_single("Med Image Anal. 42:60-88")
    assert paper.venue == "Med Image Anal."


@pytest.mark.asyncio
async def test_fix_m_f2_compact_issue_pages_cleaned() -> None:
    """"J. Neurosci. 33(4):1234-1245" keeps only the bare journal name."""
    paper = await _parse_arxiv_single("J. Neurosci. 33(4):1234-1245")
    assert paper.venue == "J. Neurosci."


@pytest.mark.asyncio
async def test_fix_m_f2_no_fake_venue_conflict_compact_journal_ref() -> None:
    """M2 end-to-end: the same journal written compactly in an arXiv
    journal_ref (``Medical Image Analysis 42:60-88``) and fully in PubMed
    (``Medical image analysis``) must not produce a fake venue-conflict
    warning after the compact noise is stripped."""
    arxiv_p = await _parse_arxiv_single("Medical Image Analysis 42:60-88")
    assert arxiv_p.venue == "Medical Image Analysis"
    pubmed_p = Paper(
        title="A Test Paper Title",  # matches the arXiv feed title
        authors=[AuthorRef(name="B", position=1)],
        year=2023,
        venue="Medical image analysis",
        pmid="33000000",
        evidence_list=[_ev(SourceType.PUBMED, 0.92, "33000000")],
    )
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers([arxiv_p, pubmed_p])
    assert len(merged) == 1
    assert merged[0].venue == "Medical Image Analysis"
    assert dedup.get_warnings() == []


# ---------------------------------------------------------------------------
# F3 (M3): expand neighbor materialization short-circuit
# ---------------------------------------------------------------------------


class _CountingStorage:
    """SQLite-backed storage wrapper that counts ``get_paper`` reads."""

    def __init__(self, store: SQLiteStorage) -> None:
        self._store = store
        self.get_paper_calls = 0

    async def get_references(self, paper_id: str) -> list[str]:
        return await self._store.get_references(paper_id)

    async def get_citations(self, paper_id: str) -> list[str]:
        return await self._store.get_citations(paper_id)

    async def get_author_papers(self, author_id: str) -> list[str]:
        return await self._store.get_author_papers(author_id)

    async def get_coauthors(self, author_id: str) -> list[str]:
        return await self._store.get_coauthors(author_id)

    async def get_paper(self, paper_id: str) -> Paper | None:
        self.get_paper_calls += 1
        return await self._store.get_paper(paper_id)

    async def get_author(self, author_id: str) -> Author | None:
        return await self._store.get_author(author_id)


@pytest.mark.asyncio
async def test_fix_m_f3_star_expand_stops_materializing_at_budget(
    tmp_path: Path,
) -> None:
    """M3: expanding a 1000-reference star with ``max_nodes=50`` must not
    ``storage.get_paper`` every candidate first — the result (nodes / edges /
    truncated / no ghost edges) is identical, but only the budgeted nodes are
    materialized."""
    store = SQLiteStorage(str(tmp_path / "f3.db"))
    await store.connect()
    try:
        refs = [f"r{i:04d}" for i in range(1000)]
        await store.save_batch(
            papers=[
                Paper(
                    id="p1",
                    title="Star Center",
                    authors=[],
                    evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-p1")],
                    references=refs,
                )
            ]
        )
        counting = _CountingStorage(store)
        graph = KnowledgeGraph()
        start = time.perf_counter()
        result = await expand_from_graph(
            graph,
            counting,
            _MinimalCollector(),
            "p1",
            relations=["references"],
            fetch_missing=False,
            max_nodes=50,
        )
        elapsed = time.perf_counter() - start

        assert result.stats.nodes_found == 50
        assert result.stats.truncated is True
        assert {n["id"] for n in result.nodes} == set(refs[:50])
        assert result.stats.edges_found == 50
        # The materialization short-circuit: only the budgeted nodes were
        # read (plus the two center reads for type resolution / node ensure),
        # far below the 1000-candidate list the pre-fix code walked in full.
        assert counting.get_paper_calls <= 50 + 2, (
            f"materialized {counting.get_paper_calls} records for a 50-node "
            f"truncated expand ({elapsed:.3f}s)"
        )
        # No ghost edges: every edge endpoint is a resident node.
        for edge in graph.edges():
            assert graph.has_node(edge["source"])
            assert graph.has_node(edge["target"])
        assert result.stats.edges_found == graph.number_of_edges()
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F4 (M4): per-source evidence confidence aligned with scorer baseline
# ---------------------------------------------------------------------------


def test_fix_m_f4_source_confidence_aligned_with_scorer_baseline() -> None:
    """M4: each adapter's per-source evidence confidence equals the scorer's
    composite baseline table for the same source (no drift between the
    evidence-level and composite-level defaults). FIX-N F3 extends the check
    to semantic_scholar / ieee, whose defaults previously drifted
    (0.9 / 0.88 vs the baseline 0.88 / 0.85)."""
    assert ArxivSource().confidence == pytest.approx(
        SOURCE_BASELINE_CONFIDENCE[SourceType.ARXIV]
    )
    assert PubMedSource().confidence == pytest.approx(
        SOURCE_BASELINE_CONFIDENCE[SourceType.PUBMED]
    )
    assert OpenAlexSource().confidence == pytest.approx(
        SOURCE_BASELINE_CONFIDENCE[SourceType.OPENALEX]
    )
    assert SemanticScholarSource().confidence == pytest.approx(
        SOURCE_BASELINE_CONFIDENCE[SourceType.SEMANTIC_SCHOLAR]
    )
    assert IEEESource().confidence == pytest.approx(
        SOURCE_BASELINE_CONFIDENCE[SourceType.IEEE]
    )


# ---------------------------------------------------------------------------
# M6: author filter % / _ treated literally (sqlite == json)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_m_m6_author_filter_wildcards_literal_backends_consistent(
    tmp_path: Path,
) -> None:
    """M6: ``_`` / ``%`` in an author query must not leak into the sqlite LIKE
    prefilter as wildcards — sqlite and json backends agree on the ids."""
    papers = [
        Paper(
            id="u1",
            title="Underscore",
            authors=[AuthorRef(name="a_b", position=1)],
            evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-u1")],
        ),
        Paper(
            id="u2",
            title="Expanded",
            authors=[AuthorRef(name="acb", position=1)],
            evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-u2")],
        ),
        Paper(
            id="u3",
            title="Percent",
            authors=[AuthorRef(name="100% Club", position=1)],
            evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-u3")],
        ),
    ]
    sqlite_store = SQLiteStorage(str(tmp_path / "m6.db"))
    json_store = JSONStorage(str(tmp_path / "m6"))
    await sqlite_store.connect()
    await json_store.connect()
    try:
        await sqlite_store.save_batch(papers=papers)
        await json_store.save_batch(papers=papers)

        async def ids(store: SQLiteStorage | JSONStorage, author: str) -> list[str]:
            return [p.id for p in await store.query_papers(author=author)]

        for author in ("a_b", "acb", "100%"):
            assert await ids(sqlite_store, author) == await ids(json_store, author), (
                f"author={author!r} backend mismatch"
            )
        # ``a_b`` must not act as a wildcard that also matches ``acb``.
        assert await ids(sqlite_store, "a_b") == ["u1"]
        assert await ids(json_store, "a_b") == ["u1"]
    finally:
        await sqlite_store.close()
        await json_store.close()


# ---------------------------------------------------------------------------
# M7: collect entry points reject negative limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_m_m7_collector_rejects_negative_limit() -> None:
    """M7: ``MultiSourceCollector.collect`` validates the limit up front,
    matching ``query_papers``' ValueError contract."""
    collector = MultiSourceCollector(config={"sources": ["openalex"]})
    with pytest.raises(ValueError):
        await collector.collect("attention", limit=-1)


@pytest.mark.asyncio
async def test_fix_m_m7_api_collect_paper_rejects_negative_limit() -> None:
    """M7: the public ``collect_paper`` entry rejects a negative limit before
    any connection / source work happens."""
    ai = AcademicIntelligence()
    with pytest.raises(ValueError):
        await ai.collect_paper("attention", limit=-1)
    assert ai._connected is False  # validation happened before connect


def test_fix_m_m7_cli_negative_limit_exits_2(tmp_path: Path) -> None:
    """M7: ``ai collect paper --limit -5`` fails fast with exit code 2 and a
    limit error message (no source call)."""
    from academic_intelligence.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "collect",
            "paper",
            "attention is all you need",
            "--limit",
            "-5",
            "--storage-path",
            str(tmp_path / "m7.db"),
        ],
    )
    assert result.exit_code == 2
    assert "limit" in result.stdout.lower()
