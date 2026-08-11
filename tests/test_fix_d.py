"""FIX-D tests: I-9 fuzzy guards (D-1), 429 → RateLimitError semantics (D-2),
expand storage-first references read (D-5), incremental merge warnings (D-6).
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from academic_intelligence.core.exceptions import RateLimitError
from academic_intelligence.core.models import Evidence, Paper
from academic_intelligence.core.types import AntiCrawlStrategy, SourceType
from academic_intelligence.processors.incremental import IncrementalProcessor
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from academic_intelligence.utils.http import HTTPClient


def _ev(
    source: SourceType = SourceType.OPENALEX,
    conf: float = 0.8,
    sid: str | None = None,
) -> Evidence:
    return Evidence(
        source=source,
        source_url=f"https://{source.value}/record",
        confidence=conf,
        source_id=sid,
        collected_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# F2 (D-2): 429 retry exhaustion surfaces as RateLimitError
# ---------------------------------------------------------------------------


class _ResponseClient:
    """Minimal async client whose ``get`` returns a canned response."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._response


@pytest.mark.asyncio
async def test_openalex_429_after_retry_exhaustion_raises_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 with Retry-After survives the HTTP retry layer as RateLimitError.

    The HTTP client retries 429 and exhausts its budget, surfacing an
    ``HTTPStatusError``; the source must map it back to ``RateLimitError``
    (carrying the ``Retry-After`` seconds) so callers can ``except
    RateLimitError`` and back off.
    """

    async def _always_429(method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        return httpx.Response(429, headers={"Retry-After": "7"}, request=request)

    strategy = AntiCrawlStrategy(
        max_retries=2, base_delay=0.0, adaptive_delay=False, jitter=False, retry_backoff=1.0
    )
    client = HTTPClient(strategy=strategy, enable_cache=False, timeout=5.0)
    await client.connect()
    try:
        assert client._client is not None
        monkeypatch.setattr(client._client, "request", _always_429)
        monkeypatch.setattr(client, "_apply_rate_limit", AsyncMock())

        src = OpenAlexSource(http_client=client)
        with pytest.raises(RateLimitError) as excinfo:
            await src._get_json("/works/W123")
        err = excinfo.value
        assert err.source_name == "openalex"
        assert err.retry_after == 7
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_openalex_direct_429_response_fills_retry_after() -> None:
    """The response-level 429 branch carries ``retry_after`` (D-2).

    When the HTTP client returns the 429 response untouched (e.g. a custom
    strategy without 429 in ``retry_on_status``), the source's own branch must
    still expose ``retry_after`` from the ``Retry-After`` header.
    """
    request = httpx.Request("GET", "https://api.openalex.org/works/W123")
    response = httpx.Response(429, headers={"Retry-After": "4"}, request=request)
    src = OpenAlexSource(http_client=_ResponseClient(response))
    with pytest.raises(RateLimitError) as excinfo:
        await src._get_json("/works/W123")
    assert excinfo.value.retry_after == 4
    assert excinfo.value.source_name == "openalex"


# ---------------------------------------------------------------------------
# F3 (D-5): expand storage-first reads the papers.references JSON column
# ---------------------------------------------------------------------------


class _NoFetchCollector:
    """Collector stub that fails loudly if a fetch is ever attempted."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def collect_citations(
        self,
        paper_id: str,
        *,
        sources: list[Any] | None = None,
    ) -> None:
        self.calls.append("collect_citations")
        raise AssertionError("storage-first path must not fetch citations")

    async def collect_paper(
        self,
        query: str,
        *,
        sources: list[Any] | None = None,
        limit: int = 10,
    ) -> None:
        self.calls.append("collect_paper")
        raise AssertionError("storage-first path must not fetch papers")


@pytest.mark.asyncio
async def test_get_references_falls_back_to_references_column(
    tmp_path: Any,
) -> None:
    """``get_references`` reads ``papers.references`` when the citations edge
    table is empty (FIX-B1 F2 persists reference ids there) (D-5)."""
    store = SQLiteStorage(str(tmp_path / "refs.db"))
    await store.connect()
    try:
        paper = Paper(
            id="W2919115771",
            title="Center",
            authors=["Geoffrey Hinton"],
            year=1986,
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
async def test_expand_references_storage_first_no_fetch(
    tmp_path: Any,
) -> None:
    """Expand of references hits storage (references column) without fetching.

    A paper stored with a non-empty ``references`` list but no citation edges
    must expand purely from storage: ``fetched_new == 0`` and the collector is
    never consulted (D-5).
    """
    from academic_intelligence.graph import KnowledgeGraph
    from academic_intelligence.graph.traversal import expand_from_graph

    store = SQLiteStorage(str(tmp_path / "refs-expand.db"))
    await store.connect()
    try:
        paper = Paper(
            id="W2919115771",
            title="Center",
            references=["W2257979135", "W2000000000"],
            evidence_list=[_ev()],
        )
        await store.save_paper(paper)

        graph = KnowledgeGraph(cache_size=100)
        graph.add_node("W2919115771", type="paper", loaded=True, title="Center")
        collector = _NoFetchCollector()
        result = await expand_from_graph(
            graph,
            store,
            collector,
            "W2919115771",
            relations=["references"],
            depth=1,
            fetch_missing=False,
        )
        assert result.stats.fetched_new == 0
        assert result.stats.nodes_found == 2
        assert result.stats.failed == 0
        assert collector.calls == []
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F4 (D-6): incremental merges surface field-conflict warnings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_merge_surfaces_year_conflict_warnings() -> None:
    """A year conflict during an incremental confidence merge is exposed on
    ``IncrementalUpdateResult.warnings`` (D-6)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            proc = IncrementalProcessor(store)
            old = Paper(
                id="p1",
                title="Conflict Paper",
                year=2020,
                venue="NeurIPS",
                evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.8, "s2-1")],
            )
            await store.save_paper(old)

            new = Paper(
                id="p1",
                title="Conflict Paper",
                year=2015,
                venue="NeurIPS",
                evidence_list=[_ev(SourceType.OPENALEX, 0.95, "oa-1")],
            )
            result = await proc.detect_changes([new], [old])
            assert len(result.updated) == 1

            await proc.apply_changes(result)
            assert any("year conflict" in w for w in result.warnings)
            assert any("openalex=2015" in w and "semantic_scholar=2020" in w for w in result.warnings)
        finally:
            await store.close()
