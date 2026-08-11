"""Unit tests for the Unpaywall source adapter (mocked HTTP).

Covers: has-OA, no-OA (is_oa=false), missing email (401), unknown DOI (404),
rate limit, parse errors, capability declaration, unsupported operations, and
the scorer baseline registration (0.85).
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
from academic_intelligence.core.models import Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.scorer import SOURCE_BASELINE_CONFIDENCE
from academic_intelligence.sources.unpaywall import OALocation, UnpaywallSource

DOI = "10.1038/s41586-025-09422-z"
EMAIL = "test@example.com"

OA_PAYLOAD = {
    "doi": DOI,
    "title": "A synthetic open-access test paper",
    "is_oa": True,
    "best_oa_location": {
        "url": f"https://www.nature.com/articles/{DOI.split('/')[-1]}",
        "url_for_pdf": f"https://www.nature.com/articles/{DOI.split('/')[-1]}.pdf",
        "host_type": "publisher",
        "license": "cc-by",
        "version": "publishedVersion",
    },
    "oa_locations": [
        {
            "url": f"https://www.nature.com/articles/{DOI.split('/')[-1]}",
            "url_for_pdf": f"https://www.nature.com/articles/{DOI.split('/')[-1]}.pdf",
            "host_type": "publisher",
            "license": "cc-by",
            "version": "publishedVersion",
        },
        {
            "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
            # Live API field is url_for_pdf; the dispatch facts sheet names
            # pdf_url — the parser accepts both spellings.
            "pdf_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/pdf/",
            "host_type": "repository",
            "version": "acceptedVersion",
        },
    ],
}

NO_OA_PAYLOAD = {
    "doi": DOI,
    "title": "A closed-access test paper",
    "is_oa": False,
    "best_oa_location": None,
    "oa_locations": [],
}


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


def _source(http: MagicMock, *, email: str | None = EMAIL) -> UnpaywallSource:
    return UnpaywallSource(http_client=http, email=email)


# ---------------------------------------------------------------------------
# Capabilities / contract
# ---------------------------------------------------------------------------


def test_unpaywall_instantiable_and_capabilities() -> None:
    source = _source(MagicMock())
    assert source.name == "unpaywall"
    assert source.source_type is SourceType.UNPAYWALL

    caps = source.capabilities
    assert caps["get"] is True
    assert caps["fulltext"] is True
    assert caps["search"] is False
    assert caps["citations"] is False
    # Long-form keys used by the current collector gate.
    assert caps["get_paper_by_doi"] is True
    assert caps["search_papers"] is False
    assert caps["get_author_papers"] is False
    assert caps["get_author_profile"] is False
    assert caps["get_citations"] is False

    assert source.supports("get") is True
    assert source.supports("fulltext") is True
    assert source.supports("search") is False
    assert source.supports("citations") is False
    assert source.supports("get_citations") is False


def test_unpaywall_unsupported_operations_raise() -> None:
    source = _source(MagicMock())
    import asyncio

    async def _probe() -> None:
        for coro_factory in (
            lambda: source.search_papers("query"),
            lambda: source.get_author_papers("Jane Doe"),
            lambda: source.get_author_profile("Jane Doe"),
            lambda: source.get_citations("10.1/foo"),
        ):
            with pytest.raises(NotSupportedError):
                await coro_factory()

    asyncio.run(_probe())


def test_unpaywall_confidence_matches_scorer_baseline() -> None:
    assert SOURCE_BASELINE_CONFIDENCE[SourceType.UNPAYWALL] == pytest.approx(0.85)
    assert UnpaywallSource().confidence == pytest.approx(0.85)
    assert UnpaywallSource().confidence == pytest.approx(
        SOURCE_BASELINE_CONFIDENCE[SourceType.UNPAYWALL]
    )


# ---------------------------------------------------------------------------
# Email handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpaywall_requires_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    http = MagicMock()
    http.get = AsyncMock()
    source = UnpaywallSource(http_client=http, email=None)
    with pytest.raises(AuthenticationError):
        await source.get_paper_by_doi(DOI)
    # Fails fast: no request is made without an email.
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_unpaywall_email_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNPAYWALL_EMAIL", "env@example.com")
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=OA_PAYLOAD))
    source = UnpaywallSource(http_client=http, email=None)
    await source.get_paper_by_doi(DOI)
    assert http.get.await_args.kwargs["params"]["email"] == "env@example.com"


@pytest.mark.asyncio
async def test_unpaywall_email_in_request_params() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=OA_PAYLOAD))
    source = _source(http)
    await source.get_paper_by_doi(DOI)
    assert http.get.await_args.kwargs["params"]["email"] == EMAIL


@pytest.mark.asyncio
async def test_unpaywall_401_rejected() -> None:
    http = MagicMock()
    http.get = AsyncMock(
        return_value=_mock_response(status_code=401, text="missing email")
    )
    source = _source(http, email="bad-email")
    with pytest.raises(AuthenticationError):
        await source.get_paper_by_doi(DOI)


@pytest.mark.asyncio
async def test_unpaywall_422_email_rejected() -> None:
    # The live API answers a missing/invalid email with 422 (observed
    # 2026-08-10); it is a configuration error, not a source outage.
    http = MagicMock()
    http.get = AsyncMock(
        return_value=_mock_response(
            status_code=422,
            text='{"HTTP_status_code": 422, "error": true, '
            '"message": "Email address required in API call"}',
        )
    )
    source = _source(http, email="bad-email")
    with pytest.raises(AuthenticationError):
        await source.get_paper_by_doi(DOI)


# ---------------------------------------------------------------------------
# get_paper_by_doi
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpaywall_get_paper_by_doi_oa() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=OA_PAYLOAD))
    source = _source(http)

    paper = await source.get_paper_by_doi(f"https://doi.org/{DOI}")

    assert paper is not None
    assert paper.doi == DOI  # prefix stripped + normalized
    assert paper.title == "A synthetic open-access test paper"
    assert paper.url == f"https://www.nature.com/articles/{DOI.split('/')[-1]}"
    assert paper.pdf_url.endswith(".pdf")
    assert paper.evidence.source is SourceType.UNPAYWALL
    raw = paper.evidence.raw_data or {}
    assert raw["is_oa"] is True
    assert len(raw["oa_locations"]) == 2


@pytest.mark.asyncio
async def test_unpaywall_get_paper_by_doi_no_oa() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=NO_OA_PAYLOAD))
    source = _source(http)

    paper = await source.get_paper_by_doi(DOI)

    assert paper is not None
    assert paper.title == "A closed-access test paper"
    assert paper.url is None
    assert paper.pdf_url is None
    assert (paper.evidence.raw_data or {}).get("is_oa") is False


@pytest.mark.asyncio
async def test_unpaywall_get_paper_by_doi_404() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(status_code=404, text="not found"))
    source = _source(http)
    assert await source.get_paper_by_doi(DOI) is None


@pytest.mark.asyncio
async def test_unpaywall_invalid_doi_returns_none() -> None:
    source = _source(MagicMock())
    assert await source.get_paper_by_doi("not-a-doi") is None


@pytest.mark.asyncio
async def test_unpaywall_rate_limit() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(status_code=429, text="slow down"))
    source = _source(http)
    with pytest.raises(RateLimitError):
        await source.get_paper_by_doi(DOI)


@pytest.mark.asyncio
async def test_unpaywall_parse_error_on_bad_json() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(text="<html>not json</html>"))
    source = _source(http)
    with pytest.raises(ParseError):
        await source.get_paper_by_doi(DOI)


# ---------------------------------------------------------------------------
# get_fulltext
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpaywall_get_fulltext_returns_links_best_first() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=OA_PAYLOAD))
    source = _source(http)

    paper = Paper(title="Test", doi=DOI)
    locations = await source.get_fulltext(paper)

    assert isinstance(locations, list)
    assert len(locations) == 2
    first, second = locations[0], locations[1]
    assert isinstance(first, OALocation)
    # best_oa_location first
    assert "nature.com" in first.url
    assert first.host_type == "publisher"
    assert first.license == "cc-by"
    assert first.version == "publishedVersion"
    assert first.pdf_url is not None and first.pdf_url.endswith(".pdf")
    # second location exercises the pdf_url spelling
    assert "pmc.ncbi.nlm.nih.gov" in second.url
    assert second.host_type == "repository"
    assert second.pdf_url is not None and second.pdf_url.endswith("/pdf/")
    assert second.license is None


@pytest.mark.asyncio
async def test_unpaywall_get_fulltext_no_doi() -> None:
    source = _source(MagicMock())
    assert await source.get_fulltext(Paper(title="No DOI")) == []


@pytest.mark.asyncio
async def test_unpaywall_get_fulltext_no_oa() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=NO_OA_PAYLOAD))
    source = _source(http)
    assert await source.get_fulltext(Paper(title="Test", doi=DOI)) == []


@pytest.mark.asyncio
async def test_unpaywall_get_fulltext_404() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(status_code=404, text="not found"))
    source = _source(http)
    assert await source.get_fulltext(Paper(title="Test", doi=DOI)) == []


@pytest.mark.asyncio
async def test_unpaywall_get_fulltext_reuses_own_evidence() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=OA_PAYLOAD))
    source = _source(http)

    paper = await source.get_paper_by_doi(DOI)
    assert paper is not None
    locations = await source.get_fulltext(paper)

    assert len(locations) == 2
    # get_paper_by_doi already fetched; get_fulltext must not re-query.
    assert http.get.await_count == 1
