"""Unit tests for the CORE (core.ac.uk) source adapter (mocked HTTP).

Covers the WP2e acceptance surface: search, get-by-DOI / get-by-id,
full-text link extraction, HTTP 401 with a missing/invalid key, and the
error mapping (rate limit, parse failure, unsupported operations).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_intelligence.core.exceptions import (
    AuthenticationError,
    NotSupportedError,
    ParseError,
    RateLimitError,
)
from academic_intelligence.core.models import Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.scorer import SOURCE_BASELINE_CONFIDENCE
from academic_intelligence.sources.core_ import CoreSource

SAMPLE_CORE_SEARCH: dict[str, Any] = {
    "totalHits": 2,
    "limit": 2,
    "offset": 0,
    "results": [
        {
            "id": "168955695",
            "title": (
                "Hierarchical Line Graph Neural Network: A Study on "
                "Alternative Representations of Graph-Structured Data"
            ),
            "authors": [{"name": "MOHAMMADI, SOLMAZ"}],
            "yearPublished": "2024",
            "publisher": "",
            "doi": "10.1234/example.2024.001",
            "abstract": (
                "This thesis addresses the challenge of feature-smoothing "
                "in deep graph neural networks (GNNs)."
            ),
            "downloadUrl": "https://core.ac.uk/download/620851279.pdf",
            "fullText": "Not available for public API users.",
            "sourceFulltextUrls": [
                "https://thesis.unipd.it/bitstream/20.500.12608/68875/1/thesis.pdf"
            ],
            "fieldOfStudy": "Graph Neural Network",
            "citationCount": 0,
            "links": [
                {"type": "download", "url": "https://core.ac.uk/download/620851279.pdf"},
                {"type": "reader", "url": "https://core.ac.uk/reader/620851279"},
                {"type": "display", "url": "https://core.ac.uk/works/168955695"},
            ],
        },
        {
            "id": "999999",
            "title": "Graph Neural Networks: A Review of Methods and Applications",
            "authors": ["Jane Doe", {"name": "John Smith"}],
            "yearPublished": 2020,
            "publisher": "IEEE",
            "doi": "10.1109/example.2020.123",
            "abstract": "A review of graph neural network methods.",
            "downloadUrl": "https://core.ac.uk/download/999999.pdf",
            "fullText": "",
            "fieldOfStudy": ["Graph Neural Network", "Deep Learning"],
            "citationCount": 42,
        },
    ],
}

SAMPLE_CORE_WORK: dict[str, Any] = SAMPLE_CORE_SEARCH["results"][0]


def _mock_response(
    *,
    status_code: int = 200,
    text: str = "",
    json_data: Any | None = None,
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


def _source_with(response: MagicMock) -> tuple[CoreSource, MagicMock]:
    http = MagicMock()
    http.get = AsyncMock(return_value=response)
    return CoreSource(http_client=http), http


def _paper_with_raw(raw: dict[str, Any]) -> Paper:
    """Build a minimal Paper carrying one CORE evidence with *raw* data."""
    evidence = Evidence(
        source=SourceType.CORE,
        source_url="https://api.core.ac.uk/v3/works/1",
        confidence=0.85,
        raw_data=raw,
    )
    return Paper(title="A CORE paper", evidence_list=[evidence])


# ---------------------------------------------------------------------------
# Instantiation / capabilities / scorer baseline
# ---------------------------------------------------------------------------


def test_core_source_capabilities() -> None:
    source = CoreSource()
    assert source.name == "core"
    assert source.source_type == SourceType.CORE
    assert source.confidence == 0.85
    assert source.capabilities["search_papers"] is True
    assert source.capabilities["get_paper_by_doi"] is True
    assert source.capabilities["get_citations"] is False
    assert source.capabilities["get_author_papers"] is False
    assert source.capabilities["get_author_profile"] is False
    assert source.capabilities["fulltext"] is True
    # C1 CLI operation keys (technical-design.md §1.1.1).
    assert source.supports("search") is True
    assert source.supports("get") is True
    assert source.supports("citations") is False
    assert source.supports("author") is False
    # Method-name keys used by the collector runtime.
    assert source.supports("search_papers") is True
    assert source.supports("get_paper_by_doi") is True
    assert source.supports("get_author_papers") is False
    # supports() is fail-closed on capability declarations: the method-name
    # key is not declared, so it is unsupported even though the method exists.
    assert source.supports("get_fulltext") is False


def test_core_scorer_baseline_registered() -> None:
    assert SOURCE_BASELINE_CONFIDENCE[SourceType.CORE] == 0.85


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_search_papers_parses_results() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_CORE_SEARCH))

    papers = await source.search_papers("graph neural network", limit=5)

    assert len(papers) == 2
    first = papers[0]
    assert first.id == "168955695"
    assert first.title.startswith("Hierarchical Line Graph")
    assert [a.name for a in first.authors] == ["MOHAMMADI, SOLMAZ"]
    assert first.year == 2024
    assert first.venue is None
    assert first.doi == "10.1234/example.2024.001"
    assert first.abstract and "feature-smoothing" in first.abstract
    assert first.pdf_url == "https://core.ac.uk/download/620851279.pdf"
    assert first.url == "https://core.ac.uk/works/168955695"
    assert first.citations == 0
    assert first.keywords == ["Graph Neural Network"]
    assert first.evidence_list[0].source == SourceType.CORE
    assert first.evidence_list[0].source_id == "168955695"
    assert first.evidence_list[0].source_url == "https://api.core.ac.uk/v3/works/168955695"

    second = papers[1]
    assert [a.name for a in second.authors] == ["Jane Doe", "John Smith"]
    assert second.year == 2020
    assert second.venue == "IEEE"
    assert second.citations == 42
    assert "Deep Learning" in second.keywords
    assert second.pdf_url == "https://core.ac.uk/download/999999.pdf"

    call = http.get.await_args
    assert call is not None
    assert call.kwargs["params"]["q"] == "graph neural network"
    assert call.kwargs["params"]["limit"] == 5
    assert call.args[0] == "https://api.core.ac.uk/v3/search/works"


@pytest.mark.asyncio
async def test_core_search_empty_query_returns_no_request() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_CORE_SEARCH))

    assert await source.search_papers("   ") == []
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_core_search_no_results_returns_empty() -> None:
    source, _ = _source_with(_mock_response(json_data={"totalHits": 0, "results": []}))

    assert await source.search_papers("nothing here") == []


@pytest.mark.asyncio
async def test_core_search_skips_malformed_records() -> None:
    payload = {"results": [{"id": "1", "authors": []}, {"not": "a paper"}]}
    source, _ = _source_with(_mock_response(json_data=payload))

    assert await source.search_papers("graph neural network") == []


# ---------------------------------------------------------------------------
# Get by DOI / by CORE id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_get_paper_by_doi_exact_match() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_CORE_SEARCH))

    paper = await source.get_paper_by_doi("https://doi.org/10.1234/example.2024.001")

    assert paper is not None
    assert paper.id == "168955695"
    assert paper.doi == "10.1234/example.2024.001"
    call = http.get.await_args
    assert call is not None
    assert call.kwargs["params"]["q"] == 'doi:"10.1234/example.2024.001"'


@pytest.mark.asyncio
async def test_core_get_paper_by_doi_invalid_returns_none() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_CORE_SEARCH))

    assert await source.get_paper_by_doi("not-a-doi") is None
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_core_get_paper_by_id_accepts_full_url() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_CORE_WORK))

    paper = await source.get_paper_by_id("https://core.ac.uk/works/168955695")

    assert paper is not None
    assert paper.id == "168955695"
    assert paper.title.startswith("Hierarchical Line Graph")
    call = http.get.await_args
    assert call is not None
    assert call.args[0] == "https://api.core.ac.uk/v3/works/168955695"


@pytest.mark.asyncio
async def test_core_get_paper_by_id_non_numeric_returns_none() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_CORE_WORK))

    assert await source.get_paper_by_id("W1234567") is None
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_core_get_paper_by_id_404_returns_none() -> None:
    source, _ = _source_with(_mock_response(status_code=404, text="not found"))

    assert await source.get_paper_by_id("168955695") is None


# ---------------------------------------------------------------------------
# Full-text link extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_get_fulltext_prefers_download_url() -> None:
    source = CoreSource()
    paper = _paper_with_raw(
        {
            "downloadUrl": "https://core.ac.uk/download/1.pdf",
            "sourceFulltextUrls": ["https://mirror.example.com/1.pdf"],
        }
    )
    assert await source.get_fulltext(paper) == "https://core.ac.uk/download/1.pdf"


@pytest.mark.asyncio
async def test_core_get_fulltext_falls_back_to_source_fulltext_urls() -> None:
    source = CoreSource()
    paper = _paper_with_raw(
        {
            "sourceFulltextUrls": [
                "https://mirror.example.com/a.pdf",
                "https://mirror.example.com/b.pdf",
            ]
        }
    )
    assert await source.get_fulltext(paper) == "https://mirror.example.com/a.pdf"


@pytest.mark.asyncio
async def test_core_get_fulltext_uses_url_shaped_full_text_field() -> None:
    source = CoreSource()
    paper = _paper_with_raw({"fullText": "https://example.com/full.pdf"})
    assert await source.get_fulltext(paper) == "https://example.com/full.pdf"


@pytest.mark.asyncio
async def test_core_get_fulltext_ignores_placeholder_full_text() -> None:
    source = CoreSource()
    # Real public-tier records carry a placeholder instead of a link; the
    # mirror URL must still be found.
    paper = _paper_with_raw(
        {
            "fullText": "Not available for public API users.",
            "sourceFulltextUrls": ["https://mirror.example.com/1.pdf"],
        }
    )
    assert await source.get_fulltext(paper) == "https://mirror.example.com/1.pdf"


@pytest.mark.asyncio
async def test_core_get_fulltext_none_when_no_oa_link() -> None:
    source = CoreSource()
    paper = _paper_with_raw({"fullText": "Not available for public API users."})
    assert await source.get_fulltext(paper) is None


@pytest.mark.asyncio
async def test_core_get_fulltext_never_returns_landing_page() -> None:
    source = CoreSource()
    paper = _paper_with_raw(
        {
            "links": [{"type": "display", "url": "https://core.ac.uk/works/1"}],
        }
    )
    assert await source.get_fulltext(paper) is None


@pytest.mark.asyncio
async def test_core_get_fulltext_reads_paper_pdf_url() -> None:
    source = CoreSource()
    paper = _paper_with_raw({})
    paper = paper.model_copy(update={"pdf_url": "https://core.ac.uk/download/9.pdf"})
    assert await source.get_fulltext(paper) == "https://core.ac.uk/download/9.pdf"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_http_401_raises_authentication_error() -> None:
    source, _ = _source_with(_mock_response(status_code=401, text="unauthorized"))

    with pytest.raises(AuthenticationError):
        await source.search_papers("graph neural network")


@pytest.mark.asyncio
async def test_core_keyless_request_has_no_auth_header() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_CORE_SEARCH))

    await source.search_papers("graph neural network", limit=1)

    call = http.get.await_args
    assert call is not None
    assert "Authorization" not in call.kwargs["headers"]


@pytest.mark.asyncio
async def test_core_api_key_sends_bearer_header() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=SAMPLE_CORE_SEARCH))
    source = CoreSource(http_client=http, api_key="secret-key")

    await source.search_papers("graph neural network", limit=1)

    call = http.get.await_args
    assert call is not None
    assert call.kwargs["headers"]["Authorization"] == "Bearer secret-key"


@pytest.mark.asyncio
async def test_core_api_key_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORE_API_KEY", "env-key")
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=SAMPLE_CORE_SEARCH))
    source = CoreSource(http_client=http)

    await source.search_papers("graph neural network", limit=1)

    call = http.get.await_args
    assert call is not None
    assert call.kwargs["headers"]["Authorization"] == "Bearer env-key"


# ---------------------------------------------------------------------------
# Error mapping / unsupported operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_rate_limit_429_raises() -> None:
    source, _ = _source_with(
        _mock_response(status_code=429, headers={"Retry-After": "5"}, text="slow down")
    )

    with pytest.raises(RateLimitError) as excinfo:
        await source.search_papers("graph neural network")
    assert excinfo.value.retry_after == 5


@pytest.mark.asyncio
async def test_core_invalid_json_raises_parse_error() -> None:
    source, _ = _source_with(_mock_response(status_code=200, text="<html>not json</html>"))

    with pytest.raises(ParseError):
        await source.search_papers("graph neural network")


@pytest.mark.asyncio
async def test_core_unsupported_operations_raise_not_supported() -> None:
    source = CoreSource()

    with pytest.raises(NotSupportedError):
        await source.get_author_papers("Geoffrey Hinton")
    with pytest.raises(NotSupportedError):
        await source.get_author_profile("Geoffrey Hinton")
    with pytest.raises(NotSupportedError):
        await source.get_citations("168955695")
