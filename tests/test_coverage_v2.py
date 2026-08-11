"""Coverage-boosting tests for the 3A v2 model batch.

Targets untested branches in source adapters (mocked HTTP), storage
backends, the AcademicIntelligence facade (offline cassettes), the
collector orchestration, and processor strategies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_intelligence.core.exceptions import (
    AllSourcesFailedError,
    AuthenticationError,
    CollectorError,
    EnrichmentError,
    ParseError,
    RateLimitError,
    SourceUnavailableError,
)
from academic_intelligence.core.models import (
    Author,
    Citation,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.processors.enricher import (
    AffiliationEnrichmentStrategy,
    AuthorListNormalizeStrategy,
    CitationCountEnrichmentStrategy,
    DoiExtractionStrategy,
    Enricher,
    PdfFromUrlStrategy,
    TitleNormalizeStrategy,
    VenueNormalizationStrategy,
)
from academic_intelligence.processors.validator import Validator, ValidatorConfig
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.google_scholar import GoogleScholarSource
from academic_intelligence.sources.ieee import IEEESource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.sources.pubmed import PubMedSource
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from tests.cassette_replay import install_cassette


def _ev(source: SourceType = SourceType.OPENALEX, conf: float = 0.9) -> Evidence:
    return Evidence(source=source, source_url="https://example.com", confidence=conf)


def _mock_response(
    *,
    status_code: int = 200,
    text: str = "",
    json_data: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
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


def _http_with(*responses: Any) -> MagicMock:
    http = MagicMock()
    http.get = AsyncMock(side_effect=list(responses))
    return http


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------


def test_openalex_parse_paper_full() -> None:
    src = OpenAlexSource(http_client=MagicMock())
    data: Dict[str, Any] = {
        "title": "Test Paper",
        "authorships": [
            {"author": {"display_name": "Alice Chen"}},
            {"author": {"display_name": "Bob Wilson"}},
        ],
        "ids": {"doi": "10.1234/abc.def", "openalex": "https://openalex.org/W123"},
        "doi": "https://doi.org/10.1234/abc.def",
        "primary_location": {
            "source": {"display_name": "Nature"},
            "pdf_url": "https://example.com/p.pdf",
            "landing_page_url": "https://example.com/paper",
        },
        "open_access": {"oa_url": None},
        "publication_year": 2021,
        "keywords": [{"display_name": "AI"}, "ML"],
        "abstract_inverted_index": {"world": [1], "Hello": [0]},
        "cited_by_count": 42,
    }
    paper = src._parse_paper(data)
    assert paper.title == "Test Paper"
    assert [a.name for a in paper.authors] == ["Alice Chen", "Bob Wilson"]
    assert paper.authors[0].position == 1
    assert paper.doi == "10.1234/abc.def"
    assert paper.url == "https://example.com/paper"
    assert paper.pdf_url == "https://example.com/p.pdf"
    assert paper.citations == 42
    assert paper.keywords == ["AI", "ML"]
    assert paper.abstract == "Hello world"
    assert paper.id == "W123"
    assert paper.evidence.source_id == "10.1234/abc.def"


def test_openalex_parse_paper_fallback_untitled() -> None:
    src = OpenAlexSource(http_client=MagicMock())
    paper = src._parse_paper({})
    assert paper.title == "Untitled"
    assert paper.authors == []
    assert paper.year is None
    assert paper.doi is None


def test_openalex_parse_author() -> None:
    src = OpenAlexSource(http_client=MagicMock())
    author = src._parse_author(
        {
            "id": "https://openalex.org/A1",
            "display_name": "Alice Chen",
            "last_known_institution": {"display_name": "MIT"},
            "summary_stats": {"h_index": 12},
            "cited_by_count": 100,
        }
    )
    assert author.id == "A1"
    assert author.openalex_id == "A1"
    assert author.name == "Alice Chen"
    assert author.affiliation == "MIT"
    assert author.h_index == 12
    assert author.citations == 100


@pytest.mark.asyncio
async def test_openalex_get_paper_by_doi_and_errors() -> None:
    src = OpenAlexSource(
        http_client=_http_with(_mock_response(json_data={"id": "https://openalex.org/W1", "title": "X"}))
    )
    paper = await src.get_paper_by_doi("https://doi.org/10.1234/abc.def")
    assert paper is not None and paper.title == "X"

    src404 = OpenAlexSource(http_client=_http_with(_mock_response(status_code=404)))
    assert await src404.get_paper_by_doi("10.1234/x.y") is None

    src429 = OpenAlexSource(http_client=_http_with(_mock_response(status_code=429)))
    with pytest.raises(RateLimitError):
        await src429.get_paper_by_doi("10.1234/x.y")

    src500 = OpenAlexSource(http_client=_http_with(_mock_response(status_code=500, text="boom")))
    with pytest.raises(SourceUnavailableError):
        await src500.get_paper_by_doi("10.1234/x.y")

    src_raise = OpenAlexSource(http_client=_http_with(RuntimeError("conn refused")))
    with pytest.raises(SourceUnavailableError):
        await src_raise.get_paper_by_doi("10.1234/x.y")

    src_badjson = OpenAlexSource(http_client=_http_with(_mock_response(text="<html>")))
    with pytest.raises(ParseError):
        await src_badjson.search_papers("q")


@pytest.mark.asyncio
async def test_openalex_get_author_papers_and_profile() -> None:
    # Author search hit -> works with author.id filter
    src = OpenAlexSource(
        http_client=_http_with(
            _mock_response(json_data={"results": [{"id": "https://openalex.org/A1"}]}),
            _mock_response(json_data={"results": [{"id": "W1", "title": "Work"}]}),
        )
    )
    papers = await src.get_author_papers("Alice Chen")
    assert len(papers) == 1
    assert papers[0].title == "Work"

    # Author search miss -> fallback to paper search
    src2 = OpenAlexSource(
        http_client=_http_with(
            _mock_response(json_data={"results": []}),
            _mock_response(json_data={"results": [{"title": "Fallback"}]}),
        )
    )
    papers2 = await src2.get_author_papers("Nobody")
    assert len(papers2) == 1
    assert papers2[0].title == "Fallback"

    # Profile miss -> None
    src3 = OpenAlexSource(http_client=_http_with(_mock_response(json_data={"results": []})))
    assert await src3.get_author_profile("Nobody") is None


@pytest.mark.asyncio
async def test_openalex_get_citations() -> None:
    src = OpenAlexSource(
        http_client=_http_with(
            _mock_response(
                json_data={
                    "results": [
                        {"id": "https://openalex.org/W10"},
                        {"id": "https://openalex.org/W5"},
                    ]
                }
            )
        )
    )
    cites = await src.get_citations("https://openalex.org/W5")
    assert len(cites) == 1
    assert cites[0].citing_paper_id == "W10"
    assert cites[0].cited_paper_id == "W5"


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------


def test_semantic_scholar_parse_paper_full() -> None:
    src = SemanticScholarSource(http_client=MagicMock())
    data: Dict[str, Any] = {
        "paperId": "abc123",
        "title": "S2 Paper",
        "abstract": "An abstract.",
        "year": 2020,
        "venue": "NeurIPS",
        "citationCount": 7,
        "externalIds": {"DOI": "10.1234/abc.def", "ArXiv": "1706.03762", "PMID": "87654321"},
        "url": "https://www.semanticscholar.org/paper/abc123",
        "openAccessPdf": {"url": "https://example.com/s2.pdf"},
        "authors": [{"name": "Ada Lovelace"}],
        "fieldsOfStudy": ["Computer Science"],
    }
    paper = src._parse_paper(data)
    assert paper.id == "abc123"
    assert paper.arxiv_id == "1706.03762"
    assert paper.pmid == "87654321"
    assert paper.fields_of_study == ["Computer Science"]
    assert paper.keywords == ["Computer Science"]
    assert paper.doi == "10.1234/abc.def"
    assert paper.evidence.source_id == "abc123"
    assert [a.name for a in paper.authors] == ["Ada Lovelace"]


def test_semantic_scholar_parse_paper_soft_doi_and_no_abstract() -> None:
    src = SemanticScholarSource(http_client=MagicMock())
    paper = src._parse_paper({"title": "  ", "externalIds": {"DOI": "bad-doi"}})
    assert paper.title == "Untitled"
    assert paper.doi is None  # softened


def test_semantic_scholar_parse_author() -> None:
    src = SemanticScholarSource(http_client=MagicMock())
    author = src._parse_author(
        {
            "authorId": "1695689",
            "name": "Geoffrey Hinton",
            "affiliations": [{"name": "U Toronto"}],
            "homepage": "https://www.cs.toronto.edu/~hinton/",
            "hIndex": 100,
            "citationCount": 500,
            "url": "https://www.semanticscholar.org/author/1695689",
        }
    )
    assert author.id == "1695689"
    assert author.semantic_scholar_id == "1695689"
    assert author.affiliation == "U Toronto"
    assert author.h_index == 100
    assert author.evidence.source_id == "1695689"


@pytest.mark.asyncio
async def test_semantic_scholar_get_paper_by_doi_and_errors() -> None:
    src = SemanticScholarSource(
        http_client=_http_with(_mock_response(json_data={"paperId": "p1", "title": "Y"}))
    )
    paper = await src.get_paper_by_doi("https://doi.org/10.1234/abc.def")
    assert paper is not None and paper.title == "Y"

    src401 = SemanticScholarSource(http_client=_http_with(_mock_response(status_code=401)))
    with pytest.raises(AuthenticationError):
        await src401.get_paper_by_doi("10.1/x")

    src403 = SemanticScholarSource(http_client=_http_with(_mock_response(status_code=403)))
    with pytest.raises(AuthenticationError):
        await src403.get_paper_by_doi("10.1/x")

    src429 = SemanticScholarSource(
        http_client=_http_with(_mock_response(status_code=429, headers={"Retry-After": "2"}))
    )
    with pytest.raises(RateLimitError):
        await src429.get_paper_by_doi("10.1/x")

    src500 = SemanticScholarSource(http_client=_http_with(_mock_response(status_code=500)))
    with pytest.raises(SourceUnavailableError):
        await src500.get_paper_by_doi("10.1/x")

    src_raise = SemanticScholarSource(http_client=_http_with(RuntimeError("down")))
    with pytest.raises(SourceUnavailableError):
        await src_raise.get_paper_by_doi("10.1/x")

    src_badjson = SemanticScholarSource(http_client=_http_with(_mock_response(text="nope")))
    with pytest.raises(ParseError):
        await src_badjson.get_paper_by_doi("10.1/x")


@pytest.mark.asyncio
async def test_semantic_scholar_get_paper_by_doi_url_encoded() -> None:
    """I-1: the DOI is percent-encoded in the API path (``/`` → ``%2F``).

    Standard DOIs such as 10.1038/nature14539 silently 404'd when the raw
    slash was embedded in the URL path.
    """
    http = _http_with(
        _mock_response(json_data={"paperId": "p1", "title": "Deep learning"}),
        _mock_response(json_data={"paperId": "p2", "title": "Another"}),
    )
    src = SemanticScholarSource(http_client=http)

    paper = await src.get_paper_by_doi("10.1038/nature14539")
    assert paper is not None and paper.title == "Deep learning"
    url = http.get.call_args.args[0]
    assert "/paper/DOI:10.1038%2Fnature14539" in url
    assert "10.1038/nature14539" not in url

    # the doi.org/ prefix is normalized away before encoding
    paper2 = await src.get_paper_by_doi("https://doi.org/10.1038/nature14539")
    assert paper2 is not None and paper2.title == "Another"
    url2 = http.get.call_args.args[0]
    assert "/paper/DOI:10.1038%2Fnature14539" in url2


@pytest.mark.asyncio
async def test_semantic_scholar_get_author_papers_fallback_and_profile() -> None:
    # Author search miss -> fallback to paper search
    src = SemanticScholarSource(
        http_client=_http_with(
            _mock_response(json_data={"data": []}),
            _mock_response(json_data={"data": [{"paperId": "p1", "title": "Fallback S2"}]}),
        )
    )
    papers = await src.get_author_papers("Nobody")
    assert len(papers) == 1
    assert papers[0].title == "Fallback S2"

    src2 = SemanticScholarSource(
        http_client=_http_with(_mock_response(json_data={"data": []}))
    )
    assert await src2.get_author_profile("Nobody") is None


@pytest.mark.asyncio
async def test_semantic_scholar_get_citations() -> None:
    src = SemanticScholarSource(
        http_client=_http_with(
            _mock_response(
                json_data={
                    "data": [
                        {"citingPaper": {"paperId": "c1"}},
                        {"citingPaper": {"paperId": "p1"}},
                        {"citingPaper": {}},
                    ]
                }
            )
        )
    )
    cites = await src.get_citations("p1")
    assert len(cites) == 1
    assert cites[0].citing_paper_id == "c1"
    assert cites[0].cited_paper_id == "p1"


# ---------------------------------------------------------------------------
# Google Scholar
# ---------------------------------------------------------------------------


def test_google_scholar_parse_organic_authors_list() -> None:
    src = GoogleScholarSource(serpapi_key="k")
    item = {
        "title": "GS Paper",
        "publication_info": {
            "authors": [{"name": "Alice Chen"}, {"name": "Bob Wilson"}],
            "summary": "Alice Chen - Some Venue, 2020 - pub",
        },
        "inline_links": {"cited_by": {"total": "12"}},
        "link": "https://example.com/gs",
        "resources": [{"file_format": "PDF", "link": "https://example.com/p.pdf"}],
        "result_id": "gs-1",
        "snippet": "snippet text",
    }
    paper = src._parse_organic(item, "https://scholar.google.com")
    assert paper.id == "gs-1"
    assert [a.name for a in paper.authors] == ["Alice Chen", "Bob Wilson"]
    assert paper.year == 2020
    assert paper.citations == 12
    assert paper.pdf_url == "https://example.com/p.pdf"
    assert paper.evidence.source_id == "gs-1"


def test_google_scholar_parse_organic_summary_fallback() -> None:
    src = GoogleScholarSource(serpapi_key="k")
    item = {
        "title": "No authors listed",
        "publication_info": {"summary": "A Author, B Author - Venue, 2019 - publisher"},
    }
    paper = src._parse_organic(item, "https://scholar.google.com")
    assert [a.name for a in paper.authors] == ["A Author", "B Author"]
    assert paper.year == 2019
    assert paper.venue == "Venue"


def test_google_scholar_parse_organic_missing_title() -> None:
    src = GoogleScholarSource(serpapi_key="k")
    assert src._parse_organic({"title": "  "}, "https://scholar.google.com") is None


@pytest.mark.asyncio
async def test_google_scholar_search_errors() -> None:
    src429 = GoogleScholarSource(serpapi_key="k", http_client=_http_with(_mock_response(status_code=429)))
    with pytest.raises(RateLimitError):
        await src429.search_papers("q")

    src401 = GoogleScholarSource(serpapi_key="k", http_client=_http_with(_mock_response(status_code=401)))
    with pytest.raises(AuthenticationError):
        await src401.search_papers("q")

    src500 = GoogleScholarSource(serpapi_key="k", http_client=_http_with(_mock_response(status_code=500)))
    with pytest.raises(SourceUnavailableError):
        await src500.search_papers("q")

    src_raise = GoogleScholarSource(serpapi_key="k", http_client=_http_with(RuntimeError("x")))
    with pytest.raises(SourceUnavailableError):
        await src_raise.search_papers("q")

    src_badjson = GoogleScholarSource(serpapi_key="k", http_client=_http_with(_mock_response(text="x")))
    with pytest.raises(ParseError):
        await src_badjson.search_papers("q")

    src_err = GoogleScholarSource(
        serpapi_key="k", http_client=_http_with(_mock_response(json_data={"error": "quota"}))
    )
    with pytest.raises(SourceUnavailableError):
        await src_err.search_papers("q")

    src_nokey = GoogleScholarSource(serpapi_key=None, http_client=MagicMock())
    with pytest.raises(AuthenticationError):
        await src_nokey.search_papers("q")


@pytest.mark.asyncio
async def test_google_scholar_get_author_profile() -> None:
    profile = {
        "name": "Alice Chen",
        "author_id": "abc",
        "affiliations": "MIT",
        "cited_by": 99,
        "interests": [{"title": "ML"}],
        "link": "https://scholar.google.com/citations?user=abc",
    }
    src = GoogleScholarSource(
        serpapi_key="k",
        http_client=_http_with(_mock_response(json_data={"profiles": [profile]})),
    )
    author = await src.get_author_profile("Alice Chen")
    assert author is not None
    assert author.name == "Alice Chen"
    assert author.citations == 99
    assert author.interests == ["ML"]

    src2 = GoogleScholarSource(
        serpapi_key="k", http_client=_http_with(_mock_response(json_data={"profiles": []}))
    )
    assert await src2.get_author_profile("Nobody") is None

    src3 = GoogleScholarSource(serpapi_key="k", http_client=_http_with(RuntimeError("boom")))
    assert await src3.get_author_profile("Nobody") is None


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

SAMPLE_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T00:00:00Z</published>
    <title>Attention Is All You Need</title>
    <summary>Summary text.</summary>
    <author><name>Ashish Vaswani</name></author>
    <link href="http://arxiv.org/abs/1706.03762v7" rel="alternate" type="text/html"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
  </entry>
</feed>
"""


@pytest.mark.asyncio
async def test_arxiv_get_paper_by_arxiv_id() -> None:
    src = ArxivSource(
        http_client=_http_with(_mock_response(text=SAMPLE_ARXIV_ATOM)),
        min_interval_seconds=0.01,
    )
    paper = await src.get_paper_by_arxiv_id("1706.03762")
    assert paper is not None
    assert paper.arxiv_id == "1706.03762v7"
    assert paper.evidence.source_id == "1706.03762v7"


@pytest.mark.asyncio
async def test_arxiv_get_paper_by_arxiv_id_bad_id() -> None:
    src = ArxivSource(http_client=MagicMock(), min_interval_seconds=0.01)
    assert await src.get_paper_by_arxiv_id("!!!not-an-id!!!") is None


@pytest.mark.asyncio
async def test_arxiv_query_errors() -> None:
    src429 = ArxivSource(
        http_client=_http_with(_mock_response(status_code=429, headers={"Retry-After": "2"})),
        min_interval_seconds=0.01,
    )
    with pytest.raises(RateLimitError):
        await src429.search_papers("transformer")

    src500 = ArxivSource(
        http_client=_http_with(_mock_response(status_code=500)), min_interval_seconds=0.01
    )
    with pytest.raises(SourceUnavailableError):
        await src500.search_papers("transformer")

    src_raise = ArxivSource(
        http_client=_http_with(RuntimeError("net")), min_interval_seconds=0.01
    )
    with pytest.raises(SourceUnavailableError):
        await src_raise.search_papers("transformer")


@pytest.mark.asyncio
async def test_arxiv_parse_feed_invalid_xml() -> None:
    src = ArxivSource(
        http_client=_http_with(_mock_response(text="<not xml")), min_interval_seconds=0.01
    )
    with pytest.raises(ParseError):
        await src.search_papers("transformer")


@pytest.mark.asyncio
async def test_arxiv_author_profile_no_papers(monkeypatch: pytest.MonkeyPatch) -> None:
    src = ArxivSource(http_client=MagicMock(), min_interval_seconds=0.01)
    monkeypatch.setattr(src, "get_author_papers", AsyncMock(return_value=[]))
    assert await src.get_author_profile("Nobody") is None


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------


def test_pubmed_parse_article_collective_and_medline_date() -> None:
    xml = """<PubmedArticleSet><PubmedArticle>
      <MedlineCitation>
        <PMID>999</PMID>
        <Article>
          <Journal>
            <Title>Lancet</Title>
            <JournalIssue><PubDate><MedlineDate>2020 Jan-Feb</MedlineDate></PubDate></JournalIssue>
          </Journal>
          <ArticleTitle>Collective work</ArticleTitle>
          <AuthorList>
            <Author><CollectiveName>COVID-19 Group</CollectiveName></Author>
          </AuthorList>
          <Abstract><AbstractText>Body.</AbstractText></Abstract>
        </Article>
      </MedlineCitation>
      <PubmedData><ArticleIdList><ArticleId IdType="pubmed">999</ArticleId></ArticleIdList></PubmedData>
    </PubmedArticle></PubmedArticleSet>"""
    src = PubMedSource(http_client=MagicMock())
    papers = src._parse_efetch_xml(xml)
    assert len(papers) == 1
    paper = papers[0]
    assert paper.pmid == "999"
    assert paper.year == 2020
    assert [a.name for a in paper.authors] == ["COVID-19 Group"]


@pytest.mark.asyncio
async def test_pubmed_get_citations_elink() -> None:
    src = PubMedSource(
        http_client=_http_with(
            _mock_response(
                json_data={
                    "linksets": [
                        {
                            "linksetdbs": [
                                {
                                    "links": ["111", "999", "222"],
                                }
                            ]
                        }
                    ]
                }
            )
        )
    )
    cites = await src.get_citations("999")
    assert len(cites) == 2
    assert {c.citing_paper_id for c in cites} == {"111", "222"}


@pytest.mark.asyncio
async def test_pubmed_get_citations_failure_returns_empty() -> None:
    src = PubMedSource(http_client=_http_with(RuntimeError("down")))
    assert await src.get_citations("999") == []


@pytest.mark.asyncio
async def test_pubmed_get_errors() -> None:
    src429 = PubMedSource(http_client=_http_with(_mock_response(status_code=429)))
    with pytest.raises(RateLimitError):
        await src429.get_author_papers("Smith")

    src500 = PubMedSource(http_client=_http_with(_mock_response(status_code=500)))
    with pytest.raises(SourceUnavailableError):
        await src500.get_author_papers("Smith")

    src_raise = PubMedSource(http_client=_http_with(RuntimeError("x")))
    with pytest.raises(SourceUnavailableError):
        await src_raise.get_author_papers("Smith")

    src_badjson = PubMedSource(http_client=_http_with(_mock_response(text="x")))
    with pytest.raises(ParseError):
        await src_badjson.search_papers("cancer")


@pytest.mark.asyncio
async def test_pubmed_author_profile_no_papers(monkeypatch: pytest.MonkeyPatch) -> None:
    src = PubMedSource(http_client=MagicMock())
    monkeypatch.setattr(src, "get_author_papers", AsyncMock(return_value=[]))
    assert await src.get_author_profile("Nobody") is None


# ---------------------------------------------------------------------------
# IEEE
# ---------------------------------------------------------------------------


def test_ieee_parse_paper_variants() -> None:
    src = IEEESource(api_key="k", http_client=MagicMock())
    data: Dict[str, Any] = {
        "article_title": "Fallback Title",
        "authors": [{"full_name": "Alice Chen"}],
        "publication_year": "20xx",
        "conference_name": "ICLR",
        "abstract": "  spaced  ",
        "doi": "10.1109/X.1",
        "articleNumber": "12345",
        "html_url": "//ieeexplore.ieee.org/document/12345",
        "pdf_url": "//ieeexplore.ieee.org/stamp/12345",
        "citing_paper_count": "3",
        "index_terms": {"ieee_terms": ["Transformers"]},
        "keywords": ["extra"],
    }
    paper = src._parse_paper(data)
    assert paper.title == "Fallback Title"
    assert paper.year is None
    assert paper.venue == "ICLR"
    assert paper.abstract == "spaced"
    assert paper.id == "12345"
    assert paper.citations == 3
    assert paper.url == "https://ieeexplore.ieee.org/document/12345"
    assert "Transformers" in paper.keywords and "extra" in paper.keywords


def test_ieee_parse_authors_fallback_string() -> None:
    src = IEEESource(api_key="k", http_client=MagicMock())
    assert src._parse_authors({"author_names": "Alice Chen; Bob Wilson"}) == [
        "Alice Chen",
        "Bob Wilson",
    ]
    assert src._parse_authors({"authors": [{"preferred_name": "Carol"}]}) == ["Carol"]


@pytest.mark.asyncio
async def test_ieee_search_errors() -> None:
    src429 = IEEESource(api_key="k", http_client=_http_with(_mock_response(status_code=429)))
    with pytest.raises(RateLimitError):
        await src429.search_papers("q")

    src401 = IEEESource(api_key="k", http_client=_http_with(_mock_response(status_code=401)))
    with pytest.raises(AuthenticationError):
        await src401.search_papers("q")

    src500 = IEEESource(api_key="k", http_client=_http_with(_mock_response(status_code=500)))
    with pytest.raises(SourceUnavailableError):
        await src500.search_papers("q")

    src_raise = IEEESource(api_key="k", http_client=_http_with(RuntimeError("x")))
    with pytest.raises(SourceUnavailableError):
        await src_raise.search_papers("q")

    src_badjson = IEEESource(api_key="k", http_client=_http_with(_mock_response(text="x")))
    with pytest.raises(ParseError):
        await src_badjson.search_papers("q")

    src_list = IEEESource(api_key="k", http_client=_http_with(_mock_response(json_data=[1, 2])))
    with pytest.raises(ParseError):
        await src_list.search_papers("q")


@pytest.mark.asyncio
async def test_ieee_get_paper_by_doi_and_author() -> None:
    ok = _mock_response(json_data={"articles": [{"title": "T1", "doi": "10.1109/X.1"}]})
    src = IEEESource(api_key="k", http_client=_http_with(ok))
    paper = await src.get_paper_by_doi("10.1109/X.1")
    assert paper is not None and paper.title == "T1"

    src404 = IEEESource(api_key="k", http_client=_http_with(_mock_response(status_code=404)))
    papers = await src404.get_author_papers("Alice")
    assert papers == []

    src_profile = IEEESource(api_key="k", http_client=_http_with(_mock_response(json_data={"articles": [{"title": "P", "authors": {"authors": [{"full_name": "Alice Chen"}]}, "citing_paper_count": 5}]})))
    profile = await src_profile.get_author_profile("Alice Chen")
    assert profile is not None
    assert "Alice" in profile.name
    assert profile.citations == 5

    src_none = IEEESource(api_key="k", http_client=_http_with(_mock_response(json_data={"articles": []})))
    assert await src_none.get_author_profile("Nobody") is None


# ---------------------------------------------------------------------------
# Storage edge paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_storage_edge_paths(tmp_path: Path) -> None:
    store = JSONStorage(str(tmp_path / "jedge"))
    await store.connect()
    try:
        p = Paper(title="P1", authors=["Ada"], year=2020, venue="V1", keywords=["ml"], evidence=_ev())
        pid = await store.save_paper(p)
        # update / delete
        assert await store.update_paper(pid, p) is True
        assert await store.update_paper("missing", p) is False
        # citation directions
        c = Citation(citing_paper_id=pid, cited_paper_id="other", evidence=_ev())
        await store.save_citation(c)
        incoming = await store.get_citations_by_paper("other", direction="incoming")
        assert len(incoming) == 1
        # query filters
        p2 = Paper(title="P2", authors=["Bob"], year=2021, venue="V2", abstract="abstract text", evidence=_ev())
        await store.save_paper(p2)
        assert len(await store.query_papers(year_from=2021)) == 1
        assert len(await store.query_papers(year_to=2020)) == 1
        assert len(await store.query_papers(venue="V2")) == 1
        assert len(await store.query_papers(keyword="abstract")) == 1
        assert len(await store.query_papers(author="Bob")) == 1
        # authors update/delete + interest query
        a = Author(name="Ada", interests=["ml", "dl"], evidence=_ev())
        aid = await store.save_author(a)
        assert await store.update_author(aid, a) is True
        assert await store.update_author("missing", a) is False
        assert len(await store.query_authors(interest="dl")) == 1
        assert len(await store.query_authors(name="Ada", affiliation="X")) == 0
        assert await store.delete_author(aid) is True
        assert await store.delete_author("missing") is False
        # hashes + update times
        await store.save_paper_hash(pid, "h" * 16)
        assert await store.get_paper_hash(pid) == "h" * 16
        await store.save_last_update_time("openalex", datetime(2026, 7, 1, tzinfo=timezone.utc))
        assert await store.get_last_update_time("openalex") is not None
        assert await store.get_last_update_time("never") is None
        assert await store.delete_paper(pid) is True
        assert await store.delete_paper(pid) is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_storage_edge_paths(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "sedge.db"))
    await store.connect()
    try:
        p = Paper(title="S1", authors=["Ada"], year=2020, evidence=_ev())
        pid = await store.save_paper(p)
        assert await store.update_paper("missing", p) is False
        assert await store.delete_paper("missing") is False
        c = Citation(citing_paper_id=pid, cited_paper_id="other", evidence=_ev())
        await store.save_citation(c)
        assert len(await store.get_citations_by_paper("other", direction="incoming")) == 1
        # author upsert + delete
        a = Author(name="Ada", interests=["ml"], evidence=_ev())
        aid = await store.save_author(a)
        a2 = Author(id=aid, name="Ada Lovelace", interests=["ml", "dl"], evidence=_ev())
        assert await store.save_author(a2) == aid  # upsert same id
        assert await store.update_author(aid, a2) is True
        assert await store.update_author("missing", a2) is False
        assert len(await store.query_authors(interest="dl")) == 1
        assert len(await store.query_authors(interest="zzz")) == 0
        assert await store.delete_author(aid) is True
        assert await store.delete_author(aid) is False
        # hash upsert
        await store.save_paper_hash(pid, "abcd1234efgh5678")
        await store.save_paper_hash(pid, "dcba4321hgfe8765")
        assert await store.get_paper_hash(pid) == "dcba4321hgfe8765"
        # batch
        ids = await store.save_batch(
            papers=[Paper(title="B1", authors=["X"], evidence=_ev())],
            authors=[Author(name="X", evidence=_ev())],
            citations=[Citation(citing_paper_id="a", cited_paper_id="b", evidence=_ev())],
        )
        assert ids["papers"] and ids["authors"] and ids["citations"]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Facade (offline cassettes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facade_connect_collect_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_cassette(monkeypatch, "openalex_search")
    from academic_intelligence import AcademicIntelligence

    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="sqlite",
            storage_path=str(tmp_path / "facade.db"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        result = await ai.collect_paper(
            "machine learning", sources=["openalex"], persist=True, limit=5
        )
        assert len(result.papers) > 0
        stats = await ai.get_stats()
        assert stats["total_papers"] >= 1
        found = await ai.query_papers(author="Hinton", limit=10)
        assert isinstance(found, list)
        authors = await ai.query_authors(name="Hinton", limit=10)
        assert isinstance(authors, list)
        # DOI lookup path
        by_doi = await ai.collect_paper("10.1038/nature14539", sources=["openalex"])
        assert len(by_doi.papers) >= 1
        # sources=None resolves to all registered sources
        all_res = await ai.collect_paper("attention is all you need", limit=3)
        assert len(all_res.papers) > 0
        # "*" resolves to all registered sources too
        star_res = await ai.collect_paper("attention is all you need", sources=["*"], limit=3)
        assert len(star_res.papers) > 0
    finally:
        await ai.close()
    # close() twice is safe
    await ai.close()


@pytest.mark.asyncio
async def test_collect_paper_persist_twice_same_doi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C-1: persisting the same DOI twice must not crash or duplicate rows.

    ``collect_paper(..., persist=True)`` twice for the same DOI previously
    crashed on the second persist (bare INSERT hit the UNIQUE constraint);
    it must now update the existing row so the papers table keeps one row.
    """
    install_cassette(monkeypatch, "openalex_search")
    from academic_intelligence import AcademicIntelligence

    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="sqlite",
            storage_path=str(tmp_path / "twice.db"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        r1 = await ai.collect_paper(
            "10.1038/nature14539", sources=["openalex"], persist=True
        )
        assert len(r1.papers) >= 1
        assert (await ai.get_stats())["total_papers"] == 1

        # second persist of the same DOI must succeed without duplicating
        r2 = await ai.collect_paper(
            "10.1038/nature14539", sources=["openalex"], persist=True
        )
        assert len(r2.papers) >= 1
        stats = await ai.get_stats()
        assert stats["total_papers"] == 1
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_facade_no_sources_raises(tmp_path: Path) -> None:
    from academic_intelligence import AcademicIntelligence

    ai = AcademicIntelligence(
        Config(sources=["unknown_source"], storage_type="json", storage_path=str(tmp_path / "empty"))
    )
    await ai.connect()
    try:
        with pytest.raises(CollectorError):
            await ai.collect_paper("q", sources=[])
        with pytest.raises(CollectorError):
            await ai.collect_paper("q")  # no registered sources
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_facade_update_paper_and_author(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_cassette(monkeypatch, "openalex_search")
    from academic_intelligence import AcademicIntelligence

    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "facade2"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        stored = Paper(
            id="p-seed",
            title="Deep learning",
            authors=["Hinton"],
            year=2015,
            doi="10.1038/nature14539",
            evidence=_ev(SourceType.OPENALEX, 0.8),
        )
        await ai.storage.save_paper(stored)
        result = await ai.update_paper("p-seed", sources=["openalex"])
        assert result.total_checked >= 0

        result2 = await ai.update_author_papers("Geoffrey Hinton", sources=["openalex"])
        assert isinstance(result2.total_checked, int)
    finally:
        await ai.close()


# ---------------------------------------------------------------------------
# Collector orchestration
# ---------------------------------------------------------------------------


class _FakeSource(BaseSource):
    name = "fake"
    source_type = SourceType.OPENALEX
    capabilities = {
        **BaseSource.capabilities,
        # C1 fail-closed dispatch: declare the author/citation ops it has.
        "get_author_papers": True,
        "get_author_profile": True,
        "get_citations": True,
    }

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        if self.fail:
            raise SourceUnavailableError("boom", source_name=self.name)
        return [Paper(title=query, authors=["A"], evidence=_ev())]

    async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
        return Paper(title=doi, authors=["A"], evidence=_ev())

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        return [Paper(title=author_name, authors=["A"], evidence=_ev())]

    async def get_author_profile(self, author_name: str) -> Optional[Author]:
        return Author(name=author_name, evidence=_ev())

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return [Citation(citing_paper_id="c1", cited_paper_id=paper_id, evidence=_ev())]

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_collector_methods() -> None:
    from academic_intelligence.collectors.base import MultiSourceCollector

    collector = MultiSourceCollector(config=Config(), sources=[_FakeSource()])
    r1 = await collector.collect("hello")
    assert len(r1.papers) == 1
    assert r1.stats["paper_count"] == 1

    r2 = await collector.collect_paper("10.1234/x.y")
    assert len(r2.papers) == 1

    r3 = await collector.collect_paper("free text query")
    assert len(r3.papers) == 1

    r4 = await collector.collect_citations("p1")
    assert len(r4.citations) == 1

    r5 = await collector.collect_author_papers("Ada")
    assert r5.papers and r5.authors


@pytest.mark.asyncio
async def test_collector_all_sources_failed() -> None:
    from academic_intelligence.collectors.base import MultiSourceCollector

    collector = MultiSourceCollector(config=Config(), sources=[_FakeSource(fail=True)])
    with pytest.raises(AllSourcesFailedError):
        await collector.collect("q")
    stats = collector.get_stats()
    assert stats["requests_failed"] >= 1
    collector.reset_stats()
    assert collector.get_stats()["requests_total"] == 0


@pytest.mark.asyncio
async def test_collector_source_missing_method() -> None:
    from academic_intelligence.collectors.base import MultiSourceCollector

    class _NoPapers(_FakeSource):
        async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:  # type: ignore[override]
            raise NotImplementedError

    collector = MultiSourceCollector(config=Config(), sources=[_NoPapers()])
    with pytest.raises(AllSourcesFailedError):
        await collector.collect("q")


def test_base_collector_context_and_stats() -> None:
    from academic_intelligence.collectors.base import BaseCollector

    class _C(BaseCollector):
        async def collect(self, query: str, **kwargs: Any) -> Any:
            return None

    c = _C(config={"min_confidence": 0.1})
    with c as active:
        assert active is c
    c._update_stats(True, items=3)
    assert c.get_stats()["items_collected"] == 3
    c._register_source(_FakeSource())
    assert len(c._sources) == 1


# ---------------------------------------------------------------------------
# Enricher strategies
# ---------------------------------------------------------------------------


def _paper(**kw: Any) -> Paper:
    defaults = {"title": "T", "evidence": _ev()}
    defaults.update(kw)
    return Paper(**defaults)


def test_enricher_strategies() -> None:
    enr = Enricher()
    # venue normalization
    vp = _paper(venue="  Some Journal , 2020 ")
    out = VenueNormalizationStrategy()(vp)
    assert out.venue == "Some Journal"
    # doi extraction from url
    dp = _paper(url="https://doi.org/10.1234/abc.def")
    assert DoiExtractionStrategy()(dp).doi == "10.1234/abc.def"
    # pdf promotion
    assert PdfFromUrlStrategy()(_paper(url="https://x.com/a.pdf")).pdf_url == "https://x.com/a.pdf"
    arxiv_paper = _paper(url="https://arxiv.org/abs/1706.03762")
    assert PdfFromUrlStrategy()(arxiv_paper).pdf_url == "https://arxiv.org/pdf/1706.03762.pdf"
    # title normalization
    tp = _paper(title="  Hello   World ")
    assert TitleNormalizeStrategy()(tp).title == "Hello World"
    # author normalization drops duplicates
    ap = _paper(authors=[" Ada  Lovelace ", "Ada Lovelace"])
    cleaned = AuthorListNormalizeStrategy()(ap)
    assert [a.name for a in cleaned.authors] == ["Ada Lovelace"]
    # no-op strategies
    assert CitationCountEnrichmentStrategy()(_paper()).citations is None
    assert AffiliationEnrichmentStrategy()(_paper()).title == "T"


def test_enricher_register_unregister_and_pipeline() -> None:
    enr = Enricher()
    strat = VenueNormalizationStrategy(priority=1)
    enr.register(strat)
    assert any(s.name == "venue_normalize" for s in enr.strategies)
    enr.unregister("venue_normalize")
    assert not any(s.name == "venue_normalize" for s in enr.strategies)

    papers = enr.enrich_papers([_paper(title="  Spaced  ", venue="J, 2021")])
    assert papers[0].title == "Spaced"

    authors = enr.enrich_authors([Author(name="  Ada  Lovelace ", evidence=_ev())])
    assert authors[0].name == "Ada Lovelace"

    cites = enr.enrich_citations([Citation(citing_paper_id="a", cited_paper_id="b", evidence=_ev())])
    assert len(cites) == 1


def test_enricher_strategy_failure_continues() -> None:
    from academic_intelligence.processors.enricher import BaseEnrichmentStrategy

    enr = Enricher()

    class _BadStrategy(BaseEnrichmentStrategy):
        def __init__(self) -> None:
            super().__init__(name="bad")

        def enrich(self, paper: Paper) -> Paper:
            raise EnrichmentError("bad", enrichment_step="test")

    enr.register(_BadStrategy())
    out = enr.enrich_papers([_paper(title="Keep")])
    assert out[0].title == "Keep"


# ---------------------------------------------------------------------------
# Validator branches
# ---------------------------------------------------------------------------


def test_validator_paper_error_branches() -> None:
    v = Validator(ValidatorConfig(require_doi=True, allowed_sources={"openalex"}, min_confidence=0.9))
    no_doi = Paper(title="X", evidence=_ev(SourceType.OPENALEX, 0.5))
    r = v.validate_paper(no_doi)
    assert any("doi" in e for e in r.errors)  # require_doi violation
    assert any("confidence" in w for w in r.warnings)  # below min threshold
    assert any("authors" in w for w in r.warnings)  # no authors listed

    wrong_src = Paper(title="X", authors=["A"], evidence=_ev(SourceType.ARXIV))
    r3 = v.validate_paper(wrong_src)
    assert any("allowed" in e for e in r3.errors)

    ok = Paper(
        title="X",
        authors=["A"],
        doi="10.1234/abc.def",
        evidence=_ev(SourceType.OPENALEX, 0.95),
    )
    assert v.validate_paper(ok).is_valid

    # filter + raise_on_invalid paths
    filtered = v.filter_valid_papers([no_doi, ok])
    assert len(filtered) == 1
    from academic_intelligence.core.exceptions import DataValidationError

    with pytest.raises(DataValidationError):
        v.validate_papers([no_doi], raise_on_invalid=True)


def test_validator_author_and_citation_branches() -> None:
    v = Validator(ValidatorConfig(strict_email=True))
    author = Author(name="Ada Lovelace", email=None, evidence=_ev())
    assert v.validate_author(author).is_valid
    rich = Author(
        name="Ada",
        homepage="https://example.com",
        profile_url="https://example.com/ada",
        h_index=5,
        citations=10,
        evidence=_ev(),
    )
    assert v.validate_author(rich).is_valid

    c1 = Citation(citing_paper_id="a", cited_paper_id="b", evidence=_ev(SourceType.OPENALEX, 0.1))
    assert v.validate_citation(c1).is_valid

    v2 = Validator()
    v2.config.allowed_sources = {"openalex"}
    c2 = Citation(citing_paper_id="a", cited_paper_id="b", evidence=_ev(SourceType.ARXIV))
    assert any("allowed" in e for e in v2.validate_citation(c2).errors)
