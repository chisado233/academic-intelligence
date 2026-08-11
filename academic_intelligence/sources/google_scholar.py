"""Google Scholar data source adapter.

Supports SerpAPI (preferred) when an API key is configured. Without a key,
methods raise ``AuthenticationError`` with guidance, or return empty results
depending on configuration.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

import httpx

from academic_intelligence.core.exceptions import (
    AuthenticationError,
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
)
from academic_intelligence.core.types import SourceType
from academic_intelligence.sources.base import (
    BaseSource,
    is_rate_limit_status,
    retry_after_from_error,
)
from academic_intelligence.utils.http import HTTPClient

logger = logging.getLogger(__name__)

_SERPAPI_URL = "https://serpapi.com/search.json"
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


class GoogleScholarSource(BaseSource):
    """Google Scholar source via SerpAPI.

    Attributes:
        name: Source identifier.
        source_type: SourceType.GOOGLE_SCHOLAR
        serpapi_key: Optional SerpAPI key for live queries.
    """

    name = "google_scholar"
    source_type = SourceType.GOOGLE_SCHOLAR
    capabilities = {
        **BaseSource.capabilities,
        # C1 revision: kept for consistency (adapter disabled by default).
        "citations": True,
        "get_author_papers": True,
        "get_author_profile": True,
        "get_citations": True,
    }

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        serpapi_key: str | None = None,
        confidence: float = 0.75,
    ) -> None:
        """Initialize Google Scholar source.

        Args:
            http_client: Shared HTTP client (optional; created if omitted).
            serpapi_key: SerpAPI API key. Required for live collection.
            confidence: Default evidence confidence for this source.
        """
        self._http = http_client
        self._owns_client = http_client is None
        self.serpapi_key = serpapi_key
        self.confidence = confidence

    async def _client(self) -> HTTPClient:
        if self._http is None:
            self._http = HTTPClient()
            await self._http.connect()
        return self._http

    async def close(self) -> None:
        """Close owned HTTP client."""
        if self._owns_client and self._http is not None:
            await self._http.close()
            self._http = None

    def _require_key(self) -> str:
        if not self.serpapi_key:
            raise AuthenticationError(
                "Google Scholar requires a SerpAPI key. "
                "Set Config.serpapi_key or SERPAPI_KEY environment variable.",
                source_name=self.name,
            )
        return self.serpapi_key

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

    async def _serpapi_get(self, params: dict[str, Any]) -> dict[str, Any]:
        key = self._require_key()
        client = await self._client()
        # Callers may override engine (e.g. google_scholar_profiles); keep api_key last
        query_params = {"engine": "google_scholar", **params, "api_key": key}
        try:
            response = await client.get(_SERPAPI_URL, params=query_params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"SerpAPI request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    "SerpAPI rate limit exceeded",
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"SerpAPI request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "SerpAPI rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code == 401:
            raise AuthenticationError(
                "Invalid SerpAPI key",
                source_name=self.name,
            )
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"SerpAPI HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        try:
            data = response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from SerpAPI: {exc}",
                source_name=self.name,
                raw_snippet=response.text[:300],
            ) from exc
        if "error" in data:
            raise SourceUnavailableError(
                str(data["error"]),
                source_name=self.name,
            )
        return data  # type: ignore[no-any-return]

    def _parse_organic(self, item: dict[str, Any], source_url: str) -> Paper | None:
        title = (item.get("title") or "").strip()
        if not title:
            return None
        publication_info = item.get("publication_info") or {}
        summary = publication_info.get("summary") or ""
        authors: list[AuthorRef] = []
        if "authors" in publication_info and isinstance(publication_info["authors"], list):
            for a in publication_info["authors"]:
                name = a.get("name", a) if isinstance(a, dict) else str(a)
                authors.append(AuthorRef(name=str(name), position=len(authors) + 1))
        elif summary:
            # "A Author, B Author - Venue, 2020 - publisher"
            head = summary.split(" - ")[0]
            for name in (a.strip() for a in head.split(",") if a.strip()):
                authors.append(AuthorRef(name=name, position=len(authors) + 1))

        year: int | None = None
        year_match = _YEAR_RE.search(summary) or _YEAR_RE.search(title)
        if year_match:
            year = int(year_match.group(0))

        venue: str | None = None
        parts = [p.strip() for p in summary.split(" - ") if p.strip()]
        if len(parts) >= 3:
            # "Author(s) - Venue, 2020 - publisher"
            if not _YEAR_RE.sub("", parts[1]).strip():
                # "T Mitchell - 1997 - McGraw Hill": parts[1] is the year
                # itself, so the venue segment is missing and the trailing
                # publisher stands in for it (FIX-J J-1)
                venue = parts[2].strip(" ,")
            else:
                venue = _YEAR_RE.sub("", parts[1]).strip(" ,")
        elif len(parts) == 2:
            if _YEAR_RE.search(parts[0]) and not _YEAR_RE.search(parts[1]):
                # "Conference on X, 2019 - IEEE": no author segment; the venue
                # (with its year) precedes the publisher
                venue = parts[0].strip(" ,")
            else:
                venue = _YEAR_RE.sub("", parts[1]).strip(" ,")
        else:
            venue = None

        inline = item.get("inline_links") or {}
        cited_by = inline.get("cited_by") or {}
        citation_count: int | None = None
        if isinstance(cited_by, dict) and cited_by.get("total") is not None:
            try:
                citation_count = int(cited_by["total"])
            except (TypeError, ValueError):
                citation_count = None

        link = item.get("link") or item.get("result_id")
        url = link if isinstance(link, str) and link.startswith("http") else source_url

        resources = item.get("resources") or []
        pdf_url: str | None = None
        for res in resources:
            if isinstance(res, dict) and res.get("file_format", "").upper() == "PDF":
                pdf_url = res.get("link")
                break

        return Paper(
            id=item.get("result_id"),
            title=title,
            authors=authors,
            year=year,
            venue=venue or None,
            abstract=item.get("snippet"),
            doi=None,
            url=url,
            pdf_url=pdf_url,
            citations=citation_count,
            keywords=[],
            evidence_list=[
                self._evidence(source_url, raw=item, source_id=item.get("result_id"))
            ],
        )

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Search Google Scholar for papers matching *query*."""
        data = await self._serpapi_get({"q": query, "num": min(limit, 20)})
        source_url = f"https://scholar.google.com/scholar?q={quote_plus(query)}"
        organic = data.get("organic_results") or []
        papers: list[Paper] = []
        for item in organic:
            if not isinstance(item, dict):
                continue
            paper = self._parse_organic(item, source_url)
            if paper:
                papers.append(paper)
            if len(papers) >= limit:
                break
        return papers

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        """Search Scholar by DOI string."""
        papers = await self.search_papers(f'doi:"{doi}"', limit=5)
        cleaned = doi.lower().strip()
        for paper in papers:
            if paper.doi and paper.doi.lower() == cleaned:
                return paper
        # Fall back to first result if DOI is in title/snippet context
        return papers[0] if papers else None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Retrieve papers by author via Scholar author search query."""
        query = f'author:"{author_name}"'
        return await self.search_papers(query, limit=20)

    async def get_author_profile(self, author_name: str) -> Author | None:
        """Retrieve a basic author profile from Scholar author search.

        Uses SerpAPI ``google_scholar_profiles`` when available.
        """
        try:
            data = await self._serpapi_get(
                {
                    "engine": "google_scholar_profiles",
                    "mauthors": author_name,
                }
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            logger.debug("Author profile lookup failed: %s", exc)
            return None

        profiles = data.get("profiles") or []
        if not profiles:
            return None
        profile = profiles[0]
        source_url = profile.get("link") or (
            f"https://scholar.google.com/citations?view_op=search_authors&mauthors="
            f"{quote_plus(author_name)}"
        )
        interests: list[str] = []
        for item in profile.get("interests") or []:
            if isinstance(item, dict) and item.get("title"):
                interests.append(str(item["title"]))
            elif isinstance(item, str):
                interests.append(item)

        cited_by = profile.get("cited_by")
        citations: int | None = None
        if cited_by is not None:
            try:
                citations = int(cited_by)
            except (TypeError, ValueError):
                citations = None

        return Author(
            id=profile.get("author_id"),
            name=profile.get("name") or author_name,
            affiliation=profile.get("affiliations"),
            email=None,
            homepage=None,
            h_index=None,
            citations=citations,
            interests=interests,
            profile_url=source_url if isinstance(source_url, str) else None,
            evidence_list=[
                self._evidence(
                    source_url
                    if isinstance(source_url, str)
                    else "https://scholar.google.com",
                    raw=profile,
                )
            ],
        )

    async def get_citations(self, paper_id: str) -> list[Citation]:
        """Retrieve citing papers for a Scholar result id via SerpAPI cites.

        Note: Google Scholar citation edges are modeled as citing → cited,
        where *paper_id* is the cited paper's result id.
        """
        data = await self._serpapi_get(
            {
                "cites": paper_id,
                "num": 20,
            }
        )
        source_url = f"https://scholar.google.com/scholar?cites={quote_plus(paper_id)}"
        organic = data.get("organic_results") or []
        citations: list[Citation] = []
        for item in organic:
            if not isinstance(item, dict):
                continue
            citing_id = item.get("result_id") or item.get("link") or item.get("title")
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
