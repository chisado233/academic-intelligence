"""FIX-E tests: JSON storage-first column fallback (E-1), edge+column union
semantics (E-2), subgraph ``loaded`` flag preservation (E-3), arxiv
``Retry-After`` header handling (E-4), traversal backfill node refresh (E-6).

The coverage acceptance test rework (E-7) lives in
``tests/test_zz_acceptance_coverage.py`` so it runs last in a full suite and
can self-compute the current run's coverage instead of reading a stale
``coverage.xml`` artifact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_intelligence.core.exceptions import RateLimitError
from academic_intelligence.core.models import Citation, CollectionResult, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.graph import KnowledgeGraph
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(
    source: SourceType = SourceType.OPENALEX,
    conf: float = 0.9,
) -> Evidence:
    return Evidence(
        source=source,
        source_url="https://example.com/record",
        confidence=conf,
        collected_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# E-1 + E-2: get_references / get_citations — column fallback + union
# ---------------------------------------------------------------------------


def _make_store(store_cls: type, tmp_path: Any):
    if store_cls is JSONStorage:
        store = JSONStorage(str(tmp_path / "data"))
    else:
        store = SQLiteStorage(str(tmp_path / "union.db"))
    return store


@pytest.mark.asyncio
async def test_json_get_references_falls_back_to_references_column(
    tmp_path: Any,
) -> None:
    """E-1: JSON backend reads ``papers.references`` when the citations edge
    table is empty, matching the sqlite backend (FIX-B1 F2 persists reference
    ids there)."""
    store = JSONStorage(str(tmp_path / "data"))
    await store.connect()
    try:
        paper = Paper(
            id="W2919115771",
            title="Center",
            references=["W2257979135", "W2000000000"],
            evidence_list=[_ev()],
        )
        await store.save_paper(paper)
        assert await store.get_references("W2919115771") == [
            "W2257979135",
            "W2000000000",
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_get_citations_falls_back_to_citations_list_column(
    tmp_path: Any,
) -> None:
    """E-1: JSON backend reads ``papers.citations_list`` when the citations
    edge table is empty (mirrors the references-column fallback)."""
    store = JSONStorage(str(tmp_path / "data"))
    await store.connect()
    try:
        paper = Paper(
            id="W2919115771",
            title="Center",
            citations_list=["W3000000000", "W3000000001"],
            evidence_list=[_ev()],
        )
        await store.save_paper(paper)
        assert await store.get_citations("W2919115771") == [
            "W3000000000",
            "W3000000001",
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_cls", [JSONStorage, SQLiteStorage], ids=["json", "sqlite"])
async def test_get_references_unions_edge_and_column(
    store_cls: type, tmp_path: Any
) -> None:
    """E-2: a partial edge-table hit must not shadow the full column.

    The V3.2 case: the column holds 39 references, the edge table records
    only one of them — the result must be the 39-id union, not the single
    edge hit."""
    store = _make_store(store_cls, tmp_path)
    await store.connect()
    try:
        refs = [f"W20000000{i:03d}" for i in range(1, 40)]
        paper = Paper(
            id="W2919115771",
            title="Center",
            references=refs,
            evidence_list=[_ev()],
        )
        await store.save_paper(paper)
        # Edge table hit for exactly one of the 39 column entries.
        await store.save_citation(
            Citation(
                citing_paper_id="W2919115771",
                cited_paper_id=refs[0],
                evidence=_ev(),
            )
        )
        got = await store.get_references("W2919115771")
        assert len(got) == 39
        assert set(got) == set(refs)
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_cls", [JSONStorage, SQLiteStorage], ids=["json", "sqlite"])
async def test_get_references_dedups_edge_and_column_overlap(
    store_cls: type, tmp_path: Any
) -> None:
    """E-2: ids present in both the edge table and the column are returned
    once."""
    store = _make_store(store_cls, tmp_path)
    await store.connect()
    try:
        paper = Paper(
            id="W2919115771",
            title="Center",
            references=["W2000000001", "W2000000002", "W2000000003"],
            evidence_list=[_ev()],
        )
        await store.save_paper(paper)
        await store.save_citation(
            Citation(
                citing_paper_id="W2919115771",
                cited_paper_id="W2000000002",
                evidence=_ev(),
            )
        )
        got = await store.get_references("W2919115771")
        assert len(got) == 3
        assert set(got) == {"W2000000001", "W2000000002", "W2000000003"}
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_cls", [JSONStorage, SQLiteStorage], ids=["json", "sqlite"])
async def test_get_citations_unions_edge_and_column(
    store_cls: type, tmp_path: Any
) -> None:
    """E-2: incoming edges unioned with the ``citations_list`` column."""
    store = _make_store(store_cls, tmp_path)
    await store.connect()
    try:
        paper = Paper(
            id="W2919115771",
            title="Center",
            citations_list=["W3000000001", "W3000000002"],
            evidence_list=[_ev()],
        )
        await store.save_paper(paper)
        await store.save_citation(
            Citation(
                citing_paper_id="W3000000003",
                cited_paper_id="W2919115771",
                evidence=_ev(),
            )
        )
        got = await store.get_citations("W2919115771")
        assert len(got) == 3
        assert set(got) == {"W3000000001", "W3000000002", "W3000000003"}
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# E-3: subgraph keeps the loaded flag
# ---------------------------------------------------------------------------


def test_to_subgraph_preserves_loaded_flag() -> None:
    """E-3: placeholder nodes (loaded=False) stay distinguishable after
    ``to_subgraph`` instead of being re-marked loaded=True by the default."""
    g = KnowledgeGraph(cache_size=100)
    g.add_node("p1", type="paper", loaded=True, title="Full Record")
    g.add_node("p2", type="paper", loaded=False)
    g.add_edge("p1", "p2", relation="cites")

    sub = g.to_subgraph("p1", radius=1)
    by_id = {n["id"]: n for n in sub.nodes()}
    assert by_id["p1"]["loaded"] is True
    assert by_id["p1"]["title"] == "Full Record"
    assert by_id["p2"]["loaded"] is False


# ---------------------------------------------------------------------------
# E-4: arxiv 429 reads the Retry-After header
# ---------------------------------------------------------------------------


def _mock_arxiv_response(
    *,
    status_code: int = 200,
    text: str = "",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    return resp


@pytest.mark.asyncio
async def test_arxiv_get_paper_by_arxiv_id_429_reads_retry_after() -> None:
    """E-4: the by-id request path surfaces ``retry_after`` from the
    ``Retry-After`` header (previously hard-coded 3), matching ``_query``."""
    http = MagicMock()
    http.get = AsyncMock(
        return_value=_mock_arxiv_response(
            status_code=429,
            text="slow down",
            headers={"Retry-After": "7"},
        )
    )
    source = ArxivSource(http_client=http, min_interval_seconds=0.01)
    with pytest.raises(RateLimitError) as excinfo:
        await source.get_paper_by_arxiv_id("1706.03762")
    assert excinfo.value.retry_after == 7
    assert excinfo.value.source_name == "arxiv"


# ---------------------------------------------------------------------------
# E-6: traversal backfill refreshes the session-graph placeholder node
# ---------------------------------------------------------------------------


class _FakeBackfillCollector:
    """Fake collector returning a single full paper record."""

    def __init__(self, paper: Paper) -> None:
        self._paper = paper
        self.calls = 0

    async def collect_paper(
        self,
        query: str,
        *,
        sources: list[Any] | None = None,
        limit: int = 10,
    ) -> CollectionResult:
        self.calls += 1
        return CollectionResult(papers=[self._paper])


@pytest.mark.asyncio
async def test_fetch_paper_record_refreshes_session_graph_placeholder(
    tmp_path: Any,
) -> None:
    """E-6: after a W-id placeholder is backfilled, the session-graph node is
    refreshed to loaded=True and carries title/year instead of staying a
    loaded=False stub."""
    from academic_intelligence.graph.traversal import _fetch_paper_record

    store = SQLiteStorage(str(tmp_path / "backfill.db"))
    await store.connect()
    try:
        paper = Paper(
            id="W2000000123",
            title="Backfilled Record",
            year=1986,
            evidence_list=[_ev()],
        )
        collector = _FakeBackfillCollector(paper)
        graph = KnowledgeGraph(cache_size=100)
        graph.add_node("W2000000123", type="paper", loaded=False)  # placeholder

        result = await _fetch_paper_record(
            store, collector, "W2000000123", sources=None, graph=graph
        )
        assert result is not None
        node = graph.get_node("W2000000123")
        assert node is not None
        assert node["loaded"] is True
        assert node["title"] == "Backfilled Record"
        assert node["year"] == 1986
    finally:
        await store.close()
