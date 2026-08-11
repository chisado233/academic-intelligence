"""Regression tests for the black-box evaluation follow-up (FIX-AG)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.exceptions import RateLimitError, SourceFailure
from academic_intelligence.core.models import Evidence, Paper
from academic_intelligence.core.types import AntiCrawlStrategy, SourceType
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.utils.http import HTTPClient


def _paper(arxiv_id: str, title: str) -> Paper:
    return Paper(
        id=arxiv_id,
        title=title,
        arxiv_id=arxiv_id,
        year=2024,
        evidence_list=[
            Evidence(
                source=SourceType.ARXIV,
                source_id=arxiv_id,
                source_url=f"https://arxiv.org/abs/{arxiv_id}",
                collected_at=datetime.now(UTC),
                confidence=0.95,
            )
        ],
    )


class _RoutingArxiv(ArxivSource):
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        self.calls.append(("search_papers", query, limit))
        return [
            _paper("1810.04805v2", "Exact target"),
            _paper("9999.00001v1", "Unrelated mention"),
        ][:limit]

    async def get_paper_by_arxiv_id(self, arxiv_id: str) -> Paper | None:
        self.calls.append(("get_paper_by_arxiv_id", arxiv_id))
        return _paper(arxiv_id, "Exact target")


class _StaticHTTP:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        **_: Any,
    ) -> httpx.Response:
        self.calls.append({"url": url, "params": params})
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, text=self.text, request=request)


def _feed(*arxiv_ids: str) -> str:
    entries = []
    for arxiv_id in arxiv_ids:
        entries.append(
            f"""
  <entry>
    <id>http://arxiv.org/abs/{arxiv_id}</id>
    <title>Paper {arxiv_id}</title>
    <summary>Deterministic fixture.</summary>
    <published>2024-01-01T00:00:00Z</published>
    <author><name>Test Author</name></author>
    <link href="http://arxiv.org/abs/{arxiv_id}" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/{arxiv_id}" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.LG"/>
    <category term="cs.LG"/>
  </entry>"""
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:arxiv="http://arxiv.org/schemas/atom">'
        + "".join(entries)
        + "\n</feed>"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("1810.04805", "1810.04805"),
        ("1810.04805v2", "1810.04805v2"),
        ("arXiv:1810.04805", "1810.04805"),
        ("https://arxiv.org/abs/1810.04805v2", "1810.04805v2"),
        ("hep-th/9901001v2", "hep-th/9901001v2"),
    ],
)
async def test_collect_paper_routes_complete_arxiv_ids_to_exact_lookup(
    query: str,
    expected: str,
) -> None:
    source = _RoutingArxiv()

    result = await MultiSourceCollector(sources=[source]).collect_paper(
        query,
        sources=[source],
    )

    assert source.calls == [("get_paper_by_arxiv_id", expected)]
    assert [paper.title for paper in result.papers] == ["Exact target"]


@pytest.mark.asyncio
async def test_collect_paper_keeps_natural_language_containing_id_on_search_path() -> None:
    source = _RoutingArxiv()

    result = await MultiSourceCollector(sources=[source]).collect_paper(
        "paper 1810.04805",
        sources=[source],
    )

    assert source.calls == [("search_papers", "paper 1810.04805", 10)]
    assert len(result.papers) == 2


@pytest.mark.asyncio
async def test_arxiv_exact_lookup_ignores_unrelated_first_entry() -> None:
    source = ArxivSource(
        http_client=_StaticHTTP(_feed("9999.00001v1", "1810.04805v2"))  # type: ignore[arg-type]
    )

    paper = await source.get_paper_by_arxiv_id("1810.04805")

    assert paper is not None
    assert paper.arxiv_id == "1810.04805v2"


@pytest.mark.asyncio
async def test_arxiv_exact_lookup_rejects_embedded_id_without_http_call() -> None:
    http = _StaticHTTP(_feed("1810.04805v2"))
    source = ArxivSource(http_client=http)  # type: ignore[arg-type]

    assert await source.get_paper_by_arxiv_id("paper 1810.04805") is None
    assert http.calls == []


def test_arxiv_parser_preserves_old_style_archive_prefix() -> None:
    source = ArxivSource(http_client=_StaticHTTP(""))  # type: ignore[arg-type]

    papers = source._parse_feed(_feed("hep-th/9901001v2"))

    assert papers[0].arxiv_id == "hep-th/9901001v2"


@pytest.mark.asyncio
async def test_source_failure_recovers_terminal_http_retry_metadata() -> None:
    calls = 0

    def _always_429(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "5"},
            request=request,
        )

    strategy = AntiCrawlStrategy(
        max_retries=2,
        base_delay=0.0,
        adaptive_delay=False,
        jitter=False,
        retry_backoff=1.0,
    )
    client = HTTPClient(strategy=strategy, enable_cache=False, timeout=5.0)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(_always_429))
    source = ArxivSource(http_client=client)

    try:
        with pytest.raises(RateLimitError) as excinfo:
            await source.get_paper_by_arxiv_id("1810.04805")
        failure = SourceFailure.from_exception(
            source="arxiv",
            operation="get_paper_by_arxiv_id",
            exc=excinfo.value,
        )
    finally:
        await client.close()

    assert calls == 3
    assert failure.retry_count == 2
    assert failure.http_status == 429
    assert excinfo.value.retry_after == 5


def test_source_failure_explicit_outer_context_precedes_cause_metadata() -> None:
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(429, request=request)
    inner = httpx.HTTPStatusError(
        "inner",
        request=request,
        response=response,
    )
    inner.retry_count = 4  # type: ignore[attr-defined]
    outer = RateLimitError(
        "outer",
        source_name="arxiv",
        context={"retry_count": 0, "http_status": 503},
    )
    outer.__cause__ = inner

    failure = SourceFailure.from_exception(
        source="arxiv",
        operation="get_paper_by_arxiv_id",
        exc=outer,
    )

    assert failure.retry_count == 0
    assert failure.http_status == 503


def test_skill_contract_documents_public_apis_and_bounded_retry_policy() -> None:
    skill = (Path(__file__).parents[1] / "SKILL.md").read_text(encoding="utf-8")

    required_contract_tokens = (
        "get_paper_by_arxiv_id",
        "AuthorDisambiguator.score_pair",
        "AuthorDisambiguator.cluster",
        "AuthorDisambiguator.disambiguate",
        "KnowledgeGraph.add_node",
        "KnowledgeGraph.add_edge",
        "KnowledgeGraph.load_snapshot",
        '"node_count"',
        '"edge_count"',
        "不得创建无界",
        "PARTIAL",
        "BLOCKED",
    )

    missing = [token for token in required_contract_tokens if token not in skill]
    assert missing == []
