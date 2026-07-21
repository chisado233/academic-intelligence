"""PubMed data source adapter.

Uses NCBI E-utilities:
https://eutils.ncbi.nlm.nih.gov/entrez/eutils/

Two-step flow: esearch (IDs) → efetch (article details as XML).
Optional NCBI API key raises the rate ceiling from ~3 to ~10 req/s.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from academic_intelligence.core.exceptions import (
    ParseError,
    RateLimitError,
    SourceUnavailableError,
)
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import AntiCrawlStrategy, SourceType
from academic_intelligence.sources.base import BaseSource
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


def _text(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    # Join all nested text (e.g. AbstractText with tags)
    parts = [t for t in el.itertext() if t and t.strip()]
    return " ".join(" ".join(parts).split())


def _safe_doi(doi: Optional[str], title: str, evidence: Evidence) -> Optional[str]:
    if not doi:
        return None
    try:
        Paper.model_validate({"title": title or "untitled", "doi": doi, "evidence": evidence})
        return doi
    except Exception:
        return None


class PubMedSource(BaseSource):
    """PubMed / NCBI E-utilities source."""

    name = "pubmed"
    source_type = SourceType.PUBMED

    def __init__(
        self,
        http_client: Optional[HTTPClient] = None,
        *,
        api_key: Optional[str] = None,
        tool: str = "academic_intelligence",
        email: Optional[str] = None,
        confidence: float = 0.9,
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

    def _common_params(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {"tool": self.tool}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _evidence(self, url: str, raw: Optional[Dict[str, Any]] = None) -> Evidence:
        return Evidence(
            source=self.source_type,
            source_url=url,
            collected_at=datetime.now(timezone.utc),
            confidence=self.confidence,
            raw_data=raw,
        )

    async def _get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        expect_json: bool = False,
    ) -> Any:
        client = await self._client()
        url = f"{_EUTILS_BASE}/{endpoint}"
        query = {**self._common_params(), **(params or {})}
        try:
            response = await client.get(url, params=query)
        except Exception as exc:
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

    async def _esearch(self, term: str, *, retmax: int = 10) -> List[str]:
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

    def _parse_article(self, article: ET.Element) -> Optional[Paper]:
        medline = article.find("MedlineCitation")
        if medline is None:
            return None

        pmid_el = medline.find("PMID")
        pmid = _text(pmid_el) or None

        article_el = medline.find("Article")
        if article_el is None:
            return None

        title = _text(article_el.find("ArticleTitle"))
        if not title:
            return None

        # Abstract: may be multiple AbstractText nodes
        abstract_parts: List[str] = []
        abstract_el = article_el.find("Abstract")
        if abstract_el is not None:
            for abs_text in abstract_el.findall("AbstractText"):
                label = abs_text.get("Label")
                body = _text(abs_text)
                if body:
                    abstract_parts.append(f"{label}: {body}" if label else body)
        abstract = " ".join(abstract_parts) if abstract_parts else None

        authors: List[str] = []
        author_list = article_el.find("AuthorList")
        if author_list is not None:
            for author in author_list.findall("Author"):
                last = _text(author.find("LastName"))
                fore = _text(author.find("ForeName")) or _text(author.find("Initials"))
                collective = _text(author.find("CollectiveName"))
                if last and fore:
                    authors.append(f"{fore} {last}")
                elif last:
                    authors.append(last)
                elif collective:
                    authors.append(collective)

        # Year from Journal Issue / PubDate
        year: Optional[int] = None
        journal = article_el.find("Journal")
        venue: Optional[str] = None
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
        doi: Optional[str] = None
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

        keywords: List[str] = []
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
        evidence = self._evidence(url, raw={"pmid": pmid, "mesh": keywords[:20]})
        safe_doi = _safe_doi(doi, title, evidence)

        return Paper(
            id=pmid,
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            abstract=abstract,
            doi=safe_doi,
            url=url if url.startswith("http") else None,
            pdf_url=None,
            citations=None,
            keywords=keywords[:30],
            evidence=evidence,
        )

    def _parse_efetch_xml(self, xml_text: str) -> List[Paper]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ParseError(
                f"Invalid XML from PubMed efetch: {exc}",
                source_name=self.name,
                raw_snippet=xml_text[:300],
            ) from exc

        papers: List[Paper] = []
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

    async def _fetch_by_ids(self, ids: List[str]) -> List[Paper]:
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

    async def search_papers(self, query: str, limit: int = 10) -> List[Paper]:
        """Search PubMed (supports free text and MeSH-style terms)."""
        q = query.strip()
        if not q:
            return []
        ids = await self._esearch(q, retmax=limit)
        papers = await self._fetch_by_ids(ids)
        return papers[:limit]

    async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
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

    async def get_author_papers(self, author_name: str) -> List[Paper]:
        """Search papers by author name using the Author field tag."""
        name = author_name.strip()
        if not name:
            return []
        # NCBI Author field: "Last FM" or free form with [Author]
        ids = await self._esearch(f"{name}[Author]", retmax=50)
        return await self._fetch_by_ids(ids)

    async def get_author_profile(self, author_name: str) -> Optional[Author]:
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
        interests: List[str] = []
        for paper in papers:
            for a in paper.authors:
                if a.lower() == name.lower() or name.lower() in a.lower():
                    matched_name = a
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
            evidence=self._evidence(profile_url, raw={"paper_count": len(papers)}),
        )

    async def get_citations(self, paper_id: str) -> List[Citation]:
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

        citations: List[Citation] = []
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
