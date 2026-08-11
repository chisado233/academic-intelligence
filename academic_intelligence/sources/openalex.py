"""OpenAlex data source adapter.

Uses the public OpenAlex API: https://api.openalex.org
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from academic_intelligence.core.exceptions import (
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
from academic_intelligence.utils.names import normalize_author_tokens

logger = logging.getLogger(__name__)

_API_BASE = "https://api.openalex.org"

# Author lookups fetch this many ``/authors`` candidates so same-name
# queries can be disambiguated locally instead of blindly taking top-1
# (N4). 25 is enough to cover OpenAlex's top relevance bucket.
_AUTHOR_SEARCH_LIMIT = 25

# OpenAlex work id: bare ``W123`` or the full ``https://openalex.org/W123`` URL.
_WORK_ID_RE = re.compile(r"^(?:https?://openalex\.org/)?(W\d+)/?$", re.IGNORECASE)


def _bare_id(value: Any) -> str | None:
    """Strip an OpenAlex entity URL down to its bare id (``A1`` / ``W1`` ...).

    Accepts full URLs (``https://openalex.org/A5108093963``), URLs with a
    trailing slash, and bare ids (``W123``).  Returns ``None`` for anything
    that is not a string.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    return value.rstrip("/").split("/")[-1]


def _normalize_work_id(value: str) -> str | None:
    """Normalize an OpenAlex work id input to the bare ``W123`` form.

    Accepts a bare id or the full ``https://openalex.org/W123`` URL; returns
    ``None`` when the input is not a work id.
    """
    match = _WORK_ID_RE.match(value.strip())
    return match.group(1) if match else None


def _author_citations(item: dict[str, Any]) -> float:
    """Cited-by count of an author candidate, top-level or ``summary_stats``."""
    cited = item.get("cited_by_count")
    if not isinstance(cited, (int, float)):
        stats = item.get("summary_stats")
        cited = stats.get("cited_by_count") if isinstance(stats, dict) else None
    return float(cited) if isinstance(cited, (int, float)) else 0.0


def _author_h_index(item: dict[str, Any]) -> int:
    """h-index of an author candidate (from ``summary_stats``), 0 when absent."""
    stats = item.get("summary_stats")
    h_index = stats.get("h_index") if isinstance(stats, dict) else None
    return int(h_index) if isinstance(h_index, (int, float)) else 0


def _contains_cjk(text: str) -> bool:
    """Return True when *text* contains any CJK Unified Ideograph (U+4E00–U+9FFF).

    Used to tell Chinese display names apart from English transliterations
    ("李飞飞" vs "Fei-Fei Li") when disambiguating CJK author queries (Q3).
    """
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _select_author_candidate(
    author_name: str,
    results: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick the best author candidate from an OpenAlex ``/authors`` response.

    (N4) The pre-fix code took ``results[0]`` from a ``per_page=1`` query,
    which deterministically resolves common names to the wrong person
    ("Wei Zhang" -> "Yong-Wei Zhang", "J. Li" -> "Jing Li").  Priority:

    1. exact normalized-name candidates — ``display_name`` token-equal to
       the query after lowercasing and dropping single-char middle initials
       (``normalize_author_tokens``), so "Wei Zhang" matches "Wei Zhang"
       but not "Yong-Wei Zhang", while "Geoffrey Hinton" matches
       "Geoffrey E. Hinton";
    2. among several exact candidates, the highest ``cited_by_count``
       (for CJK queries, highest ``h_index`` with ``cited_by_count`` as the
       tie-break — Q3: same-name Chinese scholars are frequently unrelated
       people, so citation/relevance ranking is unreliable);
    3. (Q3) CJK queries (e.g. "李飞飞") prefer candidates whose
       ``display_name`` contains CJK characters over English
       transliteration aliases ("Fei-Fei Li"), so a Chinese query does not
       silently resolve to an English-named scholar;
    4. (Q3) no exact/CJK candidate — fall back to the highest ``h_index``
       (``cited_by_count`` tie-break) instead of blind ``results[0]``, so a
       low-impact same-name scholar (e.g. an h=2 chemistry "李飞飞") never
       shadows a prominent one, and log a warning instead of silently
       assuming the pick is the intended person.
    """
    if not results:
        return None
    query_tokens = normalize_author_tokens(author_name)
    if query_tokens:
        exact = [
            item
            for item in results
            if isinstance(item, dict)
            and normalize_author_tokens(str(item.get("display_name") or "")) == query_tokens
        ]
        if exact:
            # (Q3) CJK names collide across unrelated people (common Chinese
            # names in particular), so OpenAlex relevance/citation ranking is
            # unreliable among same-name candidates — rank by h-index
            # (cited_by_count tie-break) so a low-impact same-name scholar
            # (e.g. an h=2 chemistry 李飞飞) never shadows a prominent one.
            if _contains_cjk(author_name):
                return max(
                    exact,
                    key=lambda item: (
                        _author_h_index(item),
                        _author_citations(item),
                    ),
                )
            return max(exact, key=_author_citations)
    if _contains_cjk(author_name):
        cjk = [
            item
            for item in results
            if isinstance(item, dict)
            and _contains_cjk(str(item.get("display_name") or ""))
        ]
        if cjk:
            return max(cjk, key=_author_citations)
    dict_results = [item for item in results if isinstance(item, dict)]
    if not dict_results:
        return results[0]
    chosen = max(
        dict_results,
        key=lambda item: (_author_h_index(item), _author_citations(item)),
    )
    logger.warning(
        "OpenAlex author %r: no exact-name candidate matched, fell back to "
        "highest h-index (%r, h-index=%s) — verify this is the intended person",
        author_name,
        chosen.get("display_name"),
        _author_h_index(chosen),
    )
    return chosen


class OpenAlexSource(BaseSource):
    """OpenAlex API source."""

    name = "openalex"
    source_type = SourceType.OPENALEX
    capabilities = {
        **BaseSource.capabilities,
        # C1 revision: OpenAlex supports metadata, author and citation ops.
        "citations": True,
        "get_author_papers": True,
        "get_author_profile": True,
        "get_citations": True,
        "get_citing_papers": True,
        "get_paper_by_id": True,
    }

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        email: str | None = None,
        confidence: float = 0.90,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self.email = email
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

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = dict(extra or {})
        if self.email:
            params["mailto"] = self.email
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

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = await self._client()
        url = f"{_API_BASE}{path}"
        try:
            response = await client.get(url, params=self._params(params))
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"OpenAlex request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    "OpenAlex rate limit exceeded",
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"OpenAlex request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "OpenAlex rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"OpenAlex HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        try:
            return response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from OpenAlex: {exc}",
                source_name=self.name,
                raw_snippet=response.text[:300],
            ) from exc

    def _parse_paper(self, data: dict[str, Any]) -> Paper:
        title = (data.get("title") or data.get("display_name") or "").strip() or "Untitled"
        authorships = data.get("authorships") or []
        authors: list[AuthorRef] = []
        for item in authorships:
            author = (item or {}).get("author") or {}
            name = author.get("display_name")
            if name:
                authors.append(
                    AuthorRef(
                        name=str(name),
                        author_id=_bare_id(author.get("id")),
                        position=len(authors) + 1,
                    )
                )

        ids = data.get("ids") or {}
        doi_raw = data.get("doi") or ids.get("doi")
        doi: str | None = None
        if isinstance(doi_raw, str):
            doi = doi_raw
            for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
                if doi.lower().startswith(prefix):
                    doi = doi[len(prefix) :]
                    break

        primary = data.get("primary_location") or {}
        source = primary.get("source") or {}
        venue = source.get("display_name") if isinstance(source, dict) else None
        pdf_url = primary.get("pdf_url") or data.get("open_access", {}).get("oa_url")
        landing = primary.get("landing_page_url") or ids.get("openalex") or data.get("id")

        year = data.get("publication_year")
        keywords: list[str] = []
        for kw in data.get("keywords") or []:
            if isinstance(kw, dict) and kw.get("display_name"):
                keywords.append(str(kw["display_name"]))
            elif isinstance(kw, str):
                keywords.append(kw)

        abstract = None
        inverted = data.get("abstract_inverted_index")
        if isinstance(inverted, dict) and inverted:
            try:
                positions: list[tuple[int, str]] = []
                for word, idxs in inverted.items():
                    for idx in idxs:
                        positions.append((int(idx), str(word)))
                positions.sort(key=lambda x: x[0])
                abstract = " ".join(w for _, w in positions)
            except Exception:
                abstract = None

        url = landing if isinstance(landing, str) and landing.startswith("http") else None
        openalex_id = data.get("id") or ids.get("openalex")
        paper_id = None
        if isinstance(openalex_id, str):
            paper_id = openalex_id.rstrip("/").split("/")[-1]

        # I-4: expose the cited works list (``referenced_works``) as the
        # paper's ``references`` graph relation (bare W-ids).
        raw_refs = data.get("referenced_works")
        references: list[str] | None = None
        if isinstance(raw_refs, list):
            refs: list[str] = []
            for raw in raw_refs:
                if isinstance(raw, str) and (ref_id := _normalize_work_id(raw)) is not None:
                    refs.append(ref_id)
            if refs:
                references = refs

        # Validate DOI softly
        safe_doi = doi
        if doi:
            try:
                Paper.model_validate(
                    {
                        "title": title,
                        "doi": doi,
                        "evidence_list": [
                            self._evidence(url or "https://openalex.org")
                        ],
                    }
                )
            except Exception:
                safe_doi = None

        return Paper(
            id=paper_id,
            title=title,
            authors=authors,
            year=int(year) if year is not None else None,
            venue=venue,
            abstract=abstract,
            doi=safe_doi,
            url=url,
            pdf_url=pdf_url if isinstance(pdf_url, str) and pdf_url.startswith("http") else None,
            citations=data.get("cited_by_count"),
            keywords=keywords,
            references=references,
            evidence_list=[
                self._evidence(
                    url or "https://openalex.org",
                    raw=data,
                    source_id=doi or paper_id,
                )
            ],
        )

    def _parse_author(self, data: dict[str, Any]) -> Author:
        openalex_id = data.get("id")
        author_id = None
        if isinstance(openalex_id, str):
            author_id = openalex_id.rstrip("/").split("/")[-1]
        last_inst = data.get("last_known_institution") or {}
        affiliation = last_inst.get("display_name") if isinstance(last_inst, dict) else None
        summary = data.get("summary_stats") or {}
        profile_url = openalex_id if isinstance(openalex_id, str) else None
        return Author(
            id=author_id,
            name=data.get("display_name") or "Unknown",
            openalex_id=author_id,
            affiliation=affiliation,
            email=None,
            homepage=None,
            h_index=summary.get("h_index") if isinstance(summary, dict) else None,
            citations=data.get("cited_by_count"),
            interests=[],
            profile_url=profile_url if profile_url and profile_url.startswith("http") else None,
            evidence_list=[
                self._evidence(
                    profile_url or "https://openalex.org",
                    raw=data,
                    source_id=author_id,
                )
            ],
        )

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Search works on OpenAlex."""
        data = await self._get_json(
            "/works",
            params={"search": query, "per_page": min(limit, 50)},
        )
        if not data:
            return []
        papers: list[Paper] = []
        for item in data.get("results") or []:
            if isinstance(item, dict):
                try:
                    papers.append(self._parse_paper(item))
                except Exception as exc:
                    logger.debug("Skip OpenAlex paper: %s", exc)
            if len(papers) >= limit:
                break
        return papers

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch work by DOI."""
        cleaned = doi.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                break
        data = await self._get_json(f"/works/https://doi.org/{quote(cleaned, safe='')}")
        if not data or not isinstance(data, dict):
            return None
        return self._parse_paper(data)

    async def get_paper_by_id(self, work_id: str) -> Paper | None:
        """Fetch a work record by its OpenAlex id.

        Accepts either a bare id (``"W2257979135"``) or the full
        ``https://openalex.org/W2257979135`` URL; anything that is not a
        work id returns ``None`` without making a request.

        Args:
            work_id: OpenAlex work id (bare or full URL).

        Returns:
            The parsed :class:`Paper` or ``None`` when the id is unknown.
        """
        normalized = _normalize_work_id(work_id)
        if normalized is None:
            return None
        data = await self._get_json(f"/works/{normalized}")
        if not data or not isinstance(data, dict):
            return None
        return self._parse_paper(data)

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Search authors, then list their works."""
        search = await self._get_json(
            "/authors",
            params={"search": author_name, "per_page": _AUTHOR_SEARCH_LIMIT},
        )
        if not search or not (search.get("results") or []):
            return await self.search_papers(author_name, limit=20)

        author = _select_author_candidate(author_name, search["results"])
        author_id = author.get("id") if author else None
        if not author_id:
            return await self.search_papers(author_name, limit=20)

        # Filter works by author id
        data = await self._get_json(
            "/works",
            params={"filter": f"author.id:{author_id}", "per_page": 50},
        )
        if not data:
            return []
        papers: list[Paper] = []
        for item in data.get("results") or []:
            if isinstance(item, dict):
                try:
                    papers.append(self._parse_paper(item))
                except Exception as exc:
                    logger.debug("Skip author work: %s", exc)
        return papers

    async def get_author_profile(self, author_name: str) -> Author | None:
        """Search author profile by name."""
        search = await self._get_json(
            "/authors",
            params={"search": author_name, "per_page": _AUTHOR_SEARCH_LIMIT},
        )
        if not search or not (search.get("results") or []):
            return None
        item = _select_author_candidate(author_name, search["results"])
        if not isinstance(item, dict):
            return None
        return self._parse_author(item)

    async def _fetch_citing_works(self, paper_id: str) -> dict[str, Any] | None:
        """Fetch the raw works response for works that cite *paper_id*."""
        work_id = paper_id.rstrip("/").split("/")[-1]
        data = await self._get_json(
            "/works",
            params={"filter": f"cites:{work_id}", "per_page": 50},
        )
        if not isinstance(data, dict) or not data:
            return None
        return data

    async def get_citations(self, paper_id: str) -> list[Citation]:
        """Get works that cite the given OpenAlex work id."""
        # Accept bare W-id or full URL
        work_id = paper_id.rstrip("/").split("/")[-1]
        data = await self._fetch_citing_works(paper_id)
        if not data:
            return []
        source_url = f"https://openalex.org/{work_id}"
        citations: list[Citation] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            citing_full = item.get("id")
            if not citing_full:
                continue
            citing_id = str(citing_full).rstrip("/").split("/")[-1]
            if citing_id == work_id:
                continue
            citations.append(
                Citation(
                    citing_paper_id=citing_id,
                    cited_paper_id=work_id,
                    evidence=self._evidence(source_url, raw={"citing": citing_full}),
                )
            )
        return citations

    async def get_citing_papers(self, paper_id: str) -> list[Paper]:
        """Get the Paper records for works that cite the given OpenAlex id.

        Complements :meth:`get_citations` (which returns relationship objects
        only) so that citing works can be persisted as full records and
        placeholder nodes are backfilled from the source (FIX-B1 F4).  Shares
        the same ``/works?filter=cites:`` request as :meth:`get_citations`,
        so a cache hit covers both.
        """
        data = await self._fetch_citing_works(paper_id)
        if not data:
            return []
        papers: list[Paper] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            try:
                papers.append(self._parse_paper(item))
            except Exception as exc:
                logger.debug("Skip citing work: %s", exc)
        return papers
