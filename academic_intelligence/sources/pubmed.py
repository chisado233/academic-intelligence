"""PubMed data source adapter.

Uses NCBI E-utilities:
https://eutils.ncbi.nlm.nih.gov/entrez/eutils/

Two-step flow: esearch (IDs) → efetch (article details as XML).
Optional NCBI API key raises the rate ceiling from ~3 to ~10 req/s.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

import httpx

from academic_intelligence.core.exceptions import (
    ParseError,
    RateLimitError,
    SourceUnavailableError,
    TimeoutError,
)
from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    Citation,
    Evidence,
    Paper,
    normalize_doi,
    normalize_pmid,
)
from academic_intelligence.core.types import AntiCrawlStrategy, SourceType
from academic_intelligence.sources.base import (
    BaseSource,
    is_rate_limit_status,
    retry_after_from_error,
)
from academic_intelligence.utils.http import HTTPClient
from academic_intelligence.utils.rate_limiter import create_rate_limiter

logger = logging.getLogger(__name__)

_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")


def _normalize_doi(doi: str) -> str:
    cleaned = doi.strip()
    for prefix in _DOI_PREFIXES:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    # Join all nested text (e.g. AbstractText with tags)
    parts = [t for t in el.itertext() if t and t.strip()]
    return " ".join(" ".join(parts).split())


def _safe_doi(doi: str | None, title: str, evidence: Evidence) -> str | None:
    # (FIX-AB-3) Validate the DOI with the lightweight field-level helper
    # instead of constructing a whole ``Paper`` just to run its validator:
    # the per-article ``Paper.model_validate`` calls dominated the parse hot
    # path (measured ~6 pydantic validations per article, two of them only
    # for the DOI/PMID guards).  ``title`` / ``evidence`` are kept for
    # signature stability — the old full-model guard validated only the DOI.
    return normalize_doi(doi)


def _safe_pmid(pmid: str | None) -> str | None:
    """Return *pmid* when it satisfies the NCBI 1-8 digit spec, else None.

    (FIX-S S2) The model now rejects malformed PMIDs, so the parser softens
    an invalid value instead of letting the whole article fail to parse.
    (FIX-AB-3) Validated with the shared field-level helper instead of a
    full ``Paper.model_validate``.
    """
    return normalize_pmid(pmid)


class PubMedSource(BaseSource):
    """PubMed / NCBI E-utilities source."""

    name = "pubmed"
    source_type = SourceType.PUBMED
    capabilities = {
        **BaseSource.capabilities,
        # C1 revision: PubMed implements author lookups and elink citations.
        "citations": True,
        "get_author_papers": True,
        "get_author_profile": True,
        "get_citations": True,
    }

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        api_key: str | None = None,
        tool: str = "academic_intelligence",
        email: str | None = None,
        confidence: float = 0.92,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self.api_key = api_key
        self.tool = tool
        self.email = email
        self.confidence = confidence
        # NCBI: 3 req/s without key, 10 req/s with key
        self._requests_per_second = 10.0 if api_key else 3.0

    async def _client(self) -> HTTPClient:
        if self._http is None:
            base_delay = 1.0 / self._requests_per_second
            strategy = AntiCrawlStrategy(base_delay=base_delay, adaptive_delay=False)
            self._http = HTTPClient(
                strategy=strategy,
                rate_limiter=create_rate_limiter(
                    "fixed", requests_per_second=self._requests_per_second
                ),
                enable_cache=True,
            )
            await self._http.connect()
        return self._http

    async def close(self) -> None:
        """Close owned HTTP client."""
        if self._owns_client and self._http is not None:
            await self._http.close()
            self._http = None

    def _common_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"tool": self.tool}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _evidence(
        self,
        url: str,
        raw: dict[str, Any] | None = None,
        source_id: str | None = None,
    ) -> Evidence:
        return Evidence(
            source=self.source_type,
            source_id=source_id,
            source_url=url,
            collected_at=datetime.now(UTC),
            confidence=self.confidence,
            raw_data=raw,
        )

    async def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        expect_json: bool = False,
    ) -> Any:
        client = await self._client()
        url = f"{_EUTILS_BASE}/{endpoint}"
        query = {**self._common_params(), **(params or {})}
        try:
            response = await client.get(url, params=query)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"PubMed request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    "PubMed / NCBI rate limit exceeded",
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"PubMed request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "PubMed / NCBI rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"PubMed HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )

        if expect_json:
            try:
                return response.json()
            except Exception as exc:
                raise ParseError(
                    f"Invalid JSON from PubMed: {exc}",
                    source_name=self.name,
                    raw_snippet=response.text[:300],
                ) from exc
        return response.text

    async def _esearch(self, term: str, *, retmax: int = 10) -> list[str]:
        data = await self._get(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": term,
                "retmax": min(max(retmax, 1), 200),
                "retmode": "json",
                "sort": "relevance",
            },
            expect_json=True,
        )
        if not data or not isinstance(data, dict):
            return []
        result = data.get("esearchresult") or data.get("esearchResult") or {}
        idlist = result.get("idlist") or result.get("idList") or []
        return [str(i) for i in idlist if i]

    def _parse_article(self, article: ET.Element) -> Paper | None:
        medline = article.find("MedlineCitation")
        if medline is None:
            return None

        pmid_el = medline.find("PMID")
        # Soften a malformed PMID (FIX-S S2) so one bad value never drops
        # the whole article.
        pmid = _safe_pmid(_text(pmid_el) or None)

        article_el = medline.find("Article")
        if article_el is None:
            return None

        title = _text(article_el.find("ArticleTitle"))
        if not title:
            return None

        # Abstract: may be multiple AbstractText nodes
        abstract_parts: list[str] = []
        abstract_el = article_el.find("Abstract")
        if abstract_el is not None:
            for abs_text in abstract_el.findall("AbstractText"):
                label = abs_text.get("Label")
                body = _text(abs_text)
                if body:
                    abstract_parts.append(f"{label}: {body}" if label else body)
        abstract = " ".join(abstract_parts) if abstract_parts else None

        authors: list[AuthorRef] = []
        author_list = article_el.find("AuthorList")
        if author_list is not None:
            for author in author_list.findall("Author"):
                last = _text(author.find("LastName"))
                fore = _text(author.find("ForeName")) or _text(author.find("Initials"))
                collective = _text(author.find("CollectiveName"))
                if last and fore:
                    authors.append(AuthorRef(name=f"{fore} {last}", position=len(authors) + 1))
                elif last:
                    authors.append(AuthorRef(name=last, position=len(authors) + 1))
                elif collective:
                    authors.append(AuthorRef(name=collective, position=len(authors) + 1))

        # Year from Journal Issue / PubDate
        year: int | None = None
        journal = article_el.find("Journal")
        venue: str | None = None
        if journal is not None:
            venue = _text(journal.find("Title")) or _text(journal.find("ISOAbbreviation")) or None
            pub_date = journal.find("JournalIssue/PubDate")
            if pub_date is not None:
                year_txt = _text(pub_date.find("Year"))
                if year_txt.isdigit():
                    year = int(year_txt)
                else:
                    medline_date = _text(pub_date.find("MedlineDate"))
                    if medline_date and len(medline_date) >= 4 and medline_date[:4].isdigit():
                        year = int(medline_date[:4])

        # DOI from ArticleIdList (PubmedData) or ELocationID
        doi: str | None = None
        pubmed_data = article.find("PubmedData")
        if pubmed_data is not None:
            for aid in pubmed_data.findall("ArticleIdList/ArticleId"):
                if (aid.get("IdType") or "").lower() == "doi":
                    doi = _text(aid) or None
                    break
        if not doi:
            for eloc in article_el.findall("ELocationID"):
                if (eloc.get("EIdType") or "").lower() == "doi":
                    doi = _text(eloc) or None
                    break

        keywords: list[str] = []
        # MeSH headings
        mesh_list = medline.find("MeshHeadingList")
        if mesh_list is not None:
            for mesh in mesh_list.findall("MeshHeading"):
                descriptor = _text(mesh.find("DescriptorName"))
                if descriptor and descriptor not in keywords:
                    keywords.append(descriptor)
        # KeywordList
        kw_list = medline.find("KeywordList")
        if kw_list is not None:
            for kw in kw_list.findall("Keyword"):
                val = _text(kw)
                if val and val not in keywords:
                    keywords.append(val)

        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "https://pubmed.ncbi.nlm.nih.gov/"
        evidence = self._evidence(url, raw={"pmid": pmid, "mesh": keywords[:20]}, source_id=pmid)
        safe_doi = _safe_doi(doi, title, evidence)

        return Paper(
            id=pmid,
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            abstract=abstract,
            doi=safe_doi,
            pmid=pmid,
            url=url if url.startswith("http") else None,
            pdf_url=None,
            citations=None,
            keywords=keywords[:30],
            evidence_list=[evidence],
        )

    def _parse_efetch_xml(self, xml_text: str) -> list[Paper]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ParseError(
                f"Invalid XML from PubMed efetch: {exc}",
                source_name=self.name,
                raw_snippet=xml_text[:300],
            ) from exc

        papers: list[Paper] = []
        # Root is usually PubmedArticleSet
        articles = root.findall("PubmedArticle")
        if not articles and root.tag.endswith("PubmedArticle"):
            articles = [root]
        for article in articles:
            try:
                paper = self._parse_article(article)
                if paper is not None:
                    papers.append(paper)
            except Exception as exc:
                logger.debug("Skip PubMed article parse: %s", exc)
        return papers

    async def _fetch_by_ids(self, ids: list[str]) -> list[Paper]:
        if not ids:
            return []
        xml_text = await self._get(
            "efetch.fcgi",
            {
                "db": "pubmed",
                "id": ",".join(ids),
                "retmode": "xml",
                "rettype": "abstract",
            },
            expect_json=False,
        )
        return self._parse_efetch_xml(str(xml_text))

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Search PubMed (supports free text and MeSH-style terms)."""
        q = query.strip()
        if not q:
            return []
        ids = await self._esearch(q, retmax=limit)
        papers = await self._fetch_by_ids(ids)
        return papers[:limit]

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch a PubMed article by DOI."""
        cleaned = _normalize_doi(doi)
        if not cleaned:
            return None
        ids = await self._esearch(f"{cleaned}[DOI]", retmax=1)
        if not ids:
            # Fallback free-text DOI search
            ids = await self._esearch(cleaned, retmax=3)
        papers = await self._fetch_by_ids(ids)
        for paper in papers:
            if paper.doi and paper.doi.lower() == cleaned.lower():
                return paper
        return papers[0] if papers else None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Search papers by author name using the Author field tag."""
        name = author_name.strip()
        if not name:
            return []
        # NCBI Author field: "Last FM" or free form with [Author]
        ids = await self._esearch(f"{name}[Author]", retmax=50)
        return await self._fetch_by_ids(ids)

    async def get_author_profile(self, author_name: str) -> Author | None:
        """Build a lightweight author profile from PubMed search results.

        PubMed has no dedicated author profile API; interests are derived
        from MeSH terms on recent papers.
        """
        name = author_name.strip()
        if not name:
            return None
        papers = await self.get_author_papers(name)
        if not papers:
            return None

        matched_name = name
        interests: list[str] = []
        for paper in papers:
            for ref in paper.authors:
                if ref.name.lower() == name.lower() or name.lower() in ref.name.lower():
                    matched_name = ref.name
                    break
            for kw in paper.keywords:
                if kw and kw not in interests:
                    interests.append(kw)
            if len(interests) >= 15:
                break

        profile_url = (
            f"https://pubmed.ncbi.nlm.nih.gov/?term={name.replace(' ', '+')}%5BAuthor%5D"
        )
        return Author(
            id=None,
            name=matched_name,
            affiliation=None,
            email=None,
            homepage=None,
            h_index=None,
            citations=None,
            interests=interests[:15],
            profile_url=profile_url,
            evidence_list=[
                self._evidence(profile_url, raw={"paper_count": len(papers)})
            ],
        )

    async def get_citations(self, paper_id: str) -> list[Citation]:
        """Fetch papers that cite *paper_id* via elink (pubmed_pubmed_citedin).

        *paper_id* should be a PubMed PMID. Returns empty list on failure.
        """
        pmid = paper_id.strip()
        if not pmid:
            return []
        try:
            data = await self._get(
                "elink.fcgi",
                {
                    "dbfrom": "pubmed",
                    "db": "pubmed",
                    "id": pmid,
                    "linkname": "pubmed_pubmed_citedin",
                    "retmode": "json",
                },
                expect_json=True,
            )
        except Exception as exc:
            logger.debug("PubMed elink failed for %s: %s", pmid, exc)
            return []

        if not data or not isinstance(data, dict):
            return []

        citations: list[Citation] = []
        source_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        linksets = data.get("linksets") or data.get("linkSets") or []
        for linkset in linksets:
            if not isinstance(linkset, dict):
                continue
            dbs = linkset.get("linksetdbs") or linkset.get("linkSetDbs") or []
            for db in dbs:
                if not isinstance(db, dict):
                    continue
                for link in db.get("links") or []:
                    citing_id = str(link)
                    if not citing_id or citing_id == pmid:
                        continue
                    citations.append(
                        Citation(
                            citing_paper_id=citing_id,
                            cited_paper_id=pmid,
                            evidence=self._evidence(source_url, raw={"citing": citing_id}),
                        )
                    )
        return citations
