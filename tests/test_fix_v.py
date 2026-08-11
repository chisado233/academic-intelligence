"""FIX-V regression tests: incremental correctness + citation idempotency
+ session-graph refresh (P40 round-22 defects V-A..V-E).

- F1 (V-A): citations persist is idempotent (unique pair index + upsert)
- F2 (V-C): read path lifts naive ``collected_at`` to UTC-aware
- F3 (V-B): fresh incremental records override stale re-scored stored ones
- F4 (V-D): same-session references expand refreshes the center node
- F5 (V-E): placeholder stubs upgrade once backfilled into storage
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from academic_intelligence.core.models import AuthorRef, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.graph import KnowledgeGraph
from academic_intelligence.graph.traversal import expand_from_graph
from academic_intelligence.processors.incremental import IncrementalProcessor
from academic_intelligence.processors.scorer import ConfidenceScorer
from academic_intelligence.storage.sqlite_store import (
    EvidenceRow,
    SQLiteStorage,
    _evidence_row_to_model,
)
from tests.test_graph_traversal import FakeCollector, FakeStorage


def _evidence() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W1",
        confidence=0.8,
    )


def _ev(
    source: SourceType = SourceType.OPENALEX,
    conf: float = 0.9,
    source_id: str | None = None,
) -> Evidence:
    return Evidence(
        source=source,
        source_url="https://example.com/p",
        confidence=conf,
        source_id=source_id,
    )


def _paper(
    pid: str | None,
    title: str,
    *,
    doi: str | None = None,
    year: int = 2020,
    venue: str | None = None,
    citations: int | None = None,
    evidence_list: list[Evidence] | None = None,
) -> Paper:
    return Paper(
        id=pid,
        title=title,
        authors=[AuthorRef(name="Jane Doe", position=1)],
        year=year,
        venue=venue,
        doi=doi,
        citations=citations,
        evidence_list=evidence_list if evidence_list is not None else [_ev()],
    )


def _paper_g(pid: str, title: str) -> Paper:
    """Graph-test paper: minimal record with an id + title."""
    return Paper(id=pid, title=title)


# ---------------------------------------------------------------------------
# F1 (V-A): citations idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_v_f1_save_citation_same_pair_is_idempotent(tmp_path) -> None:
    """V-A: saving the same (citing, cited) pair twice must yield one row."""
    store = SQLiteStorage(str(tmp_path / "v1a.db"))
    await store.connect()
    try:
        citation = Citation(
            citing_paper_id="p1", cited_paper_id="p2", evidence=_evidence()
        )
        await store.save_citation(citation)
        await store.save_citation(citation)
        stats = await store.get_stats()
        assert stats["total_citations"] == 1
        assert (
            len(await store.get_citations_by_paper("p1", direction="outgoing")) == 1
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_v_f1_save_batch_citations_do_not_grow(tmp_path) -> None:
    """V-A: persisting the same citation batch twice (including an intra-batch
    duplicate pair) must not inflate the citations table."""
    store = SQLiteStorage(str(tmp_path / "v1b.db"))
    await store.connect()
    try:
        citations = [
            Citation(citing_paper_id="p1", cited_paper_id="p2", evidence=_evidence()),
            Citation(citing_paper_id="p1", cited_paper_id="p3", evidence=_evidence()),
            Citation(citing_paper_id="p1", cited_paper_id="p2", evidence=_evidence()),
        ]
        ids1 = await store.save_batch(citations=citations)
        ids2 = await store.save_batch(citations=citations)
        assert ids1["citations"] and ids2["citations"]
        stats = await store.get_stats()
        assert stats["total_citations"] == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_v_f1_unique_index_created_and_legacy_rows_deduped(
    tmp_path,
) -> None:
    """V-A: connecting to a pre-existing database without the unique index
    deduplicates duplicate pairs and installs the index."""
    db = tmp_path / "v1_migrate.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE citations (id VARCHAR(64) PRIMARY KEY, "
        "citing_paper_id VARCHAR(64) NOT NULL, cited_paper_id VARCHAR(64) NOT NULL, "
        "evidence JSON NOT NULL)"
    )
    conn.execute("INSERT INTO citations VALUES ('a', 'p1', 'p2', '{}')")
    conn.execute("INSERT INTO citations VALUES ('b', 'p1', 'p2', '{}')")
    conn.execute("INSERT INTO citations VALUES ('c', 'p1', 'p3', '{}')")
    conn.commit()
    conn.close()

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


# ---------------------------------------------------------------------------
# F2 (V-C): read-path timezone unification
# ---------------------------------------------------------------------------


def test_fix_v_f2_evidence_row_to_model_lifts_naive_to_aware() -> None:
    """V-C: a naive ``collected_at`` read from the DB row is lifted to UTC."""
    row = EvidenceRow(
        entity_type="paper",
        entity_id="p1",
        source="openalex",
        source_url="https://example.com",
        collected_at=datetime(2026, 8, 8, 12, 0, 0),
        confidence=0.9,
    )
    evidence = _evidence_row_to_model(row)
    assert evidence.collected_at.tzinfo is not None
    assert evidence.collected_at == datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_fix_v_f2_mixed_naive_aware_merge_does_not_crash(tmp_path) -> None:
    """V-C: merging stored (naive) evidence with fresh (aware) evidence must
    not raise TypeError inside ConfidenceScorer (P40 V3.1a)."""
    store = SQLiteStorage(str(tmp_path / "v2.db"))
    await store.connect()
    try:
        paper = Paper(
            id="p1",
            title="Paper One",
            evidence_list=[
                _ev(SourceType.OPENALEX, conf=0.9),
                _ev(SourceType.SEMANTIC_SCHOLAR, conf=0.88),
            ],
        )
        await store.save_paper(paper)
        stored = await store.get_paper("p1")
        assert stored is not None
        # The read path must return timezone-aware evidence (V-C).
        assert all(ev.collected_at.tzinfo is not None for ev in stored.evidence_list)
        fresh = _ev(SourceType.ARXIV, conf=0.95)
        merged_list = IncrementalProcessor._merge_evidence_lists(
            stored, Paper(title="Paper One", evidence_list=[fresh])
        )
        merged = stored.model_copy(update={"evidence_list": merged_list})
        scored = ConfidenceScorer().score_paper(merged)
        assert scored.primary_evidence is not None
        assert scored.primary_evidence.confidence > 0.9
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F3 (V-B): incremental merge debias — fresh records win
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_v_f3_fresh_record_overrides_rescored_stored(tmp_path) -> None:
    """V-B: a freshly collected record must override a stored record whose
    confidence was lifted by the read-path re-scoring (DOI +0.05), so the
    update lands instead of stale values winning forever."""
    store = SQLiteStorage(str(tmp_path / "v3a.db"))
    await store.connect()
    try:
        proc = IncrementalProcessor(store)
        v1 = _paper("p1", "Paper One v1", doi="10.1000/p1", citations=1)
        await store.save_paper(v1)
        old = await store.get_paper("p1")
        assert old is not None and old.primary_evidence is not None
        assert old.primary_evidence.confidence == pytest.approx(0.95)  # re-scored
        v2 = _paper(
            "p1",
            "Paper One v2",
            doi="10.1000/p1",
            citations=99,
            evidence_list=[_ev(conf=0.9, source_id="10.1000/p1")],
        )
        result = await proc.detect_changes([v2], [old])
        assert len(result.updated) == 1
        counts = await proc.apply_changes(result)
        assert counts["updated"] == 1
        stored = await store.get_paper("p1")
        assert stored is not None
        assert stored.title == "Paper One v2"  # update must override stale value
        assert stored.citations == 99
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_v_f3_multiround_incremental_converges(tmp_path) -> None:
    """V-B: after the update lands, a second pass with the same fresh record
    reports no further update (convergence)."""
    store = SQLiteStorage(str(tmp_path / "v3b.db"))
    await store.connect()
    try:
        proc = IncrementalProcessor(store)
        v1 = _paper("p1", "Paper One v1", doi="10.1000/p1", citations=1)
        await store.save_paper(v1)
        v2 = _paper(
            "p1",
            "Paper One v2",
            doi="10.1000/p1",
            citations=99,
            evidence_list=[_ev(conf=0.9, source_id="10.1000/p1")],
        )
        old1 = await store.get_paper("p1")
        r1 = await proc.detect_changes([v2], [old1])
        assert len(r1.updated) == 1
        assert (await proc.apply_changes(r1))["updated"] == 1
        stored = await store.get_paper("p1")
        assert stored is not None
        assert stored.title == "Paper One v2"

        old2 = await store.get_paper("p1")
        r2 = await proc.detect_changes([v2], [old2])
        assert r2.updated == []
        assert "p1" in r2.unchanged
        assert (await proc.apply_changes(r2))["updated"] == 0
    finally:
        await store.close()


def test_fix_v_f3_significantly_lower_confidence_new_does_not_override() -> None:
    """V-B guard: a fresh record whose confidence is significantly lower than
    the stored record must NOT override it (old value kept)."""
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    old = _paper(
        "p1",
        "Paper One",
        venue="Old Venue",
        evidence_list=[_ev(SourceType.ARXIV, conf=0.95)],
    )
    new = _paper(
        "p1",
        "Paper One",
        venue="New Venue",
        evidence_list=[_ev(SourceType.GOOGLE_SCHOLAR, conf=0.75)],
    )
    merged = proc._merge_papers_confidence(old, new)
    assert merged.venue == "Old Venue"


# ---------------------------------------------------------------------------
# F4 (V-D): same-session center refresh on references expand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_v_f4_references_expand_refreshes_center_title() -> None:
    """V-D: after the stored record of the center changes, a same-session
    references expand must refresh the center node's title (P40 V3.3)."""
    storage = FakeStorage()
    storage.papers["p1"] = _paper_g("p1", "Center v1")
    storage.refs["p1"] = ["r1"]
    storage.papers["r1"] = _paper_g("r1", "Ref One")
    graph = KnowledgeGraph()
    collector = FakeCollector()

    await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"], fetch_missing=False
    )
    first = graph.get_node("p1")
    assert first is not None
    assert first["title"] == "Center v1"

    storage.papers["p1"] = _paper_g("p1", "Center v2")
    await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"], fetch_missing=False
    )
    refreshed = graph.get_node("p1")
    assert refreshed is not None
    assert refreshed["title"] == "Center v2"


# ---------------------------------------------------------------------------
# F5 (V-E): placeholder stub upgrade after storage backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_v_f5_stub_upgrades_after_storage_backfill() -> None:
    """V-E: a placeholder stub created in an earlier pass upgrades to a loaded
    node once the record is backfilled into storage (same session, P40 V4.2
    storage-only pass)."""
    storage = FakeStorage()
    storage.papers["p1"] = _paper_g("p1", "Alpha")
    storage.refs["p1"] = ["ghost"]
    graph = KnowledgeGraph()
    collector = FakeCollector()

    await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"], fetch_missing=False
    )
    stub = graph.get_node("ghost")
    assert stub is not None
    assert stub["loaded"] is False

    storage.papers["ghost"] = _paper_g("ghost", "Ghost Real")
    await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"], fetch_missing=False
    )
    upgraded = graph.get_node("ghost")
    assert upgraded is not None
    assert upgraded["loaded"] is True
    assert upgraded["title"] == "Ghost Real"
