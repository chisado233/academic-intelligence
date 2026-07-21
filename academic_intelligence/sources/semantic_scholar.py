"""Semantic Scholar data source adapter.

Uses the public Semantic Scholar Graph API:
https://api.semanticscholar.org/graph/v1
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from academic_intelligence.core.exceptions import (
    AuthenticationError,
    ParseError,
    RateLimitError,
    SourceUnavailableError,
)
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.utils.http import HTTPClient

logger = logging.getLogger(__name__)

_API_BASE = "https://api.semanticscholar.org/graph/v1"
_PAPER_FIELDS = (
    "paperId,title,abstract,year,venue,citationCount,externalIds,"
    "url,openAccessPdf,authors,fieldsOfStudy"
)
_AUTHOR_FIELDS = "authorId,name,affiliations,homepage,hIndex,citationCount,paperCount,url"


class SemanticScholarSource(BaseSource):
    """Semantic Scholar Graph API source."""

    name = "semantic_scholar"
    source_type = SourceType.SEMANTIC_SCHOLAR

    def __init__(
        self,
        http_client: Optional[HTTPClient] = None,
        *,
        api_key: Optional[str] = None,
        confidence: float = 0.9,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self.api_key = api_key
        self.confidence = confidence

    async def _client(self) -> HTTPClient:
        if self._http is None:
            self._http = HTTPClient()
            await self._http.connect()
        return self._http

    async def close(self) -> None:
        if self._owns_client and self._http is not None:
            await self._http.close()
            self._http = None

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _evidence(self, url: str, raw: Optional[Dict[str, Any]] = None) -> Evidence:
        return Evidence(
            source=self.source_type,
            source_url=url,
            collected_at=datetime.now(timezone.utc),
            confidence=self.confidence,
            raw_data=raw,
        )

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        client = await self._client()
        url = f"{_API_BASE}{path}"
        try:
            response = await client.get(url, headers=self._headers(), params=params)
        except Exception as exc:
            raise SourceUnavailableError(
                f"Semantic Scholar request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "Semantic Scholar rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code in (401, 403):
            raise AuthenticationError(
                "Semantic Scholar authentication failed",
                source_name=self.name,
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"Semantic Scholar HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        try:
            return response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from Semantic Scholar: {exc}",
                source_name=self.name,
                raw_snippet=response.text[:300],
            ) from exc

    def _parse_paper(self, data: Dict[str, Any]) -> Paper:
        external = data.get("externalIds") or {}
        doi = external.get("DOI")
        authors = []
        for a in data.get("authors") or []:
            if isinstance(a, dict) and a.get("name"):
                authors.append(str(a["name"]))
            elif isinstance(a, str):
                authors.append(a)

        pdf_info = data.get("openAccessPdf") or {}
        pdf_url = pdf_info.get("url") if isinstance(pdf_info, dict) else None
        paper_id = data.get("paperId")
        url = data.get("url") or (
            f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else None
        )
        fields = data.get("fieldsOfStudy") or []
        keywords = [str(f) for f in fields if f]

        # Soften invalid DOI so validation does not fail the whole record
        safe_doi: Optional[str] = None
        if doi:
            try:
                Paper.model_validate(
                    {
                        "title": data.get("title") or "untitled",
                        "doi": doi,
                        "evidence": self._evidence(url or "https://www.semanticscholar.org"),
                    }
                )
                safe_doi = doi
            except Exception:
                safe_doi = None

        return Paper(
            id=paper_id,
            title=(data.get("title") or "").strip() or "Untitled",
            authors=authors,
            year=data.get("year"),
            venue=data.get("venue") or None,
            abstract=data.get("abstract"),
            doi=safe_doi,
            url=url if isinstance(url, str) and url.startswith("http") else None,
            pdf_url=pdf_url if isinstance(pdf_url, str) and pdf_url.startswith("http") else None,
            citations=data.get("citationCount"),
            keywords=keywords,
            evidence=self._evidence(
                url if isinstance(url, str) else "https://www.semanticscholar.org",
                raw=data,
            ),
        )

    def _parse_author(self, data: Dict[str, Any]) -> Author:
        affiliations = data.get("affiliations") or []
        affiliation = affiliations[0] if affiliations else None
        if isinstance(affiliation, dict):
            affiliation = affiliation.get("name")
        homepage = data.get("homepage")
        profile_url = data.get("url") or (
            f"https://www.semanticscholar.org/author/{data.get('authorId')}"
            if data.get("authorId")
            else None
        )
        return Author(
            id=data.get("authorId"),
            name=data.get("name") or "Unknown",
            affiliation=str(affiliation) if affiliation else None,
            email=None,
            homepage=homepage if isinstance(homepage, str) and homepage.startswith("http") else None,
            h_index=data.get("hIndex"),
            citations=data.get("citationCount"),
            interests=[],
            profile_url=(
                profile_url
                if isinstance(profile_url, str) and profile_url.startswith("http")
                else None
            ),
            evidence=self._evidence(
                profile_url
                if isinstance(profile_url, str)
                else "https://www.semanticscholar.org",
                raw=data,
            ),
        )

    async def search_papers(self, query: str, limit: int = 10) -> List[Paper]:
        """Search papers via Semantic Scholar."""
        data = await self._get_json(
            "/paper/search",
            params={"query": query, "limit": min(limit, 100), "fields": _PAPER_FIELDS},
        )
        if not data:
            return []
        papers: List[Paper] = []
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("title"):
                try:
                    papers.append(self._parse_paper(item))
                except Exception as exc:
                    logger.debug("Skip paper parse error: %s", exc)
            if len(papers) >= limit:
                break
        return papers

    async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
        """Fetch paper by DOI."""
        cleaned = doi.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                break
        data = await self._get_json(
            f"/paper/DOI:{cleaned}",
            params={"fields": _PAPER_FIELDS},
        )
        if not data or not isinstance(data, dict):
            return None
        return self._parse_paper(data)

    async def get_author_papers(self, author_name: str) -> List[Paper]:
        """Find author then fetch their papers."""
        search = await self._get_json(
            "/author/search",
            params={"query": author_name, "limit": 1, "fields": "authorId,name"},
        )
        if not search or not (search.get("data") or []):
            # Fallback: paper search by author name
            return await self.search_papers(f'"{author_name}"', limit=20)

        author_id = search["data"][0].get("authorId")
        if not author_id:
            return await self.search_papers(f'"{author_name}"', limit=20)

        data = await self._get_json(
            f"/author/{author_id}/papers",
            params={"limit": 50, "fields": _PAPER_FIELDS},
        )
        if not data:
            return []
        papers: List[Paper] = []
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("title"):
                try:
                    papers.append(self._parse_paper(item))
                except Exception as exc:
                    logger.debug("Skip author paper parse: %s", exc)
        return papers

    async def get_author_profile(self, author_name: str) -> Optional[Author]:
        """Search and return first matching author profile."""
        search = await self._get_json(
            "/author/search",
            params={"query": author_name, "limit": 1, "fields": _AUTHOR_FIELDS},
        )
        if not search or not (search.get("data") or []):
            return None
        item = search["data"][0]
        if not isinstance(item, dict):
            return None
        return self._parse_author(item)

    async def get_citations(self, paper_id: str) -> List[Citation]:
        """Get papers that cite *paper_id*."""
        data = await self._get_json(
            f"/paper/{paper_id}/citations",
            params={"limit": 50, "fields": "citingPaper.paperId,citingPaper.title"},
        )
        if not data:
            return []
        citations: List[Citation] = []
        source_url = f"https://www.semanticscholar.org/paper/{paper_id}"
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            citing = item.get("citingPaper") or {}
            citing_id = citing.get("paperId")
            if not citing_id or citing_id == paper_id:
                continue
            citations.append(
                Citation(
                    citing_paper_id=str(citing_id),
                    cited_paper_id=paper_id,
                    evidence=self._evidence(source_url, raw=item),
                )
            )
        return citations
