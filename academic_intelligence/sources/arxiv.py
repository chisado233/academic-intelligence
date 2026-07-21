"""arXiv data source adapter.

Uses the public arXiv API (Atom XML):
http://export.arxiv.org/api/query

Rate limit guidance: at most one request every 3 seconds.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

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

_API_BASE = "http://export.arxiv.org/api/query"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
# arXiv ID patterns: YYMM.number[vN] or archive/YYMMNNN
_ARXIV_ID_RE = re.compile(
    r"(?:arXiv:)?((?:\d{4}\.\d{4,5})(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?",
    re.IGNORECASE,
)
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")


def _normalize_doi(doi: str) -> str:
    cleaned = doi.strip()
    for prefix in _DOI_PREFIXES:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def _text(el: Optional[ET.Element]) -> str:
    if el is None or el.text is None:
        return ""
    return " ".join(el.text.split())


def _safe_doi(doi: Optional[str], title: str, evidence: Evidence) -> Optional[str]:
    if not doi:
        return None
    try:
        Paper.model_validate({"title": title or "untitled", "doi": doi, "evidence": evidence})
        return doi
    except Exception:
        return None


class ArxivSource(BaseSource):
    """arXiv API source (Atom XML).

    Respects the recommended 3-second inter-request interval via a fixed
    :class:`~academic_intelligence.utils.rate_limiter.RateLimiter`.
    """

    name = "arxiv"
    source_type = SourceType.ARXIV

    def __init__(
        self,
        http_client: Optional[HTTPClient] = None,
        *,
        confidence: float = 0.92,
        min_interval_seconds: float = 3.0,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self.confidence = confidence
        self.min_interval_seconds = max(min_interval_seconds, 0.1)

    async def _client(self) -> HTTPClient:
        if self._http is None:
            rps = 1.0 / self.min_interval_seconds
            strategy = AntiCrawlStrategy(base_delay=self.min_interval_seconds, adaptive_delay=False)
            self._http = HTTPClient(
                strategy=strategy,
                rate_limiter=create_rate_limiter("fixed", requests_per_second=rps),
                enable_cache=True,
            )
            await self._http.connect()
        return self._http

    async def close(self) -> None:
        """Close owned HTTP client."""
        if self._owns_client and self._http is not None:
            await self._http.close()
            self._http = None

    def _evidence(self, url: str, raw: Optional[Dict[str, Any]] = None) -> Evidence:
        return Evidence(
            source=self.source_type,
            source_url=url,
            collected_at=datetime.now(timezone.utc),
            confidence=self.confidence,
            raw_data=raw,
        )

    async def _query(self, search_query: str, *, start: int = 0, max_results: int = 10) -> str:
        client = await self._client()
        params = {
            "search_query": search_query,
            "start": start,
            "max_results": min(max(max_results, 1), 2000),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        try:
            response = await client.get(_API_BASE, params=params)
        except Exception as exc:
            raise SourceUnavailableError(
                f"arXiv request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "arXiv rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "3") or 3),
            )
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"arXiv HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        return response.text

    def _parse_entry(self, entry: ET.Element) -> Optional[Paper]:
        title = _text(entry.find("atom:title", _ATOM_NS))
        if not title:
            return None

        abstract = _text(entry.find("atom:summary", _ATOM_NS)) or None
        published = _text(entry.find("atom:published", _ATOM_NS))
        year: Optional[int] = None
        if published and len(published) >= 4 and published[:4].isdigit():
            year = int(published[:4])

        authors: List[str] = []
        for author_el in entry.findall("atom:author", _ATOM_NS):
            name = _text(author_el.find("atom:name", _ATOM_NS))
            if name:
                authors.append(name)

        # Prefer abs HTML link, then atom:id
        url: Optional[str] = None
        pdf_url: Optional[str] = None
        for link in entry.findall("atom:link", _ATOM_NS):
            rel = link.get("rel") or ""
            href = link.get("href") or ""
            title_attr = (link.get("title") or "").lower()
            link_type = (link.get("type") or "").lower()
            if not href:
                continue
            if title_attr == "pdf" or link_type == "application/pdf":
                pdf_url = href.replace("http://", "https://", 1)
            elif rel in ("alternate", "") and "arxiv.org/abs/" in href:
                url = href.replace("http://", "https://", 1)

        entry_id = _text(entry.find("atom:id", _ATOM_NS))
        arxiv_id: Optional[str] = None
        if entry_id:
            # http://arxiv.org/abs/2301.00001v1 -> 2301.00001v1
            arxiv_id = entry_id.rstrip("/").split("/")[-1]
            if not url:
                bare = arxiv_id.split("v")[0] if "v" in arxiv_id else arxiv_id
                url = f"https://arxiv.org/abs/{bare}"
            if not pdf_url:
                bare = arxiv_id.split("v")[0] if "v" in arxiv_id and arxiv_id[-1].isdigit() else arxiv_id
                # Keep version in pdf if present
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

        doi_el = entry.find("arxiv:doi", _ATOM_NS)
        doi_raw = _text(doi_el) if doi_el is not None else ""
        # Also check journal_ref / comment for DOI occasionally omitted

        categories: List[str] = []
        primary = entry.find("arxiv:primary_category", _ATOM_NS)
        if primary is not None and primary.get("term"):
            categories.append(primary.get("term") or "")
        for cat in entry.findall("atom:category", _ATOM_NS):
            term = cat.get("term")
            if term and term not in categories:
                categories.append(term)

        journal = entry.find("arxiv:journal_ref", _ATOM_NS)
        venue = _text(journal) if journal is not None else None
        if not venue and categories:
            venue = f"arXiv:{categories[0]}"

        source_url = url or "https://arxiv.org"
        evidence = self._evidence(
            source_url,
            raw={
                "id": entry_id,
                "arxiv_id": arxiv_id,
                "categories": categories,
                "published": published,
            },
        )
        safe_doi = _safe_doi(doi_raw or None, title, evidence)

        return Paper(
            id=arxiv_id,
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            abstract=abstract,
            doi=safe_doi,
            url=url if url and url.startswith("http") else None,
            pdf_url=pdf_url if pdf_url and pdf_url.startswith("http") else None,
            citations=None,
            keywords=categories,
            evidence=evidence,
        )

    def _parse_feed(self, xml_text: str) -> List[Paper]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ParseError(
                f"Invalid Atom XML from arXiv: {exc}",
                source_name=self.name,
                raw_snippet=xml_text[:300],
            ) from exc

        papers: List[Paper] = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            try:
                paper = self._parse_entry(entry)
                if paper is not None:
                    papers.append(paper)
            except Exception as exc:
                logger.debug("Skip arXiv entry parse error: %s", exc)
        return papers

    async def search_papers(self, query: str, limit: int = 10) -> List[Paper]:
        """Search arXiv papers.

        Free-text is mapped to ``all:`` field search. Callers may also pass
        raw arXiv query syntax (e.g. ``ti:transformer AND cat:cs.LG``).
        """
        q = query.strip()
        if not q:
            return []
        # If caller already provided field prefixes, use as-is
        if re.search(r"\b(all|ti|au|abs|co|jr|cat|rn):", q):
            search_query = q
        else:
            search_query = f"all:{q}"

        xml_text = await self._query(search_query, max_results=min(limit, 2000))
        papers = self._parse_feed(xml_text)
        return papers[:limit]

    async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
        """Fetch a paper by DOI via arXiv ``doi:`` field search."""
        cleaned = _normalize_doi(doi)
        if not cleaned:
            return None
        xml_text = await self._query(f'doi:"{cleaned}"', max_results=5)
        papers = self._parse_feed(xml_text)
        for paper in papers:
            if paper.doi and paper.doi.lower() == cleaned.lower():
                return paper
        return papers[0] if papers else None

    async def get_paper_by_arxiv_id(self, arxiv_id: str) -> Optional[Paper]:
        """Fetch a single paper by arXiv ID (convenience helper)."""
        match = _ARXIV_ID_RE.search(arxiv_id.strip())
        if not match:
            return None
        bare = match.group(1)
        # id_list is more precise but search_query id: works for new IDs
        client = await self._client()
        params = {"id_list": bare, "max_results": 1}
        try:
            response = await client.get(_API_BASE, params=params)
        except Exception as exc:
            raise SourceUnavailableError(
                f"arXiv request failed: {exc}",
                source_name=self.name,
            ) from exc
        if response.status_code == 429:
            raise RateLimitError("arXiv rate limit exceeded", source_name=self.name, retry_after=3)
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"arXiv HTTP {response.status_code}",
                source_name=self.name,
            )
        papers = self._parse_feed(response.text)
        return papers[0] if papers else None

    async def get_author_papers(self, author_name: str) -> List[Paper]:
        """Search papers by author name using the ``au:`` field."""
        name = author_name.strip()
        if not name:
            return []
        # arXiv author search prefers last-name-first or free form
        xml_text = await self._query(f'au:"{name}"', max_results=50)
        return self._parse_feed(xml_text)

    async def get_author_profile(self, author_name: str) -> Optional[Author]:
        """Build a lightweight author profile from search results.

        arXiv has no dedicated author profile endpoint; we aggregate from
        recent papers matching the author name.
        """
        name = author_name.strip()
        if not name:
            return None
        papers = await self.get_author_papers(name)
        if not papers:
            return None

        # Prefer an exact (case-insensitive) author match from paper lists
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
            if len(interests) >= 10:
                break

        profile_url = f"https://arxiv.org/search/?query={quote_plus(matched_name)}&searchtype=author"
        return Author(
            id=None,
            name=matched_name,
            affiliation=None,
            email=None,
            homepage=None,
            h_index=None,
            citations=None,
            interests=interests[:10],
            profile_url=profile_url,
            evidence=self._evidence(profile_url, raw={"paper_count": len(papers)}),
        )

    async def get_citations(self, paper_id: str) -> List[Citation]:
        """arXiv does not expose citation links; returns empty list."""
        return []
