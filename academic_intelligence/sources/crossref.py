"""Crossref data source adapter.

Uses the public Crossref REST API (works endpoint, polite pool):
https://api.crossref.org

Polite-pool etiquette: requests should carry a ``mailto`` parameter
identifying the requester, so Crossref can contact you if your queries
abuse the API.  The adapter takes it from ``CrossrefSource(mailto=...)``
with an environment fallback (``CROSSREF_MAILTO``).

Metadata-only source: paper search and DOI lookup are supported; author
lookups and the citation graph are not (upgrade technical-design §1.1.1
C1 revision — those operations raise :class:`NotSupportedError` and the
``capabilities`` ClassVar declares them ``False``).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from academic_intelligence.core.exceptions import (
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
from academic_intelligence.utils.publisher_map import publisher_from_doi

logger = logging.getLogger(__name__)

_API_BASE = "https://api.crossref.org"

_DOI_WRAPPERS = ("https://doi.org/", "http://doi.org/", "doi:")


def _clean_doi(doi: str) -> str | None:
    """Strip DOI wrappers; return the bare DOI or ``None`` when empty."""
    cleaned = doi.strip()
    lowered = cleaned.lower()
    for wrapper in _DOI_WRAPPERS:
        if lowered.startswith(wrapper):
            cleaned = cleaned[len(wrapper) :].strip()
            break
    return cleaned or None


def _author_name(item: dict[str, Any]) -> str | None:
    """Best-effort author display name from a Crossref ``author`` entry."""
    given = item.get("given")
    family = item.get("family")
    if isinstance(given, str) and isinstance(family, str):
        combined = f"{given} {family}".strip()
        if combined:
            return combined
    literal_name = item.get("name")
    if isinstance(literal_name, str) and literal_name.strip():
        return literal_name.strip()
    return None


def _affiliation(item: dict[str, Any]) -> str | None:
    """First affiliation name of a Crossref ``author`` entry, if any."""
    for aff in item.get("affiliation") or []:
        if isinstance(aff, dict):
            aff_name = aff.get("name")
            if isinstance(aff_name, str) and aff_name.strip():
                return aff_name.strip()
    return None


def _issued_year(data: dict[str, Any]) -> int | None:
    """Publication year from Crossref date structures (earliest source wins)."""
    for key in ("published-print", "published-online", "issued", "created"):
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        parts = entry.get("date-parts")
        if not isinstance(parts, list) or not parts:
            continue
        first = parts[0]
        if isinstance(first, list) and first and isinstance(first[0], int):
            return first[0]
    return None


class CrossrefSource(BaseSource):
    """Crossref REST API source (works endpoint, polite pool)."""

    name = "crossref"
    source_type = SourceType.CROSSREF
    # C1 contract: author/citation operations are declared unsupported.
    capabilities = {
        **BaseSource.capabilities,
        "search": True,
        "get": True,
        "citations": False,
        "fulltext": False,
        "get_author_papers": False,
        "get_author_profile": False,
        "get_citations": False,
    }

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        mailto: str | None = None,
        confidence: float = 0.90,
    ) -> None:
        """Initialize the Crossref adapter.

        Args:
            http_client: Shared HTTP client (owned by the caller when given).
            mailto: Polite-pool contact email; falls back to the
                ``CROSSREF_MAILTO`` environment variable.
            confidence: Evidence confidence for records from this source.
        """
        self._http = http_client
        self._owns_client = http_client is None
        self.mailto = mailto or os.environ.get("CROSSREF_MAILTO")
        self.confidence = confidence

    async def _client(self) -> HTTPClient:
        if self._http is None:
            self._http = HTTPClient()
            await self._http.connect()
        return self._http

    async def close(self) -> None:
        """Close the owned HTTP client (shared clients stay open)."""
        if self._owns_client and self._http is not None:
            await self._http.close()
            self._http = None

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = dict(extra or {})
        if self.mailto:
            params["mailto"] = self.mailto
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

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        client = await self._client()
        url = f"{_API_BASE}{path}"
        try:
            response = await client.get(url, params=self._params(params))
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Crossref request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    "Crossref rate limit exceeded",
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"Crossref request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "Crossref rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"Crossref HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        try:
            return response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from Crossref: {exc}",
                source_name=self.name,
                raw_snippet=response.text[:300],
            ) from exc

    def _parse_paper(self, data: dict[str, Any]) -> Paper:
        """Parse one Crossref ``works`` item into a :class:`Paper`.

        Publisher resolution is two-level (technical-design §3.1): the API's
        own ``publisher`` field wins; the static DOI-prefix map
        (:mod:`academic_intelligence.utils.publisher_map`) is the fallback
        when the field is absent.
        """
        titles = data.get("title")
        if isinstance(titles, list) and titles:
            title = str(titles[0]).strip()
        elif isinstance(titles, str):
            title = titles.strip()
        else:
            title = ""
        if not title:
            title = "Untitled"

        authors: list[AuthorRef] = []
        for item in data.get("author") or []:
            if not isinstance(item, dict):
                continue
            name = _author_name(item)
            if not name:
                continue
            authors.append(
                AuthorRef(
                    name=name,
                    position=len(authors) + 1,
                    affiliation=_affiliation(item),
                )
            )

        containers = data.get("container-title")
        venue: str | None = None
        if isinstance(containers, list) and containers:
            venue = str(containers[0])
        elif isinstance(containers, str):
            venue = containers

        doi = normalize_doi(data.get("DOI"))

        api_publisher = data.get("publisher")
        publisher: str | None = None
        if isinstance(api_publisher, str) and api_publisher.strip():
            publisher = api_publisher.strip()
        else:
            publisher = publisher_from_doi(doi)

        url: str | None = None
        pdf_url: str | None = None
        for link in data.get("link") or []:
            if not isinstance(link, dict):
                continue
            href = link.get("URL")
            if not isinstance(href, str) or not href.startswith("http"):
                continue
            if url is None:
                url = href
            content_type = str(link.get("content-type") or "").lower()
            if "pdf" in content_type and pdf_url is None:
                pdf_url = href
        if url is None and doi:
            url = f"https://doi.org/{doi}"

        citations = data.get("is-referenced-by-count")
        if not isinstance(citations, int):
            citations = None

        source_url = f"https://doi.org/{doi}" if doi else f"{_API_BASE}/works"
        return Paper(
            title=title,
            authors=authors,
            year=_issued_year(data),
            venue=venue,
            doi=doi,
            url=url,
            pdf_url=pdf_url,
            citations=citations,
            publisher=publisher,
            evidence_list=[self._evidence(source_url, raw=data, source_id=doi)],
        )

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Search Crossref works with a bibliographic query."""
        q = query.strip()
        if not q:
            return []
        data = await self._get_json(
            "/works",
            params={
                "query.bibliographic": q,
                "rows": min(max(limit, 1), 1000),
            },
        )
        if not isinstance(data, dict):
            return []
        papers: list[Paper] = []
        message = data.get("message")
        items = message.get("items") if isinstance(message, dict) else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            try:
                papers.append(self._parse_paper(item))
            except Exception as exc:
                logger.debug("Skip Crossref item: %s", exc)
            if len(papers) >= limit:
                break
        return papers

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch a single Crossref work by DOI.

        Returns ``None`` when the DOI is malformed or Crossref has no record
        for it (HTTP 404).
        """
        cleaned = _clean_doi(doi)
        if cleaned is None or normalize_doi(cleaned) is None:
            return None
        data = await self._get_json(f"/works/{quote(cleaned, safe='')}")
        if not isinstance(data, dict):
            return None
        message = data.get("message")
        if not isinstance(message, dict):
            return None
        return self._parse_paper(message)

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Not supported: Crossref has no author-papers endpoint."""
        raise NotSupportedError(
            "Crossref does not support author paper lookups",
            source_name=self.name,
        )

    async def get_author_profile(self, author_name: str) -> Author | None:
        """Not supported: Crossref has no author profile endpoint."""
        raise NotSupportedError(
            "Crossref does not support author profile lookups",
            source_name=self.name,
        )

    async def get_citations(self, paper_id: str) -> list[Citation]:
        """Not supported: Crossref exposes citation counts, not the graph."""
        raise NotSupportedError(
            "Crossref does not support citation graph lookups",
            source_name=self.name,
        )


__all__ = ["CrossrefSource"]
