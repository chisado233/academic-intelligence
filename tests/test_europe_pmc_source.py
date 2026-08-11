"""Unit tests for the Europe PMC source adapter (mocked HTTP).

Covers the WP2c acceptance surface: search, get-by-DOI / PMID / PMCID,
OA full-text XML retrieval (open-access only), the non-OA guard, and the
error mapping (rate limit, parse failure, timeout, unsupported operations).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_intelligence.core.exceptions import (
    NotSupportedError,
    ParseError,
    RateLimitError,
    SourceUnavailableError,
    TimeoutError,
)
from academic_intelligence.core.models import Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.scorer import SOURCE_BASELINE_CONFIDENCE
from academic_intelligence.sources.europe_pmc import EuropePmcSource

SAMPLE_SEARCH: dict[str, Any] = {
    "hitCount": 2,
    "resultList": {
        "result": [
            {
                "id": "33033895",
                "source": "MED",
                "pmid": "33033895",
                "pmcid": None,
                "doi": "10.1007/s00426-020-01427-9",
                "title": "Is the construction of spatial models multimodal?",
                "authorList": {
                    "author": [
                        {
                            "fullName": "Grison E",
                            "firstName": "Elise",
                            "lastName": "Grison",
                            "initials": "E",
                            "authorAffiliationDetailsList": {
                                "authorAffiliation": [
                                    {
                                        "affiliation": "IFSTTAR, Versailles, France."
                                    }
                                ]
                            },
                        },
                        {
                            "fullName": "Jaco AA",
                            "firstName": "Amandine Afonso",
                            "lastName": "Jaco",
                            "initials": "AA",
                        },
                    ]
                },
                "pubYear": "2021",
                "journalInfo": {
                    "journal": {
                        "title": "Psychological research",
                        "medlineAbbreviation": "Psychol Res",
                    }
                },
                "abstractText": (
                    "<title>Abstract</title> <p>Using new developments of "
                    "interference paradigm, this paper addresses the raising "
                    "question of sensory-motor information.</p>"
                ),
                "isOpenAccess": "N",
                "inEPMC": "N",
                "fullTextUrlList": {
                    "fullTextUrl": [
                        {
                            "availability": "Subscription required",
                            "documentStyle": "doi",
                            "url": "https://doi.org/10.1007/s00426-020-01427-9",
                        }
                    ]
                },
            },
            {
                "id": "32479259",
                "source": "MED",
                "pmid": "32479259",
                "pmcid": "PMC7292645",
                "doi": "10.7554/eLife.56164",
                "title": "An open-access biomedicine study",
                "authorList": {
                    "author": [
                        {
                            "fullName": "Doe J",
                            "firstName": "Jane",
                            "lastName": "Doe",
                            "initials": "J",
                        }
                    ]
                },
                "pubYear": "2020",
                "journalInfo": {
                    "journal": {"title": "eLife", "medlineAbbreviation": "Elife"}
                },
                "abstractText": "An open-access abstract.",
                "isOpenAccess": "Y",
                "inEPMC": "Y",
                "fullTextUrlList": {
                    "fullTextUrl": [
                        {
                            "availability": "Open access",
                            "documentStyle": "pdf",
                            "url": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7292645/pdf/",
                        }
                    ]
                },
            },
        ]
    },
}

SAMPLE_DOI_RESULT: dict[str, Any] = {
    "hitCount": 1,
    "resultList": {
        "result": [
            {
                "id": "32939066",
                "source": "MED",
                "pmid": "32939066",
                "pmcid": "PMC7759461",
                "doi": "10.1038/s41586-020-2649-2",
                "title": "The species Severe acute respiratory syndrome-related coronavirus",
                "authorList": {
                    "author": [{"fullName": "Coronaviridae Study Group"}]
                },
                "pubYear": "2020",
                "journalInfo": {
                    "journal": {"title": "Nature Microbiology", "medlineAbbreviation": "Nat Microbiol"}
                },
                "abstractText": "The present study reviews the taxonomy.",
                "isOpenAccess": "Y",
                "inEPMC": "Y",
            }
        ]
    },
}

SAMPLE_FULLTEXT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><article>'
    "<front><article-meta><title-group><article-title>"
    "An open-access biomedicine study</article-title></title-group>"
    "<abstract><p>An open-access abstract.</p></abstract></article-meta></front>"
    "<body><sec><title>Introduction</title><p>Open-access full text.</p></sec></body>"
    "</article>"
)


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


def _source_with(response: MagicMock) -> tuple[EuropePmcSource, MagicMock]:
    http = MagicMock()
    http.get = AsyncMock(return_value=response)
    return EuropePmcSource(http_client=http), http


def _single_result_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Wrap one result record in a search payload shape."""
    return {"hitCount": 1, "resultList": {"result": [record]}}


def _paper_with(raw: dict[str, Any]) -> Paper:
    """Build a minimal Paper carrying one Europe PMC evidence with *raw*."""
    evidence = Evidence(
        source=SourceType.EUROPE_PMC,
        source_url="https://europepmc.org/article/MED/32479259",
        confidence=0.90,
        raw_data=raw,
    )
    return Paper(title="A paper", evidence_list=[evidence])


# ---------------------------------------------------------------------------
# Instantiation / capabilities / scorer baseline
# ---------------------------------------------------------------------------


def test_europe_pmc_source_capabilities() -> None:
    source = EuropePmcSource()
    assert source.name == "europe_pmc"
    assert source.source_type == SourceType.EUROPE_PMC
    assert source.confidence == 0.90
    # Long-form method keys used by the collector runtime.
    assert source.capabilities["search_papers"] is True
    assert source.capabilities["get_paper_by_doi"] is True
    assert source.capabilities["get_author_papers"] is False
    assert source.capabilities["get_author_profile"] is False
    assert source.capabilities["get_citations"] is False
    # C1 CLI operation keys (technical-design.md §1.1.1).
    assert source.capabilities["fulltext"] is True
    assert source.supports("search") is True
    assert source.supports("get") is True
    assert source.supports("citations") is False
    assert source.supports("author") is False
    assert source.supports("search_papers") is True
    assert source.supports("get_paper_by_doi") is True
    assert source.supports("get_author_papers") is False
    # supports() is fail-closed on capability declarations (C1 decision 2,
    # technical-design.md §1.1.1): get_fulltext is not a declared capability
    # key, so it is unsupported even though the method exists — the CLI
    # drives off the declared ``fulltext`` key instead.
    assert source.supports("get_fulltext") is False


def test_europe_pmc_scorer_baseline_registered() -> None:
    assert SOURCE_BASELINE_CONFIDENCE[SourceType.EUROPE_PMC] == 0.90


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_papers_parses_results() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_SEARCH))

    papers = await source.search_papers("spatial models", limit=5)

    assert len(papers) == 2
    first = papers[0]
    assert first.id == "33033895"
    assert first.title == "Is the construction of spatial models multimodal?"
    assert [a.name for a in first.authors] == ["Grison E", "Jaco AA"]
    assert first.authors[0].affiliation == "IFSTTAR, Versailles, France."
    assert first.year == 2021
    assert first.venue == "Psychological research"
    assert first.doi == "10.1007/s00426-020-01427-9"
    assert first.pmid == "33033895"
    assert first.abstract and "sensory-motor information" in first.abstract
    assert first.url == "https://europepmc.org/article/MED/33033895"
    assert first.evidence_list[0].source == SourceType.EUROPE_PMC
    assert first.evidence_list[0].source_id == "33033895"
    # Non-OA record: no fulltext_url, no pdf_url, is_open_access False.
    raw = first.evidence_list[0].raw_data or {}
    assert raw["is_open_access"] is False
    assert raw["fulltext_url"] is None
    assert first.pdf_url is None

    second = papers[1]
    assert second.pmid == "32479259"
    assert second.year == 2020
    assert second.venue == "eLife"
    assert second.pdf_url == (
        "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7292645/pdf/"
    )
    raw2 = second.evidence_list[0].raw_data or {}
    assert raw2["is_open_access"] is True
    assert raw2["pmcid"] == "PMC7292645"
    assert raw2["fulltext_url"] == (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC7292645/fullTextXML"
    )

    call = http.get.await_args
    assert call is not None
    assert call.kwargs["params"]["query"] == "spatial models"
    assert call.kwargs["params"]["format"] == "json"
    assert call.kwargs["params"]["pageSize"] == 5
    assert call.kwargs["params"]["resultType"] == "core"
    assert call.args[0] == "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


@pytest.mark.asyncio
async def test_search_empty_query_returns_no_request() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_SEARCH))

    assert await source.search_papers("   ") == []
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_no_results_returns_empty() -> None:
    source, _ = _source_with(
        _mock_response(json_data={"hitCount": 0, "resultList": {"result": []}})
    )

    assert await source.search_papers("nothing here") == []


@pytest.mark.asyncio
async def test_search_skips_malformed_records() -> None:
    payload = {
        "resultList": {"result": [{"id": "1"}, {"not": "a paper"}]}
    }
    source, _ = _source_with(_mock_response(json_data=payload))

    assert await source.search_papers("graph neural network") == []


# ---------------------------------------------------------------------------
# Get by DOI / PMID / PMCID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paper_by_doi() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_DOI_RESULT))

    paper = await source.get_paper_by_doi("https://doi.org/10.1038/s41586-020-2649-2")

    assert paper is not None
    assert paper.pmid == "32939066"
    assert paper.doi == "10.1038/s41586-020-2649-2"
    assert paper.title.startswith("The species")
    raw = paper.evidence_list[0].raw_data or {}
    assert raw["fulltext_url"] == (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC7759461/fullTextXML"
    )
    call = http.get.await_args
    assert call is not None
    assert call.kwargs["params"]["query"] == 'DOI:"10.1038/s41586-020-2649-2"'


@pytest.mark.asyncio
async def test_get_paper_by_doi_invalid_returns_none() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_SEARCH))

    assert await source.get_paper_by_doi("not-a-doi") is None
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_paper_by_doi_not_found_returns_none() -> None:
    source, _ = _source_with(_mock_response(status_code=404, text="not found"))

    assert await source.get_paper_by_doi("10.1038/s41586-020-9999-9") is None


@pytest.mark.asyncio
async def test_get_paper_by_pmid() -> None:
    payload = _single_result_payload(SAMPLE_SEARCH["resultList"]["result"][1])
    source, http = _source_with(_mock_response(json_data=payload))

    paper = await source.get_paper_by_pmid("32479259")

    assert paper is not None
    assert paper.pmid == "32479259"
    call = http.get.await_args
    assert call is not None
    assert call.kwargs["params"]["query"] == "EXT_ID:32479259"


@pytest.mark.asyncio
async def test_get_paper_by_pmid_invalid_returns_none() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_SEARCH))

    assert await source.get_paper_by_pmid("not-a-pmid") is None
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_paper_by_pmcid_accepts_prefix_and_bare() -> None:
    payload = _single_result_payload(SAMPLE_SEARCH["resultList"]["result"][1])
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=payload))
    source = EuropePmcSource(http_client=http)

    paper = await source.get_paper_by_pmcid("PMC7292645")
    assert paper is not None
    assert paper.pmid == "32479259"
    call = http.get.await_args
    assert call is not None
    assert call.kwargs["params"]["query"] == "PMCID:PMC7292645"

    await source.get_paper_by_pmcid("7292645")
    second_call = http.get.await_args
    assert second_call is not None
    assert second_call.kwargs["params"]["query"] == "PMCID:PMC7292645"


@pytest.mark.asyncio
async def test_get_paper_by_pmcid_invalid_returns_none() -> None:
    source, http = _source_with(_mock_response(json_data=SAMPLE_SEARCH))

    assert await source.get_paper_by_pmcid("PMC") is None
    http.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# Full text (open access only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_fulltext_oa_returns_xml() -> None:
    source, http = _source_with(_mock_response(text=SAMPLE_FULLTEXT_XML))
    paper = _paper_with(
        {"is_open_access": True, "pmcid": "PMC7292645", "fulltext_url": "x"}
    )

    xml = await source.get_fulltext(paper)

    assert xml == SAMPLE_FULLTEXT_XML
    call = http.get.await_args
    assert call is not None
    assert call.args[0] == (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC7292645/fullTextXML"
    )


@pytest.mark.asyncio
async def test_get_fulltext_non_oa_returns_none_without_request() -> None:
    source, http = _source_with(_mock_response(text=SAMPLE_FULLTEXT_XML))
    paper = _paper_with({"is_open_access": False, "pmcid": "PMC7292645"})

    assert await source.get_fulltext(paper) is None
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_fulltext_missing_pmcid_returns_none() -> None:
    source, http = _source_with(_mock_response(text=SAMPLE_FULLTEXT_XML))
    paper = _paper_with({"is_open_access": True, "pmcid": None})

    assert await source.get_fulltext(paper) is None
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_fulltext_no_europe_pmc_evidence_returns_none() -> None:
    source, http = _source_with(_mock_response(text=SAMPLE_FULLTEXT_XML))
    paper = Paper(title="A paper")  # no Europe PMC evidence

    assert await source.get_fulltext(paper) is None
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_fulltext_404_returns_none() -> None:
    source, _ = _source_with(_mock_response(status_code=404, text="not found"))
    paper = _paper_with({"is_open_access": True, "pmcid": "PMC7292645"})

    assert await source.get_fulltext(paper) is None


# ---------------------------------------------------------------------------
# Error mapping / unsupported operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_429_raises() -> None:
    source, _ = _source_with(
        _mock_response(status_code=429, headers={"Retry-After": "5"}, text="slow down")
    )

    with pytest.raises(RateLimitError) as excinfo:
        await source.search_papers("graph neural network")
    assert excinfo.value.retry_after == 5


@pytest.mark.asyncio
async def test_http_500_raises_source_unavailable() -> None:
    source, _ = _source_with(_mock_response(status_code=500, text="boom"))

    with pytest.raises(SourceUnavailableError):
        await source.search_papers("graph neural network")


@pytest.mark.asyncio
async def test_invalid_json_raises_parse_error() -> None:
    source, _ = _source_with(_mock_response(status_code=200, text="<html>not json</html>"))

    with pytest.raises(ParseError):
        await source.search_papers("graph neural network")


@pytest.mark.asyncio
async def test_timeout_raises_timeout_error() -> None:
    from httpx import ReadTimeout

    http = MagicMock()
    http.get = AsyncMock(side_effect=ReadTimeout("timed out"))
    source = EuropePmcSource(http_client=http)

    with pytest.raises(TimeoutError):
        await source.search_papers("graph neural network")


@pytest.mark.asyncio
async def test_unsupported_operations_raise_not_supported() -> None:
    source = EuropePmcSource()

    with pytest.raises(NotSupportedError):
        await source.get_author_papers("Geoffrey Hinton")
    with pytest.raises(NotSupportedError):
        await source.get_author_profile("Geoffrey Hinton")
    with pytest.raises(NotSupportedError):
        await source.get_citations("33033895")
