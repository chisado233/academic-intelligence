"""Unpaywall data source adapter (legal OA full-text locator).

Uses the public Unpaywall API: ``GET https://api.unpaywall.org/v2/{doi}?email={email}``
(free tier 100k requests/day).

Unpaywall is not a metadata search engine: given a DOI it reports where a
*legal* open-access copy of the paper lives (``best_oa_location`` /
``oa_locations[]`` / ``is_oa``). The adapter's core value is therefore
:meth:`UnpaywallSource.get_fulltext` — the ordered OA link list — while
:meth:`UnpaywallSource.get_paper_by_doi` returns a minimal
:class:`~academic_intelligence.core.models.Paper` record that carries the OA
location data in its evidence for :meth:`get_fulltext` to reuse.

The API requires a contact email (polite-pool etiquette). It is supplied via
the ``email`` constructor argument or the ``UNPAYWALL_EMAIL`` environment
variable; without one every request is rejected (HTTP 401) and the adapter
fails fast with :class:`~academic_intelligence.core.exceptions.AuthenticationError`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

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

_API_BASE = "https://api.unpaywall.org/v2"
_EMAIL_ENV = "UNPAYWALL_EMAIL"


@dataclass(frozen=True)
class OALocation:
    """One legal open-access location of a paper, as reported by Unpaywall.

    Attributes:
        url: Landing or full-text URL of the OA copy.
        pdf_url: Direct PDF URL when the location exposes one.
        host_type: Who hosts the copy (``"publisher"`` / ``"repository"``).
        license: License of the copy (e.g. ``"cc-by"``) when published.
        version: Manuscript version (``"publishedVersion"`` /
            ``"acceptedVersion"`` / ``"submittedVersion"``).
    """

    url: str
    pdf_url: str | None = None
    host_type: str | None = None
    license: str | None = None
    version: str | None = None


def _http_url(value: Any) -> str | None:
    """Return *value* as a URL when it is an absolute HTTP(S) string."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped.lower().startswith(("http://", "https://")):
        return stripped
    return None


class UnpaywallSource(BaseSource):
    """Unpaywall OA full-text locator (get-by-DOI).

    Capabilities: ``search``/``citations``/author operations are unsupported
    (Unpaywall has no metadata search and no citation graph); ``get`` and
    ``fulltext`` are its reason to exist.
    """

    name = "unpaywall"
    source_type = SourceType.UNPAYWALL
    capabilities = {
        **BaseSource.capabilities,
        # Current (pre-C1) long-form keys — explicit so the collector's
        # capability gate never mistakes Unpaywall for a full source.
        "search_papers": False,
        "get_paper_by_doi": True,
        "get_author_papers": False,
        "get_author_profile": False,
        "get_citations": False,
        # C1 contract keys (upgrade technical-design §1.1.1): the CLI source
        # registry drives off these short keys plus the new fulltext key.
        "search": False,
        "get": True,
        "citations": False,
        "fulltext": True,
    }

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        email: str | None = None,
        confidence: float = 0.85,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self.email = email or os.environ.get(_EMAIL_ENV)
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

    def _require_email(self) -> str:
        if not self.email:
            raise AuthenticationError(
                "Unpaywall requires an email address: pass email=... or set "
                f"the {_EMAIL_ENV} environment variable",
                source_name=self.name,
            )
        return self.email

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

    async def _get_json(self, doi: str) -> dict[str, Any] | None:
        """GET the Unpaywall record for a normalized DOI.

        Returns ``None`` on HTTP 404 (unknown DOI). Every other non-2xx
        status is mapped to the matching domain error.
        """
        email = self._require_email()
        client = await self._client()
        url = f"{_API_BASE}/{quote(doi, safe='')}"
        try:
            response = await client.get(url, params={"email": email})
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Unpaywall request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    "Unpaywall rate limit exceeded",
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"Unpaywall request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "Unpaywall rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        # Unpaywall rejects email problems with 401 or 422 in practice (the
        # live API answers a missing/invalid email with 422 "Email address
        # required in API call"; the WP2b facts sheet names 401). Both are
        # configuration errors, not source outages.
        if response.status_code in (401, 422):
            raise AuthenticationError(
                "Unpaywall rejected the request "
                f"(HTTP {response.status_code}): the configured email is "
                f"missing or invalid — pass email=... or set the "
                f"{_EMAIL_ENV} environment variable",
                source_name=self.name,
                context={"body": response.text[:300]},
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"Unpaywall HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        try:
            data = response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from Unpaywall: {exc}",
                source_name=self.name,
                raw_snippet=response.text[:300],
            ) from exc
        if not isinstance(data, dict):
            raise ParseError(
                "Unexpected Unpaywall payload: expected a JSON object",
                source_name=self.name,
                raw_snippet=response.text[:300],
            )
        return data

    def _parse_locations(self, data: dict[str, Any]) -> list[OALocation]:
        """Build the ordered OA location list — ``best_oa_location`` first."""
        locations: list[dict[str, Any]] = []
        best = data.get("best_oa_location")
        if isinstance(best, dict) and best:
            locations.append(best)
        for loc in data.get("oa_locations") or []:
            if isinstance(loc, dict) and loc not in locations:
                locations.append(loc)

        parsed: list[OALocation] = []
        for loc in locations:
            url = _http_url(loc.get("url"))
            if url is None:
                continue
            # The live API calls the direct PDF link ``url_for_pdf``; the
            # verified-facts sheet in the WP2b dispatch names it ``pdf_url``.
            # Accept both spellings defensively.
            pdf = _http_url(loc.get("url_for_pdf") or loc.get("pdf_url"))
            parsed.append(
                OALocation(
                    url=url,
                    pdf_url=pdf,
                    host_type=loc.get("host_type"),
                    license=loc.get("license"),
                    version=loc.get("version"),
                )
            )
        return parsed

    def _parse_paper(self, doi: str, data: dict[str, Any]) -> Paper:
        title = str(data.get("title") or "").strip() or "Untitled"
        locations = self._parse_locations(data)
        pdf_url = next((loc.pdf_url for loc in locations if loc.pdf_url), None)
        landing = next((loc.url for loc in locations), None)
        raw_locations = [
            loc
            for loc in (data.get("oa_locations") or [])
            if isinstance(loc, dict)
        ]
        return Paper(
            title=title,
            doi=doi,
            url=landing,
            pdf_url=pdf_url,
            evidence_list=[
                self._evidence(
                    f"{_API_BASE}/{quote(doi, safe='')}",
                    raw={
                        "is_oa": bool(data.get("is_oa")),
                        "best_oa_location": data.get("best_oa_location"),
                        "oa_locations": raw_locations,
                    },
                    source_id=doi,
                )
            ],
        )

    def _evidence_locations(self, paper: Paper) -> list[OALocation] | None:
        """Reuse OA locations already carried by the paper's Unpaywall evidence.

        Returns ``None`` when the paper carries no reusable Unpaywall
        evidence (the caller should query the API instead); ``[]`` when the
        evidence says the paper is not open access.
        """
        for evidence in paper.evidence_list:
            if evidence.source is not SourceType.UNPAYWALL:
                continue
            raw = evidence.raw_data or {}
            if raw.get("is_oa") is False:
                return []
            # Raw data present but empty means the evidence has no OA info to
            # reuse — fall through to a fresh API query.
            if raw:
                return self._parse_locations(raw)
            return None
        return None

    # ------------------------------------------------------------------
    # Paper queries
    # ------------------------------------------------------------------
    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch the minimal OA record for a DOI.

        Unpaywall has no metadata search: the returned :class:`Paper`
        carries the DOI (and the title Unpaywall reports, when present),
        with the full OA location data attached as raw evidence so
        :meth:`get_fulltext` can reuse it without a second request.

        Args:
            doi: Digital Object Identifier (bare or with a ``doi.org`` prefix).

        Returns:
            The minimal :class:`Paper` or ``None`` when the DOI is unknown.
        """
        cleaned = normalize_doi(doi)
        if cleaned is None:
            return None
        data = await self._get_json(cleaned)
        if data is None:
            return None
        return self._parse_paper(cleaned, data)

    async def get_fulltext(self, paper: Paper) -> list[OALocation]:
        """Return the legal OA locations of *paper*, best first.

        The DOI is taken from ``paper.doi``. When the paper already carries
        Unpaywall evidence (e.g. produced by :meth:`get_paper_by_doi`) that
        raw data is reused instead of re-querying the API.

        Args:
            paper: The paper whose OA copies are wanted.

        Returns:
            An ordered list of :class:`OALocation` (``best_oa_location``
            first) — empty when the paper has no DOI or Unpaywall reports no
            open-access copy (``is_oa`` false / no locations).
        """
        doi = paper.doi
        if doi is None:
            return []
        cached = self._evidence_locations(paper)
        if cached is not None:
            return cached
        data = await self._get_json(doi)
        if data is None:
            return []
        if not data.get("is_oa"):
            return []
        return self._parse_locations(data)

    # ------------------------------------------------------------------
    # Unsupported operations (Unpaywall has neither search nor citations)
    # ------------------------------------------------------------------
    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Not supported: Unpaywall only locates OA full text for a DOI."""
        raise NotSupportedError(
            "Unpaywall has no metadata search; use get_paper_by_doi with a "
            "known DOI instead",
            source_name=self.name,
        )

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Not supported: Unpaywall exposes no author endpoints."""
        raise NotSupportedError(
            "Unpaywall has no author endpoints",
            source_name=self.name,
        )

    async def get_author_profile(self, author_name: str) -> Author | None:
        """Not supported: Unpaywall exposes no author endpoints."""
        raise NotSupportedError(
            "Unpaywall has no author endpoints",
            source_name=self.name,
        )

    async def get_citations(self, paper_id: str) -> list[Citation]:
        """Not supported: Unpaywall is a full-text locator, not a citation graph."""
        raise NotSupportedError(
            "Unpaywall does not provide citation data",
            source_name=self.name,
        )
