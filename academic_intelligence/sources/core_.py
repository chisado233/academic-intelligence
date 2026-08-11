"""CORE (core.ac.uk) data source adapter.

Uses the CORE API v3::

    https://api.core.ac.uk/v3/search/works?q=...&limit=N   (search)
    https://api.core.ac.uk/v3/works/{id}                   (by CORE id)

CORE aggregates legal open-access full text (``downloadUrl`` /
``sourceFulltextUrls``), so this adapter declares ``fulltext=True`` while
author-class and citation operations are unsupported (``capabilities``
``False``, methods raise :class:`NotSupportedError`).

Authentication: ``Authorization: Bearer {api_key}``.  A key is optional —
the public tier works without one at a lower rate (``CORE_API_KEY`` /
``api_key=...`` raise the limits).  An invalid/expired key yields HTTP 401
which is surfaced as :class:`AuthenticationError`.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from academic_intelligence.core.exceptions import (
    AuthenticationError,
    NotSupportedError,
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
)
from academic_intelligence.core.types import SourceType
from academic_intelligence.sources.base import (
    BaseSource,
    is_rate_limit_status,
    retry_after_from_error,
)
from academic_intelligence.utils.http import HTTPClient

logger = logging.getLogger(__name__)

_API_BASE = "https://api.core.ac.uk/v3"
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")
# CORE ``limit`` is capped at 100 per request on the public tier.
_MAX_SEARCH_LIMIT = 100
#: 429 guidance: the keyless public tier is heavily rate-limited — point the
#: user at the free key instead of letting the retry loop look like a hang.
_CORE_RATE_LIMIT_MESSAGE = (
    "CORE rate limit exceeded. A free CORE_API_KEY raises the quota: "
    "register one at https://core.ac.uk/services/api and set the "
    "CORE_API_KEY environment variable (or pass api_key=...)"
)
# Model-level caps (Paper.title=500, Paper.abstract=20000, Paper.venue=300):
# CORE aggregates 300M+ heterogeneous records, so defensive truncation keeps
# an over-long field from dropping the whole record.
_TITLE_MAX = 500
_ABSTRACT_MAX = 20000
_VENUE_MAX = 300


def _normalize_doi(doi: str) -> str:
    """Strip ``https://doi.org/`` / ``doi:`` prefixes from *doi*."""
    cleaned = doi.strip()
    for prefix in _DOI_PREFIXES:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def _strip_http_prefix(value: str) -> str:
    """Return *value* unchanged; only strips when it is a full CORE URL.

    Used to reduce a CORE landing/API URL (``https://core.ac.uk/works/123``
    or ``https://api.core.ac.uk/v3/works/123``) to the bare numeric CORE id.
    Non-URL ids pass through untouched.
    """
    value = value.strip()
    if value.lower().startswith(("http://", "https://")):
        return value.rstrip("/").rsplit("/", 1)[-1]
    return value


class CoreSource(BaseSource):
    """CORE API v3 source (open-access metadata + full-text links)."""

    name = "core"
    source_type = SourceType.CORE
    capabilities = {
        **BaseSource.capabilities,
        # Author-class and citation operations are not provided by CORE.
        "get_citations": False,
        "get_author_papers": False,
        "get_author_profile": False,
        "get_paper_by_id": True,
        # C1 CLI operation keys (technical-design.md §1.1.1).
        "search": True,
        "get": True,
        "citations": False,
        "fulltext": True,
        "author": False,
    }

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        api_key: str | None = None,
        confidence: float = 0.85,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self.api_key = api_key or os.environ.get("CORE_API_KEY")
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

    def _headers(self) -> dict[str, str]:
        """Bearer auth headers; empty dict on the keyless public tier."""
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

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

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET *path* on the CORE API and return the parsed JSON body.

        HTTP error mapping (aligned with the other adapters):

        - 401 → :class:`AuthenticationError` (bad/expired API key)
        - 429 → :class:`RateLimitError` (public tier or key quota)
        - 404 → ``None`` (unknown CORE id)
        - other ``>= 400`` → :class:`SourceUnavailableError`
        """
        client = await self._client()
        url = f"{_API_BASE}{path}"
        try:
            response = await client.get(url, headers=self._headers(), params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"CORE request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    _CORE_RATE_LIMIT_MESSAGE,
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"CORE request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 401:
            raise AuthenticationError(
                "CORE rejected the API key (HTTP 401). Pass api_key=... or "
                "set the CORE_API_KEY environment variable; register a free "
                "key at https://core.ac.uk/services/api",
                source_name=self.name,
            )
        if response.status_code == 429:
            raise RateLimitError(
                _CORE_RATE_LIMIT_MESSAGE,
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"CORE HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        try:
            return response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from CORE: {exc}",
                source_name=self.name,
                raw_snippet=response.text[:300],
            ) from exc

    def _parse_authors(self, data: Any) -> list[AuthorRef]:
        """Parse CORE ``authors`` (list of ``{"name": ...}`` or plain strings)."""
        authors: list[AuthorRef] = []
        if not isinstance(data, list):
            return authors
        for entry in data:
            name: str | None = None
            if isinstance(entry, str) and entry.strip():
                name = entry.strip()
            elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                name = str(entry["name"]).strip()
            if name:
                authors.append(AuthorRef(name=name, position=len(authors) + 1))
        return authors

    def _parse_year(self, data: Any) -> int | None:
        """Parse CORE ``yearPublished`` (int or numeric string), bounded."""
        if data in (None, ""):
            return None
        try:
            year = int(data)
        except (TypeError, ValueError):
            return None
        current = datetime.now(UTC).year
        if year < 1800 or year > current + 1:
            return None
        return year

    def _parse_venue(self, data: dict[str, Any]) -> str | None:
        """Venue = publisher, else the first journal title."""
        publisher = data.get("publisher")
        if isinstance(publisher, str) and publisher.strip():
            return publisher.strip()[:_VENUE_MAX]
        journals = data.get("journals")
        if isinstance(journals, list):
            for journal in journals:
                if isinstance(journal, dict) and isinstance(journal.get("title"), str):
                    title = str(journal["title"]).strip()
                    if title:
                        return title[:_VENUE_MAX]
        return None

    def _parse_keywords(self, data: Any) -> list[str]:
        """Keywords from CORE ``fieldOfStudy`` (string or list of strings)."""
        keywords: list[str] = []
        if isinstance(data, str) and data.strip():
            keywords.append(data.strip())
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item.strip():
                    keywords.append(item.strip())
        return keywords

    def _parse_paper(self, data: dict[str, Any]) -> Paper | None:
        """Map a CORE work record onto the :class:`Paper` model."""
        raw_title = data.get("title")
        title = str(raw_title).strip() if isinstance(raw_title, str) else ""
        if not title:
            return None
        title = title[:_TITLE_MAX]

        paper_id = data.get("id")
        paper_id_str = str(paper_id) if paper_id is not None else None

        raw_abstract = data.get("abstract")
        abstract: str | None = None
        if isinstance(raw_abstract, str) and raw_abstract.strip():
            abstract = raw_abstract.strip()[:_ABSTRACT_MAX]

        doi_raw = data.get("doi")
        doi: str | None = None
        if isinstance(doi_raw, str):
            doi = normalize_doi(doi_raw)

        download_url = data.get("downloadUrl")
        pdf_url: str | None = None
        if isinstance(download_url, str) and download_url.strip().startswith("http"):
            pdf_url = download_url.strip()

        # Landing page: CORE ``links`` display entry, else the canonical
        # ``https://core.ac.uk/works/{id}`` page.
        url: str | None = None
        links = data.get("links")
        if isinstance(links, list):
            for item in links:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "display"
                    and isinstance(item.get("url"), str)
                    and item["url"].startswith("http")
                ):
                    url = str(item["url"])
                    break
        if url is None and paper_id_str:
            url = f"https://core.ac.uk/works/{paper_id_str}"

        source_url = (
            f"{_API_BASE}/works/{paper_id_str}" if paper_id_str else "https://core.ac.uk"
        )
        evidence = self._evidence(
            source_url,
            raw=data,
            source_id=paper_id_str,
        )

        return Paper(
            id=paper_id_str,
            title=title,
            authors=self._parse_authors(data.get("authors")),
            year=self._parse_year(data.get("yearPublished")),
            venue=self._parse_venue(data),
            abstract=abstract,
            doi=doi,
            url=url,
            pdf_url=pdf_url,
            citations=self._parse_int(data.get("citationCount")),
            keywords=self._parse_keywords(data.get("fieldOfStudy")),
            evidence_list=[evidence],
        )

    @staticmethod
    def _parse_int(data: Any) -> int | None:
        """Parse a non-negative integer field, ``None`` when absent/invalid."""
        if data is None:
            return None
        try:
            value = int(data)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Search CORE works by free-text *query*."""
        q = query.strip()
        if not q:
            return []
        data = await self._get_json(
            "/search/works",
            params={"q": q, "limit": min(max(limit, 1), _MAX_SEARCH_LIMIT)},
        )
        if not isinstance(data, dict):
            return []
        papers: list[Paper] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            try:
                paper = self._parse_paper(item)
            except Exception as exc:
                logger.debug("Skip CORE paper: %s", exc)
                continue
            if paper is not None:
                papers.append(paper)
            if len(papers) >= limit:
                break
        return papers

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch a CORE work by DOI (via the ``doi:`` field search)."""
        cleaned = _normalize_doi(doi)
        if not cleaned:
            return None
        if normalize_doi(cleaned) is None:
            # Not a well-formed DOI (10.<registrant>/<suffix>); a CORE
            # ``doi:"..."`` search on it would be noise, so fail soft.
            return None
        data = await self._get_json(
            "/search/works",
            params={"q": f'doi:"{cleaned}"', "limit": 5},
        )
        if not isinstance(data, dict):
            return None
        papers: list[Paper] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            try:
                paper = self._parse_paper(item)
            except Exception as exc:
                logger.debug("Skip CORE DOI result: %s", exc)
                continue
            if paper is None:
                continue
            papers.append(paper)
            if paper.doi and paper.doi.lower() == cleaned.lower():
                return paper
        return papers[0] if papers else None

    async def get_paper_by_id(self, work_id: str) -> Paper | None:
        """Fetch a CORE work record by its CORE id.

        Accepts a bare id (``"168955695"``) or a full CORE URL
        (``https://core.ac.uk/works/168955695``); anything that is not a
        numeric CORE id returns ``None`` without making a request.
        """
        normalized = _strip_http_prefix(str(work_id))
        if not normalized.isdigit():
            return None
        data = await self._get_json(f"/works/{normalized}")
        if not isinstance(data, dict):
            return None
        return self._parse_paper(data)

    async def get_fulltext(self, paper: Paper) -> str | None:
        """Return the best legal OA full-text link for *paper*, or ``None``.

        Priority: ``downloadUrl`` (direct OA PDF) > ``sourceFulltextUrls``
        (full-text mirrors) > a ``fullText`` value that is itself a URL.
        The CORE ``fullText`` field normally carries raw text (or a
        placeholder for public API users), so it is only used when it looks
        like a link; the landing page is never returned — it is not full text.
        """
        candidates: list[str] = []
        if paper.pdf_url:
            candidates.append(paper.pdf_url)
        for evidence in paper.evidence_list:
            raw = evidence.raw_data
            if not isinstance(raw, dict):
                continue
            for key in ("downloadUrl", "sourceFulltextUrls", "fullText"):
                value = raw.get(key)
                if isinstance(value, list):
                    candidates.extend(v for v in value if isinstance(v, str))
                elif isinstance(value, str):
                    candidates.append(value)
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate.lower().startswith(("http://", "https://")):
                return candidate
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """CORE has no author-works operation (capability ``False``)."""
        raise NotSupportedError(
            "CORE does not support author-paper queries",
            source_name=self.name,
        )

    async def get_author_profile(self, author_name: str) -> Author | None:
        """CORE has no author profiles (capability ``False``)."""
        raise NotSupportedError(
            "CORE does not support author profiles",
            source_name=self.name,
        )

    async def get_citations(self, paper_id: str) -> list[Citation]:
        """CORE has no citation graph (capability ``False``)."""
        raise NotSupportedError(
            "CORE does not support citation queries",
            source_name=self.name,
        )
