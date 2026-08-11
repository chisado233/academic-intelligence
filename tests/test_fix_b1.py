"""FIX-B1 tests: adapter backfill bundle (F1-F4) + HTTP cache wiring (F5).

Covers:
- F1: OpenAlex byline ``authorships[].author.id`` -> ``AuthorRef.author_id``
      (I-2 root cause); persisted rows become queryable by the real A-id
      instead of the ``~Name`` pseudo key.
- F2: OpenAlex ``referenced_works`` -> ``Paper.references`` (I-4 root cause).
- F3: ``get_paper_by_id`` + ID routing in the collector / traversal
      (I-3 placeholder backfill).
- F4: ``get_citing_papers`` so citing works can be persisted as Paper records.
- F5: ``connect()`` wires ``Cache`` into the shared ``HTTPClient``.

All tests are offline (mock HTTP / fake sources).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.models import (
    Author,
    Citation,
    CollectionResult,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import AntiCrawlStrategy, Config, SourceType
from academic_intelligence.graph import KnowledgeGraph
from academic_intelligence.graph.traversal import expand_from_graph
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.http import HTTPClient


def _ev(
    source: SourceType = SourceType.OPENALEX, conf: float = 0.9
) -> Evidence:
    return Evidence(source=source, source_url="https://example.com", confidence=conf)


# ---------------------------------------------------------------------------
# F1: byline author ids (I-2 root cause)
# ---------------------------------------------------------------------------


def test_openalex_parse_paper_fills_author_ids() -> None:
    """Byline entries carry the normalized OpenAlex author id."""
    src = OpenAlexSource(http_client=MagicMock())
    data = {
        "id": "https://openalex.org/W123",
        "title": "Deep Learning",
        "authorships": [
            {
                "author": {
                    "id": "https://openalex.org/A5108093963",
                    "display_name": "Geoffrey E. Hinton",
                }
            },
            {"author": {"display_name": "No Id Author"}},
        ],
    }
    paper = src._parse_paper(data)
    assert paper.authors[0].author_id == "A5108093963"
    assert paper.authors[0].name == "Geoffrey E. Hinton"
    assert paper.authors[0].position == 1
    assert paper.authors[1].author_id is None


@pytest.mark.asyncio
async def test_author_id_persisted_and_queryable_by_real_id(
    tmp_path: Path,
) -> None:
    """F1: after persistence, ``get_author_papers(A-id)`` returns the paper.

    The authorship row must be keyed by the real A-id, never the ``~Name``
    pseudo key, so author-paper edges are no longer broken.
    """
    src = OpenAlexSource(http_client=MagicMock())
    paper = src._parse_paper(
        {
            "id": "https://openalex.org/W2919115771",
            "title": "Deep learning",
            "publication_year": 2015,
            "doi": "https://doi.org/10.1038/nature14539",
            "authorships": [
                {
                    "author": {
                        "id": "https://openalex.org/A5108093963",
                        "display_name": "Geoffrey E. Hinton",
                    }
                }
            ],
        }
    )
    store = SQLiteStorage(str(tmp_path / "f1.db"))
    await store.connect()
    try:
        await store.save_paper(paper)
        assert await store.get_author_papers("A5108093963") == ["W2919115771"]
        # No pseudo-key authorship row is created for the resolved author.
        assert await store.get_author_papers("~Geoffrey E. Hinton") == []
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F2: referenced_works (I-4 root cause)
# ---------------------------------------------------------------------------


def test_openalex_parse_paper_fills_references() -> None:
    """``referenced_works`` is normalized to bare W-ids in ``Paper.references``."""
    src = OpenAlexSource(http_client=MagicMock())
    data = {
        "id": "https://openalex.org/W123",
        "title": "T",
        "referenced_works": [
            "https://openalex.org/W2257979135",
            "https://openalex.org/W2000000000",
        ],
    }
    paper = src._parse_paper(data)
    assert paper.references == ["W2257979135", "W2000000000"]


def test_openalex_parse_paper_references_none_when_absent() -> None:
    """Absent ``referenced_works`` keeps ``references`` None (unknown)."""
    src = OpenAlexSource(http_client=MagicMock())
    paper = src._parse_paper({"id": "https://openalex.org/W123", "title": "T"})
    assert paper.references is None


# ---------------------------------------------------------------------------
# F3: get_paper_by_id + collector / traversal ID routing (I-3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openalex_get_paper_by_id_normalizes_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full URL input is normalized to ``/works/W...`` and parsed."""
    src = OpenAlexSource(http_client=MagicMock())
    calls: list[tuple[str, object]] = []

    async def fake_get_json(path: str, params: dict[str, Any] | None = None) -> Any:
        calls.append((path, params))
        assert path == "/works/W2257979135"
        return {
            "id": "https://openalex.org/W2257979135",
            "title": "Citing Paper",
            "referenced_works": ["https://openalex.org/W2919115771"],
            "authorships": [
                {"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}}
            ],
        }

    monkeypatch.setattr(src, "_get_json", fake_get_json)
    paper = await src.get_paper_by_id("https://openalex.org/W2257979135/")
    assert paper is not None
    assert paper.id == "W2257979135"
    assert paper.title == "Citing Paper"
    assert paper.references == ["W2919115771"]
    assert paper.authors[0].author_id == "A1"
    assert calls == [("/works/W2257979135", None)]


@pytest.mark.asyncio
async def test_openalex_get_paper_by_id_bare_id_and_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare ``W123`` input works; a 404 (None) returns None without raising."""
    src = OpenAlexSource(http_client=MagicMock())
    paths: list[str] = []

    async def fake_get_json(path: str, params: dict[str, Any] | None = None) -> Any:
        paths.append(path)
        if path == "/works/W2257979135":
            return {"id": "https://openalex.org/W2257979135", "title": "T"}
        return None

    monkeypatch.setattr(src, "_get_json", fake_get_json)
    paper = await src.get_paper_by_id("W2257979135")
    assert paper is not None and paper.id == "W2257979135"
    assert await src.get_paper_by_id("W9999999999") is None
    assert paths == ["/works/W2257979135", "/works/W9999999999"]


class _FakeByIDSource(BaseSource):
    """Minimal source exposing the ``get_paper_by_id`` capability."""

    name = "fake_by_id"
    source_type = SourceType.OPENALEX
    capabilities = {
        **BaseSource.capabilities,
        # C1 fail-closed dispatch: declare the by-id + citation ops it has.
        "get_paper_by_id": True,
        "get_citations": True,
    }

    def __init__(self, paper: Paper | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._paper = paper

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        self.calls.append(("search_papers", query))
        return []

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        self.calls.append(("get_paper_by_doi", doi))
        return self._paper

    async def get_paper_by_id(self, work_id: str) -> Paper | None:
        self.calls.append(("get_paper_by_id", work_id))
        return self._paper

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        self.calls.append(("get_author_papers", author_name))
        return []

    async def get_author_profile(self, author_name: str) -> Author | None:
        self.calls.append(("get_author_profile", author_name))
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        self.calls.append(("get_citations", paper_id))
        return []


class _FakeSearchOnlySource(BaseSource):
    """Minimal source WITHOUT the ``get_paper_by_id`` capability."""

    name = "fake_search_only"
    source_type = SourceType.SEMANTIC_SCHOLAR

    def __init__(self, paper: Paper | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._paper = paper

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        self.calls.append(("search_papers", query))
        return [self._paper] if self._paper is not None else []

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        self.calls.append(("get_paper_by_doi", doi))
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        self.calls.append(("get_author_papers", author_name))
        return []

    async def get_author_profile(self, author_name: str) -> Author | None:
        self.calls.append(("get_author_profile", author_name))
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        self.calls.append(("get_citations", paper_id))
        return []


@pytest.mark.asyncio
async def test_collect_paper_routes_work_id_to_get_paper_by_id() -> None:
    """F3: a W-id query hits ``get_paper_by_id``, never the search path."""
    paper = Paper(id="W2257979135", title="Citing Paper", evidence_list=[_ev()])
    source = _FakeByIDSource(paper=paper)
    collector = MultiSourceCollector(config=Config(), sources=[source])
    result = await collector.collect_paper("https://openalex.org/W2257979135")
    assert ("get_paper_by_id", "W2257979135") in source.calls
    assert ("search_papers", "https://openalex.org/W2257979135") not in source.calls
    assert [p.id for p in result.papers] == ["W2257979135"]


@pytest.mark.asyncio
async def test_collect_paper_work_id_search_only_source_keeps_original_path() -> None:
    """F3: sources without the capability still use the original search path."""
    paper = Paper(id="p1", title="Found by search", evidence_list=[_ev()])
    source = _FakeSearchOnlySource(paper=paper)
    collector = MultiSourceCollector(config=Config(), sources=[source])
    result = await collector.collect_paper("W2257979135")
    assert ("search_papers", "W2257979135") in source.calls
    assert [p.id for p in result.papers] == ["p1"]


@pytest.mark.asyncio
async def test_collect_paper_work_id_mixed_sources() -> None:
    """F3: capable sources use the ID path, the rest keep searching."""
    by_id = _FakeByIDSource(
        paper=Paper(id="W2257979135", title="By ID", evidence_list=[_ev()])
    )
    search = _FakeSearchOnlySource(
        paper=Paper(id="p1", title="By search", evidence_list=[_ev()])
    )
    collector = MultiSourceCollector(config=Config(), sources=[by_id, search])
    result = await collector.collect_paper("W2257979135")
    assert ("get_paper_by_id", "W2257979135") in by_id.calls
    assert ("search_papers", "W2257979135") in search.calls
    assert {p.id for p in result.papers} == {"W2257979135", "p1"}


class _MemStorage:
    """Minimal in-memory storage for the traversal backfill test."""

    def __init__(self) -> None:
        self.papers: dict[str, Paper] = {}

    async def get_references(self, paper_id: str) -> list[str]:
        return []

    async def get_citations(self, paper_id: str) -> list[str]:
        return []

    async def get_author_papers(self, author_id: str) -> list[str]:
        return []

    async def get_coauthors(self, author_id: str) -> list[str]:
        return []

    async def get_paper(self, paper_id: str) -> Paper | None:
        return self.papers.get(paper_id)

    async def get_author(self, author_id: str) -> Author | None:
        return None

    async def save_batch(
        self,
        *,
        authors: list[Author] | None = None,
        papers: list[Paper] | None = None,
        citations: list[Citation] | None = None,
    ) -> dict[str, list[str]]:
        for p in papers or []:
            if p.id:
                self.papers[p.id] = p
        return {}


class _BackfillCollector:
    """Fake collector returning a citing paper for the placeholder W-id."""

    def __init__(
        self, citing_paper: Paper | None, citations: list[Citation] | None
    ) -> None:
        self._citing_paper = citing_paper
        self._citations = citations or []
        self.calls: list[tuple[str, str]] = []

    async def collect_citations(
        self, paper_id: str, *, sources: list[Any] | None = None
    ) -> CollectionResult:
        self.calls.append(("collect_citations", paper_id))
        return CollectionResult(citations=self._citations)

    async def collect_paper(
        self,
        query: str,
        *,
        sources: list[Any] | None = None,
        limit: int = 10,
    ) -> CollectionResult:
        self.calls.append(("collect_paper", query))
        papers = [self._citing_paper] if self._citing_paper is not None else []
        return CollectionResult(papers=papers)


@pytest.mark.asyncio
async def test_expand_backfills_work_id_placeholder() -> None:
    """F3: a placeholder citing node is backfilled on a second expand.

    ``expand(citations)`` leaves the citing node unloaded; a later
    ``expand(references)`` on that W-id fetches the record through
    ``collect_paper`` (ID path) instead of failing.
    """
    center = Paper(id="W2919115771", title="Center", evidence_list=[_ev()])
    citing = Paper(
        id="W2257979135",
        title="Citing Paper",
        references=["W2919115771"],
        evidence_list=[_ev()],
    )
    citation = Citation(
        citing_paper_id="W2257979135",
        cited_paper_id="W2919115771",
        evidence=_ev(),
    )
    storage = _MemStorage()
    storage.papers["W2919115771"] = center
    collector = _BackfillCollector(citing_paper=citing, citations=[citation])
    graph = KnowledgeGraph(cache_size=100)
    graph.add_node("W2919115771", type="paper", loaded=True, title="Center")

    first = await expand_from_graph(
        graph,
        storage,
        collector,
        "W2919115771",
        relations=["citations"],
        depth=1,
        fetch_missing=True,
    )
    assert first.stats.fetched_new == 1
    citing_node = graph.get_node("W2257979135")
    assert citing_node is not None
    assert citing_node["loaded"] is False  # placeholder

    second = await expand_from_graph(
        graph,
        storage,
        collector,
        "W2257979135",
        relations=["references"],
        depth=1,
        fetch_missing=True,
    )
    assert ("collect_paper", "W2257979135") in collector.calls
    assert second.stats.fetched_new >= 1
    assert second.stats.failed == 0
    center_node = graph.get_node("W2919115771")
    assert center_node is not None
    assert center_node["loaded"] is True


# ---------------------------------------------------------------------------
# F4: get_citing_papers (I-3 enhancement)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openalex_get_citing_papers_parses_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Citing works are parsed into Paper records (references included)."""
    src = OpenAlexSource(http_client=MagicMock())
    params_calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get_json(path: str, params: dict[str, Any] | None = None) -> Any:
        params_calls.append((path, params))
        return {
            "results": [
                {
                    "id": "https://openalex.org/W2257979135",
                    "title": "Citing A",
                    "referenced_works": ["https://openalex.org/W2919115771"],
                },
                {"id": "https://openalex.org/W2000000000", "title": "Citing B"},
            ]
        }

    monkeypatch.setattr(src, "_get_json", fake_get_json)
    papers = await src.get_citing_papers("W2919115771")
    assert [p.id for p in papers] == ["W2257979135", "W2000000000"]
    assert papers[0].references == ["W2919115771"]
    assert params_calls == [("/works", {"filter": "cites:W2919115771", "per_page": 50})]


@pytest.mark.asyncio
async def test_openalex_get_citing_papers_empty() -> None:
    """A 404 / empty response yields no papers (no exception)."""
    src = OpenAlexSource(
        http_client=MagicMock()
    )
    src._get_json = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await src.get_citing_papers("W2919115771") == []


class _FakeCitationsSource(BaseSource):
    """Minimal source exposing ``get_citations`` + ``get_citing_papers``."""

    name = "fake_citations"
    source_type = SourceType.OPENALEX
    capabilities = {
        **BaseSource.capabilities,
        # C1 fail-closed dispatch: declare the citation ops it has.
        "citations": True,
        "get_citations": True,
        "get_citing_papers": True,
    }

    def __init__(
        self,
        citations: list[Citation] | None = None,
        citing_papers: list[Paper] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self._citations = citations or []
        self._citing_papers = citing_papers or []

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        self.calls.append(("search_papers", query))
        return []

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        self.calls.append(("get_paper_by_doi", doi))
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        self.calls.append(("get_author_papers", author_name))
        return []

    async def get_author_profile(self, author_name: str) -> Author | None:
        self.calls.append(("get_author_profile", author_name))
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        self.calls.append(("get_citations", paper_id))
        return self._citations

    async def get_citing_papers(self, paper_id: str) -> list[Paper]:
        self.calls.append(("get_citing_papers", paper_id))
        return self._citing_papers


@pytest.mark.asyncio
async def test_collect_citations_persists_citing_papers() -> None:
    """F4: ``collect_citations`` also collects citing works as Papers."""
    citation = Citation(
        citing_paper_id="W2257979135",
        cited_paper_id="W2919115771",
        evidence=_ev(),
    )
    citing = Paper(id="W2257979135", title="Citing", evidence_list=[_ev()])
    source = _FakeCitationsSource(citations=[citation], citing_papers=[citing])
    collector = MultiSourceCollector(config=Config(), sources=[source])
    result = await collector.collect_citations("W2919115771")
    assert ("get_citations", "W2919115771") in source.calls
    assert ("get_citing_papers", "W2919115771") in source.calls
    assert len(result.citations) == 1
    assert [p.id for p in result.papers] == ["W2257979135"]


@pytest.mark.asyncio
async def test_collect_citations_without_get_citing_papers_unchanged() -> None:
    """Sources without ``get_citing_papers`` keep the original behavior."""
    source = _FakeByIDSource()
    collector = MultiSourceCollector(config=Config(), sources=[source])
    result = await collector.collect_citations("W2919115771")
    assert ("get_citations", "W2919115771") in source.calls
    assert not any(name == "get_citing_papers" for name, _ in source.calls)
    assert len(result.citations) == 0
    assert len(result.papers) == 0


# ---------------------------------------------------------------------------
# F5: HTTP cache wiring (P17)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_client_get_served_from_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTPClient cache path: second identical GET is served from cache."""
    strategy = AntiCrawlStrategy(
        max_retries=0, base_delay=0.0, adaptive_delay=False, jitter=False
    )
    client = HTTPClient(strategy=strategy, cache=Cache(ttl=60), timeout=5.0)
    await client.connect()
    calls = {"n": 0}

    async def _req(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        request = httpx.Request(method, url)
        return httpx.Response(200, json={"ok": True}, request=request)

    assert client._client is not None
    monkeypatch.setattr(client._client, "request", _req)
    monkeypatch.setattr(client, "_apply_rate_limit", AsyncMock())

    r1 = await client.get("https://example.com/api")
    r2 = await client.get("https://example.com/api")
    assert calls["n"] == 1
    assert r1.json() == {"ok": True}
    assert r2.json() == {"ok": True}
    await client.close()


@pytest.mark.asyncio
async def test_http_client_cache_disabled_no_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``enable_cache=False`` every GET reaches the transport."""
    strategy = AntiCrawlStrategy(
        max_retries=0, base_delay=0.0, adaptive_delay=False, jitter=False
    )
    client = HTTPClient(strategy=strategy, enable_cache=False, timeout=5.0)
    await client.connect()
    calls = {"n": 0}

    async def _req(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    assert client._client is not None
    monkeypatch.setattr(client._client, "request", _req)
    monkeypatch.setattr(client, "_apply_rate_limit", AsyncMock())

    await client.get("https://example.com/api")
    await client.get("https://example.com/api")
    assert calls["n"] == 2
    assert client._cache is None
    await client.close()


@pytest.mark.asyncio
async def test_connect_wires_http_cache_from_config(tmp_path: Path) -> None:
    """F5: ``connect()`` passes ``Cache(ttl=config.cache_ttl)`` to HTTPClient.

    Regression for P17: ``cache_enabled`` was previously a no-op because the
    shared client was constructed without a ``Cache`` instance.
    """
    from academic_intelligence import AcademicIntelligence

    ai = AcademicIntelligence(
        config=Config(
            cache_enabled=True,
            storage_path=str(tmp_path / "cached.db"),
        )
    )
    await ai.connect()
    try:
        assert ai._http is not None
        assert ai._http._cache is not None
        assert ai._http._cache.ttl == Config().cache_ttl
    finally:
        await ai.close()

    ai2 = AcademicIntelligence(
        config=Config(
            cache_enabled=False,
            storage_path=str(tmp_path / "uncached.db"),
        )
    )
    await ai2.connect()
    try:
        assert ai2._http is not None
        assert ai2._http._cache is None
    finally:
        await ai2.close()
