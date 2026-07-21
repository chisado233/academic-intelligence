"""Unit tests for arXiv, PubMed, and IEEE source plugins (mocked HTTP)."""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_intelligence.core.exceptions import AuthenticationError, RateLimitError
from academic_intelligence.core.types import SourceType
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.ieee import IEEESource
from academic_intelligence.sources.pubmed import PubMedSource

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SAMPLE_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query: search_query=all:transformer</title>
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <updated>2023-08-02T00:00:00Z</updated>
    <published>2017-06-12T00:00:00Z</published>
    <title>Attention Is All You Need</title>
    <summary>The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.</summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <author><name>Niki Parmar</name></author>
    <link href="http://arxiv.org/abs/1706.03762v7" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/1706.03762v7" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/1810.04805v2</id>
    <published>2018-10-11T00:00:00Z</published>
    <title>BERT: Pre-training of Deep Bidirectional Transformers</title>
    <summary>We introduce a new language representation model called BERT.</summary>
    <author><name>Jacob Devlin</name></author>
    <author><name>Ming-Wei Chang</name></author>
    <link href="http://arxiv.org/abs/1810.04805v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/1810.04805v2" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
"""

SAMPLE_PUBMED_ESEARCH = {
    "esearchresult": {
        "count": "2",
        "retmax": "2",
        "idlist": ["12345678", "87654321"],
    }
}

SAMPLE_PUBMED_EFETCH = """<?xml version="1.0" ?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2024//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_240101.dtd">
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">12345678</PMID>
      <Article PubModel="Print">
        <Journal>
          <Title>Nature</Title>
          <ISOAbbreviation>Nature</ISOAbbreviation>
          <JournalIssue CitedMedium="Print">
            <PubDate>
              <Year>2020</Year>
              <Month>Jan</Month>
            </PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Deep learning for medical imaging</ArticleTitle>
        <Abstract>
          <AbstractText>This paper reviews deep learning applications in medical imaging.</AbstractText>
        </Abstract>
        <AuthorList CompleteYN="Y">
          <Author ValidYN="Y">
            <LastName>Smith</LastName>
            <ForeName>Jane</ForeName>
            <Initials>J</Initials>
          </Author>
          <Author ValidYN="Y">
            <LastName>Doe</LastName>
            <ForeName>John</ForeName>
            <Initials>J</Initials>
          </Author>
        </AuthorList>
        <ELocationID EIdType="doi" ValidYN="Y">10.1038/s41586-020-0001-1</ELocationID>
      </Article>
      <MeshHeadingList>
        <MeshHeading>
          <DescriptorName MajorTopicYN="Y">Deep Learning</DescriptorName>
        </MeshHeading>
        <MeshHeading>
          <DescriptorName MajorTopicYN="N">Diagnostic Imaging</DescriptorName>
        </MeshHeading>
      </MeshHeadingList>
      <KeywordList Owner="NOTNLM">
        <Keyword MajorTopicYN="N">AI</Keyword>
      </KeywordList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">12345678</ArticleId>
        <ArticleId IdType="doi">10.1038/s41586-020-0001-1</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">87654321</PMID>
      <Article>
        <Journal>
          <Title>Lancet</Title>
          <JournalIssue>
            <PubDate>
              <Year>2019</Year>
            </PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Another medical AI paper</ArticleTitle>
        <AuthorList>
          <Author>
            <LastName>Smith</LastName>
            <ForeName>Jane</ForeName>
          </Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">87654321</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

SAMPLE_IEEE_SEARCH: Dict[str, Any] = {
    "total_records": 2,
    "total_searched": 1000,
    "articles": [
        {
            "doi": "10.1109/TPAMI.2020.1234567",
            "title": "A Novel Vision Transformer for Image Classification",
            "publisher": "IEEE",
            "publication_title": "IEEE Transactions on Pattern Analysis and Machine Intelligence",
            "publication_year": "2021",
            "article_number": "9321234",
            "abstract": "We propose a novel vision transformer architecture.",
            "authors": {
                "authors": [
                    {"full_name": "Alice Chen", "author_order": 1},
                    {"full_name": "Bob Wilson", "author_order": 2},
                ]
            },
            "index_terms": {
                "ieee_terms": {"terms": ["Transformers", "Computer vision"]},
                "author_terms": {"terms": ["deep learning"]},
            },
            "citing_paper_count": 42,
            "html_url": "https://ieeexplore.ieee.org/document/9321234/",
            "pdf_url": "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=9321234",
        },
        {
            "doi": "10.1109/CVPR.2019.00001",
            "title": "Another IEEE Paper",
            "publication_year": "2019",
            "article_number": "1000001",
            "authors": {"authors": [{"full_name": "Alice Chen"}]},
            "abstract": "Secondary result.",
        },
    ],
}


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


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arxiv_search_papers_parses_atom() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(text=SAMPLE_ARXIV_ATOM))
    source = ArxivSource(http_client=http, min_interval_seconds=0.01)

    papers = await source.search_papers("transformer", limit=10)

    assert len(papers) == 2
    assert papers[0].title == "Attention Is All You Need"
    assert "Ashish Vaswani" in papers[0].authors
    assert papers[0].year == 2017
    assert papers[0].id == "1706.03762v7"
    assert papers[0].url and "arxiv.org/abs" in papers[0].url
    assert papers[0].pdf_url and "pdf" in papers[0].pdf_url
    assert "cs.CL" in papers[0].keywords
    assert papers[0].evidence.source == SourceType.ARXIV
    assert papers[1].title.startswith("BERT")
    http.get.assert_awaited()


@pytest.mark.asyncio
async def test_arxiv_get_paper_by_doi() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(text=SAMPLE_ARXIV_ATOM))
    source = ArxivSource(http_client=http, min_interval_seconds=0.01)

    paper = await source.get_paper_by_doi("10.48550/arXiv.1706.03762")
    assert paper is not None
    assert paper.title == "Attention Is All You Need"


@pytest.mark.asyncio
async def test_arxiv_get_author_papers_and_profile() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(text=SAMPLE_ARXIV_ATOM))
    source = ArxivSource(http_client=http, min_interval_seconds=0.01)

    papers = await source.get_author_papers("Vaswani")
    assert len(papers) >= 1

    profile = await source.get_author_profile("Ashish Vaswani")
    assert profile is not None
    assert "Vaswani" in profile.name
    assert profile.evidence.source == SourceType.ARXIV


@pytest.mark.asyncio
async def test_arxiv_rate_limit_error() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(status_code=429, text="slow down"))
    source = ArxivSource(http_client=http, min_interval_seconds=0.01)

    with pytest.raises(RateLimitError):
        await source.search_papers("test")


@pytest.mark.asyncio
async def test_arxiv_empty_query() -> None:
    source = ArxivSource(http_client=MagicMock(), min_interval_seconds=0.01)
    assert await source.search_papers("  ") == []


@pytest.mark.asyncio
async def test_arxiv_get_citations_empty() -> None:
    source = ArxivSource(http_client=MagicMock(), min_interval_seconds=0.01)
    assert await source.get_citations("1706.03762") == []


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pubmed_search_papers_two_step() -> None:
    http = MagicMock()

    async def _get(url: str, **kwargs: Any) -> MagicMock:
        if "esearch" in url:
            return _mock_response(json_data=SAMPLE_PUBMED_ESEARCH)
        if "efetch" in url:
            return _mock_response(text=SAMPLE_PUBMED_EFETCH)
        return _mock_response(status_code=404, text="not found")

    http.get = AsyncMock(side_effect=_get)
    source = PubMedSource(http_client=http)

    papers = await source.search_papers("deep learning", limit=10)

    assert len(papers) == 2
    assert papers[0].id == "12345678"
    assert papers[0].title == "Deep learning for medical imaging"
    assert "Jane Smith" in papers[0].authors
    assert papers[0].year == 2020
    assert papers[0].venue == "Nature"
    assert papers[0].doi == "10.1038/s41586-020-0001-1"
    assert "Deep Learning" in papers[0].keywords
    assert papers[0].evidence.source == SourceType.PUBMED
    assert papers[0].url and "pubmed" in papers[0].url


@pytest.mark.asyncio
async def test_pubmed_get_paper_by_doi() -> None:
    http = MagicMock()

    async def _get(url: str, **kwargs: Any) -> MagicMock:
        if "esearch" in url:
            return _mock_response(json_data=SAMPLE_PUBMED_ESEARCH)
        return _mock_response(text=SAMPLE_PUBMED_EFETCH)

    http.get = AsyncMock(side_effect=_get)
    source = PubMedSource(http_client=http)

    paper = await source.get_paper_by_doi("https://doi.org/10.1038/s41586-020-0001-1")
    assert paper is not None
    assert paper.doi == "10.1038/s41586-020-0001-1"


@pytest.mark.asyncio
async def test_pubmed_get_author_papers() -> None:
    http = MagicMock()

    async def _get(url: str, **kwargs: Any) -> MagicMock:
        if "esearch" in url:
            return _mock_response(json_data=SAMPLE_PUBMED_ESEARCH)
        return _mock_response(text=SAMPLE_PUBMED_EFETCH)

    http.get = AsyncMock(side_effect=_get)
    source = PubMedSource(http_client=http, api_key="test-key")

    papers = await source.get_author_papers("Jane Smith")
    assert len(papers) == 2
    # Ensure API key is threaded into params
    call_kwargs = http.get.await_args_list[0].kwargs
    assert call_kwargs["params"].get("api_key") == "test-key"


@pytest.mark.asyncio
async def test_pubmed_author_profile() -> None:
    http = MagicMock()

    async def _get(url: str, **kwargs: Any) -> MagicMock:
        if "esearch" in url:
            return _mock_response(json_data=SAMPLE_PUBMED_ESEARCH)
        return _mock_response(text=SAMPLE_PUBMED_EFETCH)

    http.get = AsyncMock(side_effect=_get)
    source = PubMedSource(http_client=http)
    profile = await source.get_author_profile("Jane Smith")
    assert profile is not None
    assert profile.evidence.source == SourceType.PUBMED


@pytest.mark.asyncio
async def test_pubmed_rate_limit() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(status_code=429, text="too many"))
    source = PubMedSource(http_client=http)
    with pytest.raises(RateLimitError):
        await source.search_papers("cancer")


# ---------------------------------------------------------------------------
# IEEE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ieee_requires_api_key() -> None:
    source = IEEESource(http_client=MagicMock(), api_key=None)
    # Ensure env does not leak a key
    source.api_key = None
    with pytest.raises(AuthenticationError):
        await source.search_papers("transformer")


@pytest.mark.asyncio
async def test_ieee_search_papers() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=SAMPLE_IEEE_SEARCH))
    source = IEEESource(http_client=http, api_key="fake-ieee-key")

    papers = await source.search_papers("vision transformer", limit=10)

    assert len(papers) == 2
    assert papers[0].title.startswith("A Novel Vision Transformer")
    assert "Alice Chen" in papers[0].authors
    assert papers[0].year == 2021
    assert papers[0].doi == "10.1109/TPAMI.2020.1234567"
    assert papers[0].citations == 42
    assert "Transformers" in papers[0].keywords
    assert papers[0].evidence.source == SourceType.IEEE
    assert papers[0].id == "9321234"

    # apikey must be present
    params = http.get.await_args.kwargs["params"]
    assert params["apikey"] == "fake-ieee-key"
    assert params["querytext"] == "vision transformer"


@pytest.mark.asyncio
async def test_ieee_get_paper_by_doi() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=SAMPLE_IEEE_SEARCH))
    source = IEEESource(http_client=http, api_key="k")

    paper = await source.get_paper_by_doi("10.1109/TPAMI.2020.1234567")
    assert paper is not None
    assert paper.doi == "10.1109/TPAMI.2020.1234567"


@pytest.mark.asyncio
async def test_ieee_get_author_papers_and_profile() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(json_data=SAMPLE_IEEE_SEARCH))
    source = IEEESource(http_client=http, api_key="k")

    papers = await source.get_author_papers("Alice Chen")
    assert len(papers) == 2

    profile = await source.get_author_profile("Alice Chen")
    assert profile is not None
    assert "Alice" in profile.name
    assert profile.citations == 42  # only first paper has citations


@pytest.mark.asyncio
async def test_ieee_auth_failure() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(status_code=403, text="forbidden"))
    source = IEEESource(http_client=http, api_key="bad")
    with pytest.raises(AuthenticationError):
        await source.search_papers("x")


@pytest.mark.asyncio
async def test_ieee_rate_limit() -> None:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(status_code=429, text="slow"))
    source = IEEESource(http_client=http, api_key="k")
    with pytest.raises(RateLimitError):
        await source.search_papers("x")


# ---------------------------------------------------------------------------
# Package exports
# ---------------------------------------------------------------------------


def test_sources_package_exports() -> None:
    from academic_intelligence.sources import (
        ArxivSource as A,
        IEEESource as I,
        PubMedSource as P,
    )

    assert A is ArxivSource
    assert P is PubMedSource
    assert I is IEEESource
    assert A.name == "arxiv"
    assert P.name == "pubmed"
    assert I.name == "ieee"
