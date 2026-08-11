"""WP6 source fetcher — OpenAlex / Semantic Scholar author data.

``SourceFetcher`` talks directly to the OpenAlex and Semantic Scholar REST
endpoints over the shared polite ``HTTPClient`` (global rate limiting /
retry / cache, default 1 req/s) and parses the payloads into the
:mod:`academic_intelligence.identity.models` shapes.

Three capabilities used by the resolver:

- ``fetch_profile(author_id, source)`` — the full source profile plus
  representative works sorted by citation count desc (branch A /
  ``profile``);
- ``search(name, source, limit)`` — same-name candidates with
  institution / interests / h-index (branch B / ``search``);
- ``works_context(author_id, source, limit)`` — coauthors / active years /
  venues extracted from the candidate's works (disambiguation features for
  the comparison table).

Everything is a pure GET with polite rate limiting; no writes, no
credentials, no paywall content.  The parse helpers are module-level pure
functions so unit tests can exercise them without HTTP.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field

from academic_intelligence.core.exceptions import (
    ParseError,
    RateLimitError,
    SourceUnavailableError,
    TimeoutError,
)
from academic_intelligence.core.models import normalize_doi
from academic_intelligence.identity.exceptions import IdentitySourceError
from academic_intelligence.identity.models import (
    AuthorCandidate,
    AuthorProfile,
    RepresentativePaper,
    evidence_entry,
)
from academic_intelligence.sources.base import (
    is_rate_limit_status,
    retry_after_from_error,
)
from academic_intelligence.utils.http import HTTPClient

logger = logging.getLogger(__name__)

_OPENALEX_BASE = "https://api.openalex.org"
_S2_BASE = "https://api.semanticscholar.org/graph/v1"

#: How many works are pulled for the profile representative list / the
#: candidate context extraction (kept small: polite, and only the top
#: citation-sorted slice is used anyway).
_PROFILE_WORKS = 10
_CONTEXT_WORKS = 25


class WorksContext(BaseModel):
    """Disambiguation context extracted from a candidate's works.

    ``arxiv_ids`` / ``dois`` / ``titles`` feed the resolver's paper-match
    check (the candidate authored the very paper being resolved — identity-
    grade evidence), the rest feed the disambiguator features.
    """

    coauthors: list[str] = Field(default_factory=list)
    active_years: list[int] = Field(default_factory=list)
    venues: list[str] = Field(default_factory=list)
    arxiv_ids: list[str] = Field(default_factory=list)
    dois: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Small parsing helpers (pure functions)
# ---------------------------------------------------------------------------


def _bare_id(value: Any) -> str | None:
    """Strip an OpenAlex entity URL down to its bare id (``A1`` / ``W1``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.rstrip("/").split("/")[-1]


def _normalize_arxiv_id(value: str) -> str:
    """Normalize an arXiv id to the bare ``YYYY.NNNNN`` form.

    Strips the ``arXiv:`` prefix, whitespace and the version suffix
    (``arXiv:2403.05525v1`` → ``2403.05525``) so ids from different
    sources compare equal.
    """
    cleaned = value.strip().lower()
    if cleaned.startswith("arxiv:"):
        cleaned = cleaned[len("arxiv:") :].strip()
    # Drop a trailing version suffix (e.g. ``v1`` / ``v2``).
    if re.search(r"v\d+$", cleaned):
        cleaned = re.sub(r"v\d+$", "", cleaned)
    return cleaned


def _first_dict(values: Any) -> dict[str, Any] | None:
    """Return the first dict of *values*, or ``None``."""
    if isinstance(values, list) and values and isinstance(values[0], dict):
        return values[0]
    return None


def _list_display_names(values: Any) -> list[str]:
    """Extract ``display_name`` strings from an OpenAlex list-of-dicts."""
    out: list[str] = []
    for item in values or []:
        if isinstance(item, dict) and item.get("display_name"):
            out.append(str(item["display_name"]))
    return out


def _author_affiliation_openalex(item: dict[str, Any]) -> str | None:
    """Institution name: ``last_known_institutions`` list or singular."""
    institutions = _list_display_names(item.get("last_known_institutions"))
    if institutions:
        return ", ".join(institutions)
    singular = item.get("last_known_institution")
    if isinstance(singular, dict) and singular.get("display_name"):
        return str(singular["display_name"])
    return None


def parse_openalex_author(item: dict[str, Any]) -> AuthorCandidate:
    """Parse one OpenAlex ``/authors`` result into an :class:`AuthorCandidate`."""
    author_id = _bare_id(item.get("id"))
    summary = item.get("summary_stats") or {}
    h_index = summary.get("h_index") if isinstance(summary, dict) else None
    cited = item.get("cited_by_count")
    if not isinstance(cited, (int, float)):
        cited = summary.get("cited_by_count") if isinstance(summary, dict) else None
    profile_url = item.get("id")
    return AuthorCandidate(
        candidate_id=f"openalex:{author_id}" if author_id else f"openalex:{item.get('id')}",
        source="openalex",
        name=str(item.get("display_name") or "Unknown"),
        affiliation=_author_affiliation_openalex(item),
        interests=_list_display_names(item.get("interests")),
        h_index=int(h_index) if isinstance(h_index, (int, float)) else None,
        citations=int(cited) if isinstance(cited, (int, float)) else None,
        paper_count=item.get("works_count"),
        profile_url=profile_url if isinstance(profile_url, str) else None,
        evidence=[
            evidence_entry(
                "openalex",
                profile_url if isinstance(profile_url, str) else _OPENALEX_BASE,
                source_id=author_id,
            )
        ],
    )


def parse_openalex_work(item: dict[str, Any]) -> RepresentativePaper:
    """Parse one OpenAlex work result into a :class:`RepresentativePaper`."""
    primary = item.get("primary_location") or {}
    source = primary.get("source") or {}
    venue = source.get("display_name") if isinstance(source, dict) else None
    return RepresentativePaper(
        title=str(item.get("title") or item.get("display_name") or "Untitled"),
        year=item.get("publication_year"),
        cited_by_count=int(item.get("cited_by_count") or 0),
        venue=str(venue) if venue else None,
        work_id=_bare_id(item.get("id")),
        doi=item.get("doi"),
    )


def parse_s2_author(item: dict[str, Any]) -> AuthorCandidate:
    """Parse one Semantic Scholar author result into an :class:`AuthorCandidate`."""
    author_id = item.get("authorId")
    affiliations = item.get("affiliations") or []
    affiliation = affiliations[0] if affiliations else None
    if isinstance(affiliation, dict):
        affiliation = affiliation.get("name")
    profile_url = item.get("url") or (
        f"https://www.semanticscholar.org/author/{author_id}" if author_id else None
    )
    return AuthorCandidate(
        candidate_id=f"s2:{author_id}" if author_id else f"s2:{item.get('name')}",
        source="s2",
        name=str(item.get("name") or "Unknown"),
        affiliation=str(affiliation) if affiliation else None,
        h_index=item.get("hIndex"),
        citations=item.get("citationCount"),
        paper_count=item.get("paperCount"),
        profile_url=profile_url if isinstance(profile_url, str) else None,
        evidence=[
            evidence_entry(
                "s2",
                profile_url if isinstance(profile_url, str) else _S2_BASE,
                source_id=str(author_id) if author_id else None,
            )
        ],
    )


def parse_s2_work(item: dict[str, Any]) -> RepresentativePaper:
    """Parse one Semantic Scholar paper result into a :class:`RepresentativePaper`."""
    external = item.get("externalIds") or {}
    return RepresentativePaper(
        title=str(item.get("title") or "Untitled"),
        year=item.get("year"),
        cited_by_count=int(item.get("citationCount") or 0),
        venue=str(item.get("venue")) if item.get("venue") else None,
        work_id=item.get("paperId"),
        doi=external.get("DOI") if isinstance(external, dict) else None,
    )


# ---------------------------------------------------------------------------
# SourceFetcher
# ---------------------------------------------------------------------------


class SourceFetcher:
    """Polite OpenAlex / Semantic Scholar fetcher for author identity.

    Args:
        http_client: Optional shared ``HTTPClient``; when omitted the
            fetcher creates (and owns) a polite client and :meth:`close`
            releases it.  When given, the caller owns its lifecycle.
        email: Optional OpenAlex polite-pool ``mailto`` value.
        api_key: Optional Semantic Scholar API key (``x-api-key`` header,
            reduces 429s).
    """

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        email: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self._client: HTTPClient | None = None
        self.email = email
        self.api_key = api_key

    # -- lifecycle ----------------------------------------------------------

    async def _get(self) -> HTTPClient:
        if self._http is not None:
            return self._http
        if self._client is None:
            self._client = HTTPClient()
            await self._client.connect()
        return self._client

    async def close(self) -> None:
        """Close the owned HTTP client (a shared client is never closed here)."""
        if self._owns_client and self._client is not None:
            await self._client.close()
            self._client = None

    async def __aenter__(self) -> SourceFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- HTTP plumbing -------------------------------------------------------

    def _openalex_params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(extra or {})
        if self.email:
            params["mailto"] = self.email
        return params

    def _s2_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    async def _get_json(
        self,
        source: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET + JSON-decode with source-consistent error mapping (WP6)."""
        client = await self._get()
        try:
            response = await client.get(url, headers=headers, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"{source} request timed out: {exc}",
                source_name=source,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    f"{source} rate limit exceeded",
                    source_name=source,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"{source} request failed: {exc}",
                source_name=source,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                f"{source} rate limit exceeded",
                source_name=source,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"{source} HTTP {response.status_code}",
                source_name=source,
                context={"body": response.text[:500]},
            )
        try:
            return response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from {source}: {exc}",
                source_name=source,
                raw_snippet=response.text[:300],
            ) from exc

    # -- OpenAlex ------------------------------------------------------------

    async def _openalex_profile(self, author_id: str) -> AuthorProfile | None:
        author_url = f"{_OPENALEX_BASE}/authors/{author_id}"
        data = await self._get_json(
            "openalex", author_url, params=self._openalex_params()
        )
        if not isinstance(data, dict) or not data:
            return None
        candidate = parse_openalex_author(data)
        works: list[RepresentativePaper] = []
        works_url = f"{_OPENALEX_BASE}/works"
        works_data = await self._get_json(
            "openalex",
            works_url,
            params=self._openalex_params(
                {
                    "filter": f"author.id:{author_id}",
                    "sort": "cited_by_count:desc",
                    "per_page": _PROFILE_WORKS,
                }
            ),
        )
        for item in (works_data or {}).get("results") or []:
            if isinstance(item, dict):
                try:
                    works.append(parse_openalex_work(item))
                except Exception as exc:
                    logger.debug("Skip OpenAlex work: %s", exc)
        return AuthorProfile(
            name=candidate.name,
            author_id=author_id,
            source="openalex",
            affiliation=candidate.affiliation,
            homepage=data.get("homepage"),
            h_index=candidate.h_index,
            citations=candidate.citations,
            paper_count=candidate.paper_count,
            interests=candidate.interests,
            profile_url=candidate.profile_url,
            representative_papers=works,
            evidence=candidate.evidence,
        )

    async def _openalex_search(
        self,
        name: str,
        limit: int,
    ) -> list[AuthorCandidate]:
        url = f"{_OPENALEX_BASE}/authors"
        data = await self._get_json(
            "openalex",
            url,
            params=self._openalex_params(
                {"search": name, "per_page": min(limit, 50)}
            ),
        )
        candidates: list[AuthorCandidate] = []
        for item in (data or {}).get("results") or []:
            if isinstance(item, dict):
                try:
                    candidates.append(parse_openalex_author(item))
                except Exception as exc:
                    logger.debug("Skip OpenAlex author: %s", exc)
        return candidates

    async def _openalex_works_context(
        self,
        author_id: str,
        limit: int,
    ) -> WorksContext:
        url = f"{_OPENALEX_BASE}/works"
        data = await self._get_json(
            "openalex",
            url,
            params=self._openalex_params(
                {"filter": f"author.id:{author_id}", "per_page": min(limit, 50)}
            ),
        )
        context = WorksContext()
        for item in (data or {}).get("results") or []:
            if not isinstance(item, dict):
                continue
            year = item.get("publication_year")
            if isinstance(year, int):
                context.active_years.append(year)
            primary = item.get("primary_location") or {}
            source = primary.get("source") or {}
            venue = source.get("display_name") if isinstance(source, dict) else None
            if isinstance(venue, str) and venue:
                context.venues.append(venue)
            for authorship in item.get("authorships") or []:
                author = (authorship or {}).get("author") or {}
                display = author.get("display_name")
                if isinstance(display, str) and display:
                    context.coauthors.append(display)
            if isinstance(item.get("title"), str) and item["title"]:
                context.titles.append(item["title"])
            ids = item.get("ids") or {}
            arxiv = ids.get("arxiv")
            if isinstance(arxiv, str):
                context.arxiv_ids.append(_normalize_arxiv_id(arxiv))
            doi = ids.get("doi")
            normalized_doi = normalize_doi(doi) if isinstance(doi, str) else None
            if normalized_doi:
                context.dois.append(normalized_doi.lower())
        return context

    # -- Semantic Scholar -----------------------------------------------------

    async def _s2_profile(self, author_id: str) -> AuthorProfile | None:
        author_url = f"{_S2_BASE}/author/{author_id}"
        fields = (
            "authorId,name,affiliations,homepage,hIndex,citationCount,"
            "paperCount,url"
        )
        data = await self._get_json(
            "s2", author_url, params={"fields": fields}, headers=self._s2_headers()
        )
        if not isinstance(data, dict) or not data:
            return None
        candidate = parse_s2_author(data)
        works: list[RepresentativePaper] = []
        papers_data = await self._get_json(
            "s2",
            f"{_S2_BASE}/author/{author_id}/papers",
            params={"limit": _PROFILE_WORKS, "fields": "paperId,title,year,citationCount,venue,externalIds"},
            headers=self._s2_headers(),
        )
        for item in (papers_data or {}).get("data") or []:
            if isinstance(item, dict):
                try:
                    works.append(parse_s2_work(item))
                except Exception as exc:
                    logger.debug("Skip S2 paper: %s", exc)
        works.sort(key=lambda w: w.cited_by_count, reverse=True)
        return AuthorProfile(
            name=candidate.name,
            author_id=author_id,
            source="s2",
            affiliation=candidate.affiliation,
            homepage=data.get("homepage"),
            h_index=candidate.h_index,
            citations=candidate.citations,
            paper_count=candidate.paper_count,
            interests=[],
            profile_url=candidate.profile_url,
            representative_papers=works,
            evidence=candidate.evidence,
        )

    async def _s2_search(
        self,
        name: str,
        limit: int,
    ) -> list[AuthorCandidate]:
        fields = (
            "authorId,name,affiliations,homepage,hIndex,citationCount,"
            "paperCount,url"
        )
        data = await self._get_json(
            "s2",
            f"{_S2_BASE}/author/search",
            params={
                "query": name,
                "limit": min(limit, 100),
                "fields": fields,
            },
            headers=self._s2_headers(),
        )
        candidates: list[AuthorCandidate] = []
        for item in (data or {}).get("data") or []:
            if isinstance(item, dict):
                try:
                    candidates.append(parse_s2_author(item))
                except Exception as exc:
                    logger.debug("Skip S2 author: %s", exc)
        return candidates

    async def _s2_works_context(
        self,
        author_id: str,
        limit: int,
    ) -> WorksContext:
        data = await self._get_json(
            "s2",
            f"{_S2_BASE}/author/{author_id}/papers",
            params={
                "limit": min(limit, 100),
                "fields": (
                    "paperId,title,year,citationCount,venue,authors,externalIds"
                ),
            },
            headers=self._s2_headers(),
        )
        context = WorksContext()
        for item in (data or {}).get("data") or []:
            if not isinstance(item, dict):
                continue
            year = item.get("year")
            if isinstance(year, int):
                context.active_years.append(year)
            if isinstance(item.get("venue"), str) and item["venue"]:
                context.venues.append(item["venue"])
            if isinstance(item.get("title"), str) and item["title"]:
                context.titles.append(item["title"])
            external = item.get("externalIds") or {}
            arxiv = external.get("ArXiv") if isinstance(external, dict) else None
            if isinstance(arxiv, str):
                context.arxiv_ids.append(_normalize_arxiv_id(arxiv))
            doi = external.get("DOI") if isinstance(external, dict) else None
            normalized_doi = normalize_doi(doi) if isinstance(doi, str) else None
            if normalized_doi:
                context.dois.append(normalized_doi.lower())
            for author in item.get("authors") or []:
                name = author.get("name") if isinstance(author, dict) else None
                if isinstance(name, str) and name:
                    context.coauthors.append(name)
        return context

    # -- public API -----------------------------------------------------------

    async def fetch_profile(self, author_id: str, source: str) -> AuthorProfile | None:
        """Fetch the full source profile for one authority id.

        Args:
            author_id: OpenAlex author id (``A...``) or Semantic Scholar
                author id.
            source: ``"openalex"`` or ``"s2"``.

        Returns:
            The parsed :class:`AuthorProfile` (with representative works
            sorted by citation count desc), or ``None`` when the id is
            unknown (source 404).

        Raises:
            IdentitySourceError: On transient/unavailable source failures
                (the underlying :class:`SourceError` is the ``__cause__``).
        """
        try:
            if source == "openalex":
                return await self._openalex_profile(author_id)
            if source == "s2":
                return await self._s2_profile(author_id)
            raise IdentitySourceError(f"unsupported author source: {source!r}")
        except IdentitySourceError:
            raise
        except Exception as exc:
            raise IdentitySourceError(
                f"failed to fetch {source} author profile for {author_id!r}"
            ) from exc

    async def fetch_by_orcid(self, orcid: str) -> AuthorProfile | None:
        """Resolve an ORCID to an OpenAlex author profile.

        OpenAlex indexes ORCIDs, so an ``AuthorRef`` carrying an ORCID can
        still reach a full profile: ``/authors?filter=orcid:...`` first, then
        the profile of the matching OpenAlex author.  Returns ``None`` when
        OpenAlex has no author for the ORCID.
        """
        try:
            data = await self._get_json(
                "openalex",
                f"{_OPENALEX_BASE}/authors",
                params=self._openalex_params({"filter": f"orcid:{orcid}", "per_page": 1}),
            )
            results = (data or {}).get("results") or []
            if not results or not isinstance(results[0], dict):
                return None
            author_id = _bare_id(results[0].get("id"))
            if not author_id:
                return None
            return await self._openalex_profile(author_id)
        except Exception as exc:
            raise IdentitySourceError(
                f"failed to resolve ORCID {orcid!r} via OpenAlex"
            ) from exc

    async def search(
        self,
        name: str,
        source: str,
        limit: int = 25,
    ) -> list[AuthorCandidate]:
        """Search same-name candidates from one source."""
        try:
            if source == "openalex":
                return await self._openalex_search(name, limit)
            if source == "s2":
                return await self._s2_search(name, limit)
            raise IdentitySourceError(f"unsupported author source: {source!r}")
        except IdentitySourceError:
            raise
        except Exception as exc:
            raise IdentitySourceError(
                f"failed to search {source} authors for {name!r}"
            ) from exc

    async def works_context(
        self,
        author_id: str,
        source: str,
        limit: int = _CONTEXT_WORKS,
    ) -> WorksContext:
        """Extract the disambiguation context from the author's works.

        Returns the coauthors / active years / venues plus the works'
        identifiers (arxiv ids / dois / titles, used by the resolver's
        paper-match check).  Failure is fail-soft: an empty context is
        returned so a missing context never blocks the resolution.
        """
        try:
            if source == "openalex":
                return await self._openalex_works_context(author_id, limit)
            if source == "s2":
                return await self._s2_works_context(author_id, limit)
            return WorksContext()
        except Exception as exc:
            logger.warning("works context for %s:%s failed: %s", source, author_id, exc)
            return WorksContext()
