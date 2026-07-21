"""IEEE Xplore data source adapter.

Uses the IEEE Xplore Metadata API:
https://developer.ieee.org/

Requires an API key (set via constructor or IEEE_API_KEY env var).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from academic_intelligence.core.exceptions import (
    AuthenticationError,
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

_API_BASE = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")


def _normalize_doi(doi: str) -> str:
    cleaned = doi.strip()
    for prefix in _DOI_PREFIXES:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def _safe_doi(doi: Optional[str], title: str, evidence: Evidence) -> Optional[str]:
    if not doi:
        return None
    try:
        Paper.model_validate({"title": title or "untitled", "doi": doi, "evidence": evidence})
        return doi
    except Exception:
        return None


class IEEESource(BaseSource):
    """IEEE Xplore Metadata API source.

    An API key is required for all live requests. Obtain one at
    https://developer.ieee.org/
    """

    name = "ieee"
    source_type = SourceType.IEEE

    def __init__(
        self,
        http_client: Optional[HTTPClient] = None,
        *,
        api_key: Optional[str] = None,
        confidence: float = 0.88,
        requests_per_second: float = 2.0,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self.api_key = api_key or os.environ.get("IEEE_API_KEY")
        self.confidence = confidence
        self._requests_per_second = max(requests_per_second, 0.1)

    async def _client(self) -> HTTPClient:
        if self._http is None:
            base_delay = 1.0 / self._requests_per_second
            strategy = AntiCrawlStrategy(base_delay=base_delay, adaptive_delay=True)
            self._http = HTTPClient(
                strategy=strategy,
                rate_limiter=create_rate_limiter(
                    "adaptive", requests_per_second=self._requests_per_second
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

    def _require_key(self) -> str:
        if not self.api_key:
            raise AuthenticationError(
                "IEEE Xplore requires an API key. "
                "Pass api_key=... or set IEEE_API_KEY environment variable. "
                "Register at https://developer.ieee.org/",
                source_name=self.name,
            )
        return self.api_key

    def _evidence(self, url: str, raw: Optional[Dict[str, Any]] = None) -> Evidence:
        return Evidence(
            source=self.source_type,
            source_url=url,
            collected_at=datetime.now(timezone.utc),
            confidence=self.confidence,
            raw_data=raw,
        )

    async def _search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        key = self._require_key()
        client = await self._client()
        query = {"apikey": key, **params}
        try:
            response = await client.get(_API_BASE, params=query)
        except Exception as exc:
            raise SourceUnavailableError(
                f"IEEE Xplore request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "IEEE Xplore rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code in (401, 403):
            raise AuthenticationError(
                "IEEE Xplore authentication failed (check API key)",
                source_name=self.name,
            )
        if response.status_code == 404:
            return {}
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"IEEE Xplore HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        try:
            data = response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from IEEE Xplore: {exc}",
                source_name=self.name,
                raw_snippet=response.text[:300],
            ) from exc
        if not isinstance(data, dict):
            raise ParseError(
                "IEEE Xplore response is not a JSON object",
                source_name=self.name,
                raw_snippet=str(data)[:300],
            )
        return data

    def _parse_authors(self, data: Dict[str, Any]) -> List[str]:
        authors: List[str] = []
        # authors.authors is the common shape
        authors_block = data.get("authors")
        if isinstance(authors_block, dict):
            for item in authors_block.get("authors") or []:
                if isinstance(item, dict):
                    name = item.get("full_name") or item.get("preferred_name")
                    if name:
                        authors.append(str(name))
                elif isinstance(item, str):
                    authors.append(item)
        elif isinstance(authors_block, list):
            for item in authors_block:
                if isinstance(item, dict):
                    name = item.get("full_name") or item.get("preferred_name") or item.get("name")
                    if name:
                        authors.append(str(name))
                elif isinstance(item, str):
                    authors.append(item)
        # Fallback: author_names string
        if not authors and data.get("author_names"):
            raw = str(data["author_names"])
            authors = [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]
        return authors

    def _parse_paper(self, data: Dict[str, Any]) -> Paper:
        title = (data.get("title") or data.get("article_title") or "").strip() or "Untitled"
        authors = self._parse_authors(data)

        year_raw = data.get("publication_year") or data.get("year")
        year: Optional[int] = None
        if year_raw is not None:
            try:
                year = int(str(year_raw)[:4])
            except (TypeError, ValueError):
                year = None

        venue = (
            data.get("publication_title")
            or data.get("conference_name")
            or data.get("publisher")
            or None
        )
        if isinstance(venue, str):
            venue = venue.strip() or None

        abstract = data.get("abstract")
        if isinstance(abstract, str):
            abstract = abstract.strip() or None
        else:
            abstract = None

        doi_raw = data.get("doi")
        doi: Optional[str] = None
        if isinstance(doi_raw, str) and doi_raw.strip():
            doi = _normalize_doi(doi_raw)

        article_number = data.get("article_number") or data.get("articleNumber")
        html_url = data.get("html_url") or data.get("abstract_url")
        if not html_url and article_number:
            html_url = f"https://ieeexplore.ieee.org/document/{article_number}"
        if isinstance(html_url, str) and html_url.startswith("//"):
            html_url = "https:" + html_url

        pdf_url = data.get("pdf_url")
        if isinstance(pdf_url, str) and pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url

        citations = data.get("citing_paper_count") or data.get("citation_count")
        try:
            citations_int: Optional[int] = int(citations) if citations is not None else None
        except (TypeError, ValueError):
            citations_int = None

        keywords: List[str] = []
        index_terms = data.get("index_terms") or {}
        if isinstance(index_terms, dict):
            for key in ("ieee_terms", "author_terms", "controlledterms", "controlled_terms"):
                block = index_terms.get(key)
                if isinstance(block, dict):
                    terms = block.get("terms") or block.get("term") or []
                    if isinstance(terms, list):
                        for t in terms:
                            if t and str(t) not in keywords:
                                keywords.append(str(t))
                    elif isinstance(terms, str) and terms not in keywords:
                        keywords.append(terms)
                elif isinstance(block, list):
                    for t in block:
                        if t and str(t) not in keywords:
                            keywords.append(str(t))
        # Top-level keyword fields
        for key in ("keywords", "ieee_terms", "author_terms"):
            val = data.get(key)
            if isinstance(val, list):
                for t in val:
                    if t and str(t) not in keywords:
                        keywords.append(str(t))
            elif isinstance(val, str) and val not in keywords:
                keywords.append(val)

        paper_id = str(article_number) if article_number is not None else None
        source_url = (
            html_url
            if isinstance(html_url, str) and html_url.startswith("http")
            else "https://ieeexplore.ieee.org"
        )
        evidence = self._evidence(source_url, raw=data if isinstance(data, dict) else None)
        safe_doi = _safe_doi(doi, title, evidence)

        return Paper(
            id=paper_id,
            title=title,
            authors=authors,
            year=year,
            venue=venue if isinstance(venue, str) else None,
            abstract=abstract,
            doi=safe_doi,
            url=source_url if source_url.startswith("http") else None,
            pdf_url=pdf_url if isinstance(pdf_url, str) and pdf_url.startswith("http") else None,
            citations=citations_int,
            keywords=keywords[:30],
            evidence=evidence,
        )

    def _articles_from_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        articles = data.get("articles") or data.get("article") or []
        if isinstance(articles, dict):
            articles = [articles]
        return [a for a in articles if isinstance(a, dict)]

    async def search_papers(self, query: str, limit: int = 10) -> List[Paper]:
        """Search IEEE Xplore articles by free-text query."""
        q = query.strip()
        if not q:
            return []
        data = await self._search(
            {
                "querytext": q,
                "max_records": min(max(limit, 1), 200),
                "start_record": 1,
                "sort_order": "desc",
                "sort_field": "relevance",
            }
        )
        papers: List[Paper] = []
        for item in self._articles_from_response(data):
            try:
                papers.append(self._parse_paper(item))
            except Exception as exc:
                logger.debug("Skip IEEE paper parse: %s", exc)
            if len(papers) >= limit:
                break
        return papers

    async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
        """Fetch an article by DOI."""
        cleaned = _normalize_doi(doi)
        if not cleaned:
            return None
        data = await self._search(
            {
                "doi": cleaned,
                "max_records": 5,
                "start_record": 1,
            }
        )
        articles = self._articles_from_response(data)
        if not articles:
            # Fallback: querytext with DOI
            data = await self._search(
                {
                    "querytext": cleaned,
                    "max_records": 5,
                    "start_record": 1,
                }
            )
            articles = self._articles_from_response(data)

        for item in articles:
            try:
                paper = self._parse_paper(item)
            except Exception as exc:
                logger.debug("Skip IEEE DOI paper: %s", exc)
                continue
            if paper.doi and paper.doi.lower() == cleaned.lower():
                return paper
        if articles:
            try:
                return self._parse_paper(articles[0])
            except Exception:
                return None
        return None

    async def get_author_papers(self, author_name: str) -> List[Paper]:
        """Search articles by author name."""
        name = author_name.strip()
        if not name:
            return []
        data = await self._search(
            {
                "author": name,
                "max_records": 50,
                "start_record": 1,
                "sort_order": "desc",
                "sort_field": "publication_year",
            }
        )
        papers: List[Paper] = []
        for item in self._articles_from_response(data):
            try:
                papers.append(self._parse_paper(item))
            except Exception as exc:
                logger.debug("Skip IEEE author paper: %s", exc)
        return papers

    async def get_author_profile(self, author_name: str) -> Optional[Author]:
        """Build a lightweight author profile from IEEE search results."""
        name = author_name.strip()
        if not name:
            return None
        papers = await self.get_author_papers(name)
        if not papers:
            return None

        matched_name = name
        interests: List[str] = []
        total_citations = 0
        for paper in papers:
            for a in paper.authors:
                if a.lower() == name.lower() or name.lower() in a.lower():
                    matched_name = a
                    break
            for kw in paper.keywords:
                if kw and kw not in interests:
                    interests.append(kw)
            if paper.citations:
                total_citations += paper.citations
            if len(interests) >= 15:
                break

        profile_url = f"https://ieeexplore.ieee.org/search/searchresult.jsp?queryText=author:{matched_name}"
        return Author(
            id=None,
            name=matched_name,
            affiliation=None,
            email=None,
            homepage=None,
            h_index=None,
            citations=total_citations or None,
            interests=interests[:15],
            profile_url=profile_url if profile_url.startswith("http") else None,
            evidence=self._evidence(
                profile_url if profile_url.startswith("http") else "https://ieeexplore.ieee.org",
                raw={"paper_count": len(papers)},
            ),
        )

    async def get_citations(self, paper_id: str) -> List[Citation]:
        """IEEE Metadata API does not expose full citation graphs; returns empty.

        Citation counts may still appear on individual Paper records when
        present in search results.
        """
        return []
