"""Unit tests for the OpenCitations (COCI) source adapter (mocked HTTP).

Covers the two citation directions, invalid/empty inputs, empty results,
edge-quality filtering (missing citing DOIs, self-citations) and the
domain error mapping — all offline via a mocked HTTP client.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from academic_intelligence.core.exceptions import (
    NotSupportedError,
    ParseError,
    RateLimitError,
    SourceUnavailableError,
    TimeoutError,
)
from academic_intelligence.core.models import Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.sources.opencitations import OpenCitationsSource

CITING_DOI = "10.1038/s41586-025-09422-z"

# Real COCI ``/citations`` shape: the second edge carries an empty citing
# DOI (incomplete record) and must be skipped.
SAMPLE_CITING_EDGES = [
    {
        "oci": "06014867264-06012514696",
        "citing": "10.1109/iros60139.2025.11246595",
        "cited": CITING_DOI,
        "creation": "2025-10-19",
        "timespan": "P0Y1M2D",
        "journal_sc": "no",
        "author_sc": "no",
    },
    {
        "oci": "06023113877-06012514696",
        "citing": "",
        "cited": CITING_DOI,
        "creation": "2025",
        "timespan": "P0Y",
        "journal_sc": "no",
        "author_sc": "no",
    },
    {
        "oci": "06010259181-06012514696",
        "citing": "10.1038/d41586-025-02703-7",
        "cited": CITING_DOI,
        "creation": "2025-09-17",
        "timespan": "P0Y0M0D",
        "journal_sc": "yes",
        "author_sc": "no",
    },
]

SAMPLE_CITED_EDGES = [
    {
        "oci": "06012514696-06604008939",
        "citing": CITING_DOI,
        "cited": "10.48550/arxiv.1707.06347",
        "creation": "2025-09-17",
        "timespan": "",
        "journal_sc": "no",
        "author_sc": "no",
    },
    {
        "oci": "06012514696-0609739742",
        "citing": CITING_DOI,
        "cited": "10.5281/zenodo.15753192",
        "creation": "2025-09-17",
        "timespan": "",
        "journal_sc": "no",
        "author_sc": "no",
    },
]

# Edge whose citing DOI equals its cited DOI (rejected by the Citation model).
SELF_CITATION_EDGE = {
    "oci": "06012514696-06012514696",
    "citing": CITING_DOI,
    "cited": CITING_DOI,
    "creation": "2025",
    "timespan": "P0Y",
    "journal_sc": "no",
    "author_sc": "no",
}


def _mock_response(
    *,
    status_code: int = 200,
    text: str = "",
    json_data: Any = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)
    else:
        resp.json = MagicMock(side_effect=ValueError("no json"))
    return resp


def _source_with_response(response: MagicMock) -> tuple[OpenCitationsSource, MagicMock]:
    http = MagicMock()
    http.get = AsyncMock(return_value=response)
    return OpenCitationsSource(http_client=http), http


# ---------------------------------------------------------------------------
# Instantiation / capabilities
# ---------------------------------------------------------------------------


def test_instantiation_and_capabilities() -> None:
    source = OpenCitationsSource(http_client=MagicMock())
    assert source.name == "opencitations"
    assert source.source_type is SourceType.OPEN_CITATIONS
    assert source.confidence == 0.85
    # C1 short keys: only citations is True.
    assert source.capabilities["citations"] is True
    assert source.capabilities["search"] is False
    assert source.capabilities["get"] is False
    assert source.capabilities["fulltext"] is False
    # author-class operations are unsupported.
    assert source.capabilities["get_author_papers"] is False
    assert source.capabilities["get_author_profile"] is False
    # pre-C1 long-form keys mirror the same contract.
    assert source.capabilities["get_citations"] is True
    assert source.capabilities["search_papers"] is False
    assert source.capabilities["get_paper_by_doi"] is False


def test_supports() -> None:
    source = OpenCitationsSource(http_client=MagicMock())
    assert source.supports("citations") is True
    assert source.supports("get_citations") is True
    assert source.supports("search") is False
    assert source.supports("get") is False
    assert source.supports("fulltext") is False
    assert source.supports("search_papers") is False
    assert source.supports("get_paper_by_doi") is False
    assert source.supports("get_author_papers") is False
    assert source.supports("get_author_profile") is False


# ---------------------------------------------------------------------------
# get_citations — directions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_citations_citing_direction() -> None:
    source, http = _source_with_response(_mock_response(json_data=SAMPLE_CITING_EDGES))

    citations = await source.get_citations(CITING_DOI)

    # Empty-citing edge is skipped: 3 raw edges -> 2 citations.
    assert len(citations) == 2
    assert citations[0].citing_paper_id == "10.1109/iros60139.2025.11246595"
    assert citations[0].cited_paper_id == CITING_DOI
    assert citations[1].citing_paper_id == "10.1038/d41586-025-02703-7"
    assert citations[1].cited_paper_id == CITING_DOI

    # /citations/ endpoint was used.
    url = http.get.await_args.args[0]
    assert "opencitations.net/index/coci/api/v1/citations/10.1038" in url

    evidence = citations[0].evidence
    assert evidence.source is SourceType.OPEN_CITATIONS
    assert evidence.source_id == CITING_DOI
    assert evidence.confidence == 0.85
    assert "opencitations.net/index/coci/api/v1/citations/" in evidence.source_url
    assert evidence.raw_data == SAMPLE_CITING_EDGES[0]


@pytest.mark.asyncio
async def test_get_citations_cited_direction() -> None:
    source, http = _source_with_response(_mock_response(json_data=SAMPLE_CITED_EDGES))

    citations = await source.get_citations(CITING_DOI, direction="cited")

    assert len(citations) == 2
    assert citations[0].citing_paper_id == CITING_DOI
    assert citations[0].cited_paper_id == "10.48550/arxiv.1707.06347"
    assert citations[1].cited_paper_id == "10.5281/zenodo.15753192"

    # /references/ endpoint was used.
    url = http.get.await_args.args[0]
    assert "opencitations.net/index/coci/api/v1/references/10.1038" in url


@pytest.mark.asyncio
async def test_get_citations_accepts_paper_record() -> None:
    source, _ = _source_with_response(_mock_response(json_data=SAMPLE_CITING_EDGES))
    paper = Paper(title="Some paper", doi=CITING_DOI)

    citations = await source.get_citations(paper)

    assert len(citations) == 2
    assert all(c.cited_paper_id == CITING_DOI for c in citations)


@pytest.mark.asyncio
async def test_get_citations_strips_doi_prefix() -> None:
    source, http = _source_with_response(_mock_response(json_data=SAMPLE_CITING_EDGES))

    citations = await source.get_citations(f"https://doi.org/{CITING_DOI}")

    assert len(citations) == 2
    url = http.get.await_args.args[0]
    assert CITING_DOI.replace("/", "%2F") in url


# ---------------------------------------------------------------------------
# get_citations — invalid input / empty results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_citations_invalid_doi_format() -> None:
    source = OpenCitationsSource(http_client=MagicMock())
    with pytest.raises(ValueError, match="invalid DOI format"):
        await source.get_citations("not-a-doi")
    # No request should have been made.
    source._http.get.assert_not_called()


@pytest.mark.asyncio
async def test_get_citations_paper_without_doi() -> None:
    source = OpenCitationsSource(http_client=MagicMock())
    with pytest.raises(ValueError, match="no DOI"):
        await source.get_citations(Paper(title="No DOI here"))


@pytest.mark.asyncio
async def test_get_citations_invalid_direction() -> None:
    source = OpenCitationsSource(http_client=MagicMock())
    with pytest.raises(ValueError, match="direction must be 'citing' or 'cited'"):
        await source.get_citations(CITING_DOI, direction="sideways")


@pytest.mark.asyncio
async def test_get_citations_empty_result() -> None:
    source, _ = _source_with_response(_mock_response(json_data=[]))
    assert await source.get_citations(CITING_DOI) == []


@pytest.mark.asyncio
async def test_get_citations_http_404_returns_empty() -> None:
    source, _ = _source_with_response(_mock_response(status_code=404, text="resource not found"))
    assert await source.get_citations(CITING_DOI) == []


@pytest.mark.asyncio
async def test_get_citations_skips_self_citation() -> None:
    source, _ = _source_with_response(
        _mock_response(json_data=[SELF_CITATION_EDGE, SAMPLE_CITING_EDGES[0]])
    )
    citations = await source.get_citations(CITING_DOI)
    assert len(citations) == 1
    assert citations[0].citing_paper_id == "10.1109/iros60139.2025.11246595"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_citations_rate_limit() -> None:
    source, _ = _source_with_response(
        _mock_response(status_code=429, text="slow down", headers={"Retry-After": "3"})
    )
    with pytest.raises(RateLimitError):
        await source.get_citations(CITING_DOI)


@pytest.mark.asyncio
async def test_get_citations_http_error() -> None:
    source, _ = _source_with_response(_mock_response(status_code=500, text="internal error"))
    with pytest.raises(SourceUnavailableError, match="HTTP 500"):
        await source.get_citations(CITING_DOI)


@pytest.mark.asyncio
async def test_get_citations_transport_error() -> None:
    http = MagicMock()
    http.get = AsyncMock(side_effect=RuntimeError("connection refused"))
    source = OpenCitationsSource(http_client=http)
    with pytest.raises(SourceUnavailableError, match="request failed"):
        await source.get_citations(CITING_DOI)


@pytest.mark.asyncio
async def test_get_citations_timeout() -> None:
    http = MagicMock()
    http.get = AsyncMock(side_effect=httpx.TimeoutException("slow response"))
    source = OpenCitationsSource(http_client=http)
    with pytest.raises(TimeoutError):
        await source.get_citations(CITING_DOI)


@pytest.mark.asyncio
async def test_get_citations_invalid_json() -> None:
    source, _ = _source_with_response(_mock_response(text="<html>not json</html>"))
    with pytest.raises(ParseError, match="Invalid JSON"):
        await source.get_citations(CITING_DOI)


@pytest.mark.asyncio
async def test_get_citations_non_array_payload() -> None:
    source, _ = _source_with_response(_mock_response(json_data={"error": "boom"}))
    with pytest.raises(ParseError, match="expected a JSON edge array"):
        await source.get_citations(CITING_DOI)


# ---------------------------------------------------------------------------
# Unsupported operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_operations_raise_not_supported() -> None:
    source = OpenCitationsSource(http_client=MagicMock())
    with pytest.raises(NotSupportedError, match="no metadata search"):
        await source.search_papers("transformer")
    with pytest.raises(NotSupportedError, match="no metadata records"):
        await source.get_paper_by_doi(CITING_DOI)
    with pytest.raises(NotSupportedError, match="no author endpoints"):
        await source.get_author_papers("Ada Lovelace")
    with pytest.raises(NotSupportedError, match="no author endpoints"):
        await source.get_author_profile("Ada Lovelace")
