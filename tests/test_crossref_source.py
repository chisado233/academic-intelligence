"""Unit tests for the Crossref source adapter (mocked HTTP).

The HTTP layer is replaced with an ``AsyncMock``-backed ``HTTPClient.get``,
so every test runs fully offline while still exercising the adapter's
request building, error mapping, and parsing paths.  Cassette-replay tests
live in ``tests/integration/test_crossref_cassettes.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

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
from academic_intelligence.sources.crossref import CrossrefSource
from academic_intelligence.utils.http import HTTPClient

# Realistic Crossref ``/works/{doi}`` payload for the DeepSeek-R1 paper.
DEEPSEEK_R1_DOI = "10.1038/s41586-025-09422-z"
DEEPSEEK_R1_WORK: dict[str, Any] = {
    "DOI": DEEPSEEK_R1_DOI,
    "title": ["DeepSeek-R1 incentivizes reasoning in LLMs through reinforcement learning"],
    "publisher": "Springer Nature",
    "container-title": ["Nature"],
    "author": [
        {
            "given": "Dayiheng",
            "family": "Liu",
            "affiliation": [{"name": "DeepSeek-AI"}],
        },
        {
            "given": "Qiyuan",
            "family": "Chen",
            "affiliation": [{"name": "DeepSeek-AI"}],
        },
    ],
    "issued": {"date-parts": [[2025, 9, 18]]},
    "is-referenced-by-count": 128,
    "link": [
        {
            "URL": "https://www.nature.com/articles/s41586-025-09422-z.pdf",
            "content-type": "application/pdf",
        }
    ],
}


def _response(status: int, *, json: Any | None = None, text: str | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=json,
        text=text,
        request=httpx.Request("GET", "https://api.crossref.org/works"),
    )


def _make_source(mailto: str | None = None) -> tuple[CrossrefSource, AsyncMock]:
    client = AsyncMock(spec=HTTPClient)
    source = CrossrefSource(http_client=client, mailto=mailto)
    return source, client


def _works_response(items: list[dict[str, Any]]) -> httpx.Response:
    return _response(200, json={"status": "ok", "message": {"items": items}})


# ---------------------------------------------------------------------------
# Contract / capabilities
# ---------------------------------------------------------------------------


def test_source_identity() -> None:
    source, _ = _make_source()
    assert source.name == "crossref"
    assert source.source_type == SourceType.CROSSREF


def test_capabilities_declared() -> None:
    source, _ = _make_source()
    caps = source.capabilities
    assert caps["search"] is True
    assert caps["get"] is True
    assert caps["citations"] is False
    assert caps["fulltext"] is False
    assert caps["get_author_papers"] is False
    assert caps["get_author_profile"] is False
    assert caps["get_citations"] is False


def test_supports() -> None:
    source, _ = _make_source()
    assert source.supports("search") is True
    assert source.supports("get") is True
    assert source.supports("citations") is False
    assert source.supports("fulltext") is False
    assert source.supports("get_author_papers") is False


# ---------------------------------------------------------------------------
# get_paper_by_doi
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paper_by_doi_fields() -> None:
    source, client = _make_source(mailto="researcher@example.org")
    client.get.return_value = _response(200, json={"message": DEEPSEEK_R1_WORK})

    paper = await source.get_paper_by_doi(DEEPSEEK_R1_DOI)

    assert paper is not None
    assert isinstance(paper, Paper)
    assert "DeepSeek-R1" in paper.title
    assert paper.doi == DEEPSEEK_R1_DOI
    assert paper.venue == "Nature"
    assert paper.publisher == "Springer Nature"
    assert paper.year == 2025
    assert paper.citations == 128
    assert paper.pdf_url == "https://www.nature.com/articles/s41586-025-09422-z.pdf"
    assert [a.name for a in paper.authors] == ["Dayiheng Liu", "Qiyuan Chen"]
    assert paper.authors[0].position == 1
    assert paper.authors[0].affiliation == "DeepSeek-AI"
    assert paper.evidence_list[0].source == SourceType.CROSSREF
    assert paper.evidence_list[0].source_id == DEEPSEEK_R1_DOI
    assert paper.evidence_list[0].confidence == pytest.approx(0.90)


@pytest.mark.asyncio
async def test_get_paper_by_doi_url_is_encoded_and_polite() -> None:
    source, client = _make_source(mailto="researcher@example.org")
    client.get.return_value = _response(200, json={"message": DEEPSEEK_R1_WORK})

    await source.get_paper_by_doi(DEEPSEEK_R1_DOI)

    args, kwargs = client.get.await_args
    url = args[0]
    assert url == "https://api.crossref.org/works/10.1038%2Fs41586-025-09422-z"
    params = kwargs["params"]
    assert params["mailto"] == "researcher@example.org"


@pytest.mark.asyncio
async def test_mailto_falls_back_to_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CROSSREF_MAILTO", "env-contact@example.org")
    source, client = _make_source()
    client.get.return_value = _response(200, json={"message": DEEPSEEK_R1_WORK})

    await source.get_paper_by_doi(DEEPSEEK_R1_DOI)

    params = client.get.await_args.kwargs["params"]
    assert params["mailto"] == "env-contact@example.org"


@pytest.mark.asyncio
async def test_no_mailto_param_when_unconfigured() -> None:
    source, client = _make_source()
    client.get.return_value = _response(200, json={"message": DEEPSEEK_R1_WORK})

    await source.get_paper_by_doi(DEEPSEEK_R1_DOI)

    assert "mailto" not in client.get.await_args.kwargs["params"]


@pytest.mark.asyncio
async def test_publisher_falls_back_to_prefix_map() -> None:
    work = {**DEEPSEEK_R1_WORK}
    del work["publisher"]
    source, client = _make_source()
    client.get.return_value = _response(200, json={"message": work})

    paper = await source.get_paper_by_doi(DEEPSEEK_R1_DOI)

    assert paper is not None
    assert paper.publisher == "Springer Nature"  # from utils.publisher_map


@pytest.mark.asyncio
async def test_publisher_none_when_map_has_no_entry() -> None:
    work = {
        **DEEPSEEK_R1_WORK,
        "DOI": "10.99999/unknown-publisher",
    }
    del work["publisher"]
    source, client = _make_source()
    client.get.return_value = _response(200, json={"message": work})

    paper = await source.get_paper_by_doi("10.99999/unknown-publisher")

    assert paper is not None
    assert paper.publisher is None


def test_parse_paper_publisher_fallback_fixture() -> None:
    """``_parse_paper`` backfills publisher from the prefix map (C5).

    Fixture-driven: a Crossref work without a ``publisher`` field (the live
    API virtually always carries one) maps through ``publisher_from_doi`` —
    here the 10.1101 (bioRxiv/medRxiv) prefix resolves to Cold Spring Harbor
    Laboratory.
    """
    work = {
        **DEEPSEEK_R1_WORK,
        "DOI": "10.1101/2024.01.01.575000",
    }
    del work["publisher"]
    source, _ = _make_source()

    paper = source._parse_paper(work)

    assert paper.publisher == "Cold Spring Harbor Laboratory"
    assert paper.doi == "10.1101/2024.01.01.575000"


@pytest.mark.asyncio
async def test_get_paper_by_doi_404_returns_none() -> None:
    source, client = _make_source()
    client.get.return_value = _response(404, text="Not Found")

    paper = await source.get_paper_by_doi("10.1038/s41586-025-09422-z")

    assert paper is None


@pytest.mark.asyncio
async def test_get_paper_by_doi_wrapped_input() -> None:
    source, client = _make_source()
    client.get.return_value = _response(200, json={"message": DEEPSEEK_R1_WORK})

    paper = await source.get_paper_by_doi(f"https://doi.org/{DEEPSEEK_R1_DOI}")

    assert paper is not None
    assert paper.doi == DEEPSEEK_R1_DOI


@pytest.mark.asyncio
async def test_invalid_doi_returns_none_without_request() -> None:
    source, client = _make_source()

    assert await source.get_paper_by_doi("not-a-doi") is None
    assert await source.get_paper_by_doi("") is None

    client.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# search_papers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_papers_parses_items() -> None:
    source, client = _make_source()
    client.get.return_value = _works_response([DEEPSEEK_R1_WORK, DEEPSEEK_R1_WORK])

    papers = await source.search_papers("deepseek reinforcement learning", limit=5)

    assert len(papers) == 2
    assert all(isinstance(p, Paper) for p in papers)
    assert all(p.evidence_list[0].source == SourceType.CROSSREF for p in papers)
    args, kwargs = client.get.await_args
    assert args[0] == "https://api.crossref.org/works"
    assert kwargs["params"]["query.bibliographic"] == "deepseek reinforcement learning"
    assert kwargs["params"]["rows"] == 5


@pytest.mark.asyncio
async def test_search_papers_respects_limit() -> None:
    source, client = _make_source()
    client.get.return_value = _works_response([DEEPSEEK_R1_WORK] * 10)

    papers = await source.search_papers("transformer", limit=3)

    assert len(papers) == 3
    assert client.get.await_args.kwargs["params"]["rows"] == 3


@pytest.mark.asyncio
async def test_search_empty_query_no_request() -> None:
    source, client = _make_source()

    assert await source.search_papers("   ") == []

    client.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_raises_with_retry_after() -> None:
    source, client = _make_source()
    client.get.return_value = _response(429, text="Too Many Requests", json=None)

    with pytest.raises(RateLimitError) as excinfo:
        await source.get_paper_by_doi(DEEPSEEK_R1_DOI)
    assert excinfo.value.retry_after == 1  # default Retry-After


@pytest.mark.asyncio
async def test_rate_limit_reads_retry_after_header() -> None:
    source, client = _make_source()
    client.get.return_value = httpx.Response(
        429,
        headers={"Retry-After": "3"},
        text="Too Many Requests",
        request=httpx.Request("GET", "https://api.crossref.org/works"),
    )

    with pytest.raises(RateLimitError) as excinfo:
        await source.get_paper_by_doi(DEEPSEEK_R1_DOI)
    assert excinfo.value.retry_after == 3


@pytest.mark.asyncio
async def test_http_5xx_raises_source_unavailable() -> None:
    source, client = _make_source()
    client.get.return_value = _response(500, text="internal error")

    with pytest.raises(SourceUnavailableError):
        await source.get_paper_by_doi(DEEPSEEK_R1_DOI)


@pytest.mark.asyncio
async def test_invalid_json_raises_parse_error() -> None:
    source, client = _make_source()
    client.get.return_value = _response(200, text="<html>not json</html>")

    with pytest.raises(ParseError):
        await source.get_paper_by_doi(DEEPSEEK_R1_DOI)


@pytest.mark.asyncio
async def test_timeout_raises_timeout_error() -> None:
    source, client = _make_source()
    client.get.side_effect = httpx.ConnectTimeout("connection timed out")

    with pytest.raises(TimeoutError):
        await source.get_paper_by_doi(DEEPSEEK_R1_DOI)


@pytest.mark.asyncio
async def test_transport_failure_raises_source_unavailable() -> None:
    source, client = _make_source()
    client.get.side_effect = httpx.ConnectError("connection refused")

    with pytest.raises(SourceUnavailableError):
        await source.get_paper_by_doi(DEEPSEEK_R1_DOI)


# ---------------------------------------------------------------------------
# Unsupported operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_operations_raise_not_supported() -> None:
    source, _ = _make_source()

    with pytest.raises(NotSupportedError):
        await source.get_author_papers("Geoffrey Hinton")
    with pytest.raises(NotSupportedError):
        await source.get_author_profile("Geoffrey Hinton")
    with pytest.raises(NotSupportedError):
        await source.get_citations("10.1038/s41586-025-09422-z")
