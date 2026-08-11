"""OpenCitations (COCI) data source adapter — pure citation graph.

Uses the free, unauthenticated COCI API: ``https://opencitations.net``.
OpenCitations has **no metadata capability** — it only resolves DOI-keyed
citation edges, which is exactly what the ``citations`` operation needs:

- ``GET /index/coci/api/v1/citations/{doi}`` — who cites the given DOI
  (the DOI is the *cited* side of every returned edge)
- ``GET /index/coci/api/v1/references/{doi}`` — what the given DOI cites
  (the DOI is the *citing* side of every returned edge)

Both endpoints return a JSON array of edge objects
``{"oci", "citing", "cited", "creation", "timespan", "journal_sc",
"author_sc"}``.  The live API emits empty-string ``citing`` values for
incomplete records, so edges whose citing/cited DOI is missing or invalid
are skipped rather than surfaced as broken citation pairs.
"""

from __future__ import annotations

import logging
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

_API_BASE = "https://opencitations.net/index/coci/api/v1"

# Valid ``direction`` values for get_citations.
_DIRECTIONS = frozenset({"citing", "cited"})


class OpenCitationsSource(BaseSource):
    """OpenCitations (COCI) citation-graph source, keyed by DOI.

    Capabilities: only ``citations`` is supported (both ``citing`` — who
    cites the DOI — and ``cited`` — what the DOI cites — via the
    ``direction`` parameter); metadata ``search``/``get``, author
    operations and ``fulltext`` are unsupported.
    """

    name = "opencitations"
    source_type = SourceType.OPEN_CITATIONS
    capabilities = {
        **BaseSource.capabilities,
        # Current (pre-C1) long-form keys — explicit so the collector's
        # capability gate never mistakes OpenCitations for a metadata source.
        "search_papers": False,
        "get_paper_by_doi": False,
        "get_author_papers": False,
        "get_author_profile": False,
        "get_citations": True,
        # C1 contract keys (upgrade technical-design §1.1.1): the CLI source
        # registry drives off these short keys plus the new fulltext key.
        "search": False,
        "get": False,
        "citations": True,
        "fulltext": False,
    }

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        confidence: float = 0.85,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
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

    def _resolve_doi(self, paper_id: str | Paper) -> str:
        """Normalize the input to a bare DOI, or fail fast on invalid input."""
        if isinstance(paper_id, Paper):
            doi = paper_id.doi
            if not doi:
                raise ValueError("paper record has no DOI; OpenCitations is keyed by DOI")
        else:
            doi = paper_id
        normalized = normalize_doi(doi)
        if normalized is None:
            raise ValueError(f"invalid DOI format: {doi!r}")
        return normalized

    async def _get_json(self, path: str) -> Any:
        """GET the COCI edge array for *path*, mapping failures to domain errors."""
        client = await self._client()
        url = f"{_API_BASE}{path}"
        try:
            response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"OpenCitations request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    "OpenCitations rate limit exceeded",
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"OpenCitations request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "OpenCitations rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code == 404:
            # Unknown DOI / no citation edges recorded.
            return []
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"OpenCitations HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        try:
            return response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from OpenCitations: {exc}",
                source_name=self.name,
                raw_snippet=response.text[:300],
            ) from exc

    def _parse_edges(self, data: Any, *, url: str, doi: str) -> list[Citation]:
        """Convert the COCI edge array into Citation objects.

        Edges with a missing/invalid citing or cited DOI, and self-citation
        edges (citing == cited, which the Citation model rejects), are
        skipped defensively.
        """
        if not isinstance(data, list):
            raise ParseError(
                "Unexpected OpenCitations payload: expected a JSON edge array",
                source_name=self.name,
                raw_snippet=str(data)[:300],
            )
        citations: list[Citation] = []
        for edge in data:
            if not isinstance(edge, dict):
                continue
            citing_raw = edge.get("citing")
            cited_raw = edge.get("cited")
            if not isinstance(citing_raw, str) or not isinstance(cited_raw, str):
                continue
            citing = normalize_doi(citing_raw)
            cited = normalize_doi(cited_raw)
            if not citing or not cited or citing == cited:
                continue
            try:
                citations.append(
                    Citation(
                        citing_paper_id=citing,
                        cited_paper_id=cited,
                        evidence=self._evidence(url, raw=edge, source_id=doi),
                    )
                )
            except Exception as exc:
                logger.debug("Skip OpenCitations edge: %s", exc)
        return citations

    # ------------------------------------------------------------------
    # Citation queries
    # ------------------------------------------------------------------
    async def get_citations(
        self,
        paper_id: str | Paper,
        direction: str = "citing",
    ) -> list[Citation]:
        """Retrieve citation edges for a DOI-keyed paper.

        Args:
            paper_id: A DOI (``https://doi.org/`` / ``doi:`` prefixes are
                stripped) or a :class:`Paper` record carrying a DOI.
            direction: ``"citing"`` (who cites the DOI — the default) or
                ``"cited"`` (what the DOI cites).

        Returns:
            Citation edges as ``citing_paper_id`` / ``cited_paper_id`` DOI
            pairs, each with provenance evidence. Empty when COCI reports no
            edges for the DOI.

        Raises:
            ValueError: If *paper_id* is not a valid DOI (or a paper record
                without a DOI), or *direction* is not ``"citing"``/``"cited"``.
            SourceUnavailableError: If the source is unreachable or returns
                a non-2xx error (other than 404).
            RateLimitError: On HTTP 429.
            TimeoutError: On request timeouts.
            ParseError: If the response body cannot be parsed as a JSON
                edge array.
        """
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction must be 'citing' or 'cited', got {direction!r}")
        doi = self._resolve_doi(paper_id)
        endpoint = "citations" if direction == "citing" else "references"
        path = f"/{endpoint}/{quote(doi, safe='')}"
        data = await self._get_json(path)
        return self._parse_edges(data, url=f"{_API_BASE}{path}", doi=doi)

    # ------------------------------------------------------------------
    # Unsupported operations (OpenCitations exposes citation edges only)
    # ------------------------------------------------------------------
    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Not supported: OpenCitations has no metadata search."""
        raise NotSupportedError(
            "OpenCitations has no metadata search; it only resolves "
            "DOI-keyed citation edges via get_citations",
            source_name=self.name,
        )

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        """Not supported: OpenCitations has no metadata records."""
        raise NotSupportedError(
            "OpenCitations has no metadata records; it only resolves "
            "DOI-keyed citation edges via get_citations",
            source_name=self.name,
        )

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Not supported: OpenCitations exposes no author endpoints."""
        raise NotSupportedError(
            "OpenCitations exposes no author endpoints",
            source_name=self.name,
        )

    async def get_author_profile(self, author_name: str) -> Author | None:
        """Not supported: OpenCitations exposes no author endpoints."""
        raise NotSupportedError(
            "OpenCitations exposes no author endpoints",
            source_name=self.name,
        )
