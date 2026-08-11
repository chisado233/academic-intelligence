"""Trace-citing primitive: pull papers that cite a given paper (reverse citations).

Library layer consumed by the ``trace-citing`` CLI (Task 4).  Two free,
unauthenticated sources are merged; a paper appears once in the output —
the dedup key is ``doi`` when the paper carries one, else
``citing_paper_id``, so an OpenAlex W-id row and a COCI DOI row for the
*same* paper collapse into one entry:

- **OpenAlex** — ``GET https://api.openalex.org/works?filter=cites:{W-id}``
  with cursor pagination (``per-page=200``; the first page cursor must be
  ``*`` — OpenAlex rejects ``cursor=`` on the first page).  Works are keyed
  by OpenAlex W-id; ``paper_id`` is normalized first (W-id used directly,
  DOI via ``filter=doi:``, arXiv id via ``filter=ids.arxiv:``).
- **OpenCitations (COCI)** — keyed by DOI.  Note: although the naive
  reading of "who cites this paper" suggests ``references/{doi}``, that
  endpoint returns the edges where the queried DOI is the *citing* side
  (the paper's own references).  Reverse citations live on
  ``GET .../index/coci/api/v1/citations/{doi}`` — the queried DOI is the
  *cited* side and the returned ``citing`` field is the citing paper — so
  that is the endpoint used here (matches
  :mod:`academic_intelligence.sources.opencitations`).

The API is fail-soft: a source that fails (network error, unresolvable id,
HTTP error) is recorded in ``CitingResult.errors`` as a
:class:`~academic_intelligence.core.exceptions.SourceFailure` and the other
source still runs.  ``limit`` caps the deduplicated *output*: OpenAlex
pagination is bounded by the remaining quota and OpenCitations (a single
unpaginated response) always runs when a DOI is available — a source is
never silently skipped — then the merged result is truncated to ``limit``.
``resume_cursor`` tells the caller where to continue via ``resume_from``:
it is the next page's OpenAlex cursor; the unconsumed tail of a page cut
mid-way by ``limit`` (at most ``per-page - 1`` works) is dropped, so the
caller always makes progress by passing it back as ``resume_from`` (dedupe
across calls as needed).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from academic_intelligence.core.exceptions import (
    ParseError,
    SourceFailure,
)
from academic_intelligence.core.models import normalize_doi
from academic_intelligence.utils.http import HTTPClient

logger = logging.getLogger(__name__)

_OPENALEX_BASE = "https://api.openalex.org/works"
_OPENALEX_PAGE_SIZE = 200
# OpenAlex cursor pagination: the first request must pass cursor="*".
_OPENALEX_FIRST_CURSOR = "*"
_OPENCITATIONS_BASE = "https://opencitations.net/index/coci/api/v1"

# Sources the trace primitive knows how to drive.  ``fetch_citing_papers``
# runs them in this order (deterministic ``limit`` semantics).
_SOURCES: dict[str, str] = {
    "openalex": "OpenAlex reverse citations (W-id keyed)",
    "opencitations": "OpenCitations COCI citation edges (DOI keyed)",
}

_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")
_WORK_ID_RE = re.compile(r"^(?:https?://openalex\.org/)?(W\d+)/?$", re.IGNORECASE)
_ARXIV_ID_RE = re.compile(r"^(?:https?://arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})(?:v\d+)?$")
# Legacy pre-2007 arXiv ids (``hep-th/9901001``, ``math.GT/0309136``) are not
# accepted by OpenAlex ``filter=ids.arxiv:`` — detected to give a targeted
# error instead of the generic "cannot resolve" message (M-1).
_LEGACY_ARXIV_ID_RE = re.compile(
    r"^(?:https?://arxiv\.org/(?:abs|pdf)/)?[A-Za-z-]+(?:\.[A-Za-z-]+)?/\d{7}$"
)


@dataclass
class CitingPaper:
    """A paper that cites the queried paper (merged across sources).

    Attributes:
        citing_paper_id: OpenAlex W-id (bare form) or the COCI edge's citing DOI.
        doi: Bare DOI when the source provided one, else ``None``.
        title: Work title (OpenAlex only).
        year: Publication year (OpenAlex only).
        venue: Source/venue display name (OpenAlex only).
        authors_raw: Raw author display names — no Chinese/pinyin handling.
        authors_detail: OpenAlex ``authorships`` entries preserved verbatim
            (``author.id`` / ``display_name`` / ``institutions``) for
            downstream enrichment (Task 2).
    """

    citing_paper_id: str
    doi: str | None = None
    title: str | None = None
    year: int | None = None
    venue: str | None = None
    authors_raw: list[str] = field(default_factory=list)
    authors_detail: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CitingResult:
    """Aggregated result of a reverse-citation pull.

    Attributes:
        papers: Deduplicated citing papers (dedup key: ``doi`` when present,
            else ``citing_paper_id``), truncated to ``limit`` when set.
        resume_cursor: OpenAlex cursor to pass as ``resume_from`` when the
            ``limit`` threshold cut the pull short — the next page's
            ``meta.next_cursor`` (the cut page's unconsumed tail is dropped);
            ``None`` when exhausted (or OpenAlex was not used).
        source_stats: Unique paper count per source that ran (what was
            *pulled* — before any ``limit`` truncation).
        written_stats: Per-source count of papers that survived into
            ``papers`` (first source to provide a paper is credited when a
            paper was seen by both sources).  Differs from ``source_stats``
            only when ``limit`` truncated the merged output.
        errors: Fail-soft records for sources that failed or were skipped.
    """

    papers: list[CitingPaper] = field(default_factory=list)
    resume_cursor: str | None = None
    source_stats: dict[str, int] = field(default_factory=dict)
    written_stats: dict[str, int] = field(default_factory=dict)
    errors: list[SourceFailure] = field(default_factory=list)


# ---------------------------------------------------------------------------
# paper_id normalization helpers
# ---------------------------------------------------------------------------


def _normalize_work_id(value: str) -> str | None:
    """Normalize an OpenAlex work id input to the bare ``W123`` form.

    Accepts a bare id or the full ``https://openalex.org/W123`` URL; returns
    ``None`` when the input is not a work id.
    """
    match = _WORK_ID_RE.match(value.strip())
    return match.group(1) if match else None


def _strip_doi_prefix(value: str) -> str:
    """Strip common DOI prefixes from *value*, returning the bare remainder."""
    cleaned = value.strip()
    for prefix in _DOI_PREFIXES:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def _normalize_doi(value: str) -> str | None:
    """Normalize *value* to a bare DOI, or ``None`` when it is not a DOI.

    Reuses :func:`academic_intelligence.core.models.normalize_doi`, which
    strips ``https://doi.org/`` / ``http://doi.org/`` / ``doi:`` prefixes
    and applies the ``10.<registrant>/<suffix>`` format check.
    """
    return normalize_doi(value)


def _normalize_arxiv_id(value: str) -> str | None:
    """Normalize an arXiv id input to the bare ``NNNN.NNNNN`` form.

    Accepts a bare id (``1707.06347``), an ``arXiv:``-prefixed id, a
    ``https://arxiv.org/abs/...`` / ``pdf/...`` URL, and version suffixes
    (``2106.09685v2`` → ``2106.09685``); returns ``None`` otherwise —
    including legacy pre-2007 ids (``hep-th/9901001``), which the OpenAlex
    ``ids.arxiv`` filter does not accept.
    """
    cleaned = value.strip()
    if cleaned.lower().startswith("arxiv:"):
        cleaned = cleaned[len("arxiv:") :].strip()
    match = _ARXIV_ID_RE.match(cleaned)
    return match.group(1) if match else None


def _bare_work_id(value: Any) -> str | None:
    """Strip an OpenAlex work id down to the bare ``W123`` form.

    Accepts bare ids and full ``https://openalex.org/W123`` URLs; returns
    ``None`` for anything else.
    """
    if not isinstance(value, str):
        return None
    return _normalize_work_id(value)


def _first_work_id(data: Any) -> str | None:
    """Extract the bare W-id of the first work in an OpenAlex response."""
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    return _bare_work_id(first.get("id"))


# ---------------------------------------------------------------------------
# source fetchers
# ---------------------------------------------------------------------------


def _parse_openalex_paper(item: dict[str, Any]) -> CitingPaper | None:
    """Map an OpenAlex ``/works`` result item to a :class:`CitingPaper`."""
    paper_id = _bare_work_id(item.get("id"))
    if paper_id is None:
        return None

    title_raw = item.get("title") or item.get("display_name")
    title = str(title_raw) if title_raw is not None else None

    year_raw = item.get("publication_year")
    year = year_raw if isinstance(year_raw, int) and not isinstance(year_raw, bool) else None

    venue: str | None = None
    primary = item.get("primary_location")
    if isinstance(primary, dict):
        source = primary.get("source")
        if isinstance(source, dict):
            venue_raw = source.get("display_name")
            venue = str(venue_raw) if venue_raw is not None else None

    authors_raw: list[str] = []
    authors_detail: list[dict[str, Any]] = []
    authorships = item.get("authorships")
    if isinstance(authorships, list):
        for entry in authorships:
            if not isinstance(entry, dict):
                continue
            authors_detail.append(entry)
            author = entry.get("author")
            if isinstance(author, dict):
                name = author.get("display_name")
                if isinstance(name, str) and name:
                    authors_raw.append(name)

    return CitingPaper(
        citing_paper_id=paper_id,
        doi=_strip_doi_prefix(item["doi"]) if isinstance(item.get("doi"), str) else None,
        title=title,
        year=year,
        venue=venue,
        authors_raw=authors_raw,
        authors_detail=authors_detail,
    )


async def _fetch_openalex(
    http: HTTPClient,
    work_id: str,
    *,
    limit: int | None,
    resume_from: str | None,
) -> tuple[list[CitingPaper], str | None]:
    """Pull citing works from OpenAlex with cursor pagination.

    Returns ``(papers, resume_cursor)``.  When ``limit`` cuts the pull, the
    resume cursor is the *next* page's ``meta.next_cursor`` (or ``None`` on
    the final page); the unconsumed tail of the cut page (at most
    ``per-page - 1`` works) is dropped — a cursor is an opaque OpenAlex
    snapshot and cannot be resumed mid-page.  A stateless caller therefore
    always makes progress by passing the returned cursor back as
    ``resume_from``.
    """
    papers: dict[str, CitingPaper] = {}
    cursor = resume_from if resume_from else _OPENALEX_FIRST_CURSOR

    while True:
        data = await http.get_json(
            _OPENALEX_BASE,
            params={
                "filter": f"cites:{work_id}",
                "per-page": _OPENALEX_PAGE_SIZE,
                "cursor": cursor,
            },
        )
        if not isinstance(data, dict):
            raise ParseError(
                "unexpected OpenAlex payload: expected an object with results/meta",
                source_name="openalex",
                raw_snippet=str(data)[:300],
            )
        results = data.get("results")
        if not isinstance(results, list):
            raise ParseError(
                "unexpected OpenAlex payload: results must be an array",
                source_name="openalex",
                raw_snippet=str(data)[:300],
            )
        meta = data.get("meta")
        next_cursor = meta.get("next_cursor") if isinstance(meta, dict) else None

        for item in results:
            if not isinstance(item, dict):
                continue
            paper = _parse_openalex_paper(item)
            if paper is None:
                continue
            papers[paper.citing_paper_id] = paper
            if limit is not None and len(papers) >= limit:
                return list(papers.values()), (str(next_cursor) if next_cursor else None)

        if not next_cursor:
            return list(papers.values()), None
        cursor = str(next_cursor)


async def _fetch_opencitations(http: HTTPClient, doi: str) -> list[CitingPaper]:
    """Pull citing DOIs from OpenCitations COCI (single unpaginated response).

    Uses the ``/citations/{doi}`` endpoint (reverse direction — see module
    docstring).  Edges with a missing citing DOI, self-citations and
    malformed DOIs are skipped.
    """
    url = f"{_OPENCITATIONS_BASE}/citations/{doi.replace('/', '%2F')}"
    data = await http.get_json(url)
    if not isinstance(data, list):
        raise ParseError(
            "unexpected OpenCitations payload: expected a JSON edge array",
            source_name="opencitations",
            raw_snippet=str(data)[:300],
        )
    papers: dict[str, CitingPaper] = {}
    for edge in data:
        if not isinstance(edge, dict):
            continue
        citing_raw = edge.get("citing")
        if not isinstance(citing_raw, str):
            continue
        citing = normalize_doi(citing_raw)
        if citing is None or citing == doi:
            continue
        papers[citing] = CitingPaper(citing_paper_id=citing, doi=citing)
    return list(papers.values())


# ---------------------------------------------------------------------------
# paper_id resolution
# ---------------------------------------------------------------------------


async def _resolve_openalex_work_id(
    http: HTTPClient, *, doi: str | None, arxiv: str | None
) -> tuple[str | None, SourceFailure | None]:
    """Resolve a DOI/arXiv id to an OpenAlex W-id via a ``filter=`` lookup.

    Returns ``(work_id, failure)``; ``work_id`` is ``None`` and *failure* is
    populated when the id is not found or the lookup itself fails.  Only
    called when the input is not already a W-id.
    """
    if doi is not None:
        try:
            data = await http.get_json(_OPENALEX_BASE, params={"filter": f"doi:{doi}"})
        except Exception as exc:
            return None, SourceFailure.from_exception(
                source="openalex", operation="resolve_paper_id", exc=exc
            )
        work_id = _first_work_id(data)
        if work_id is None:
            return None, SourceFailure.from_message(
                source="openalex",
                operation="resolve_paper_id",
                message=f"no OpenAlex work found for DOI {doi!r}",
                error_type="LookupError",
            )
        return work_id, None

    if arxiv is not None:
        try:
            data = await http.get_json(_OPENALEX_BASE, params={"filter": f"ids.arxiv:{arxiv}"})
        except Exception as exc:
            return None, SourceFailure.from_exception(
                source="openalex", operation="resolve_paper_id", exc=exc
            )
        work_id = _first_work_id(data)
        if work_id is None:
            return None, SourceFailure.from_message(
                source="openalex",
                operation="resolve_paper_id",
                message=f"no OpenAlex work found for arXiv id {arxiv!r}",
                error_type="LookupError",
            )
        return work_id, None

    return None, None


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


async def fetch_citing_papers(
    paper_id: str,
    *,
    sources: list[str] | None = None,
    limit: int | None = None,
    resume_from: str | None = None,
    http: HTTPClient | None = None,
) -> CitingResult:
    """Fetch papers that cite *paper_id*, merged across the requested sources.

    Args:
        paper_id: An OpenAlex W-id (``W123`` / full URL), a DOI (``10.…``,
            optionally ``https://doi.org/`` / ``doi:`` prefixed), or an arXiv
            id (``1707.06347``, ``arXiv:…``, ``abs/…`` URL, with optional
            version suffix).  DOIs and arXiv ids are resolved to a W-id via
            OpenAlex ``filter=`` lookups.
        sources: Source names to use, defaulting to
            ``["openalex", "opencitations"]``.  Unknown names are recorded
            fail-soft in ``errors``.
        limit: Cap on the deduplicated *output* paper count.  OpenAlex
            pagination is bounded by the remaining quota (``limit <= 0``
            fetches nothing) and OpenCitations still runs when a DOI is
            available — it is never silently skipped — then the merged
            result is truncated to ``limit``.  The caller continues with
            ``resume_from`` set to the returned ``resume_cursor``.
        resume_from: OpenAlex cursor to continue from (see
            ``resume_cursor``).  OpenCitations is unpaginated and re-runs
            fully on a resume; callers dedupe across calls.
        http: Shared :class:`HTTPClient`; when omitted a client is created,
            connected and closed for the duration of the call.

    Returns:
        A :class:`CitingResult` with deduplicated papers (dedup key ``doi``
        when present, else ``citing_paper_id``), the resume cursor (if
        truncated), per-source pulled/written counts, and fail-soft errors.

    Raises:
        RuntimeError: When *http* is provided but not connected
            (``HTTPClient`` requires an explicit ``connect()``).
    """
    names = list(sources) if sources is not None else list(_SOURCES)
    result = CitingResult()

    if limit is not None and limit <= 0:
        return result

    for name in names:
        if name not in _SOURCES:
            result.errors.append(
                SourceFailure.from_message(
                    source=name,
                    operation="fetch_citing_papers",
                    message=f"unknown source {name!r}",
                    error_type="ValueError",
                )
            )

    wants_openalex = "openalex" in names
    wants_opencitations = "opencitations" in names
    if not wants_openalex and not wants_opencitations:
        return result

    # Cheap input-form classification (no network).
    work_id = _normalize_work_id(paper_id)
    doi = _normalize_doi(paper_id)
    arxiv = _normalize_arxiv_id(paper_id)

    client = http
    if client is None:
        client = HTTPClient()
        await client.connect()
        own_client = True
    else:
        own_client = False
    try:
        # Cross-source merge: a paper appears once — keyed by ``doi`` when
        # it carries one, else ``citing_paper_id`` (I-3).  This collapses an
        # OpenAlex W-id row and a COCI DOI row for the same paper into one
        # entry.  ``origin`` records which source first provided each key so
        # the written counts can be attributed after ``limit`` truncation.
        merged: dict[str, CitingPaper] = {}
        origin: dict[str, str] = {}

        def merge(papers: list[CitingPaper], source: str) -> None:
            for paper in papers:
                key = paper.doi if paper.doi else paper.citing_paper_id
                if key not in merged:
                    merged[key] = paper
                    origin[key] = source

        if wants_openalex:
            if work_id is None:
                if doi is not None or arxiv is not None:
                    work_id, resolve_error = await _resolve_openalex_work_id(
                        client, doi=doi, arxiv=arxiv
                    )
                    if work_id is None and resolve_error is not None:
                        result.errors.append(resolve_error)
                else:
                    if _LEGACY_ARXIV_ID_RE.match(paper_id.strip()):
                        resolve_message = (
                            f"legacy arXiv id {paper_id!r} is not supported; "
                            "only new-style YYYY.NNNNN arXiv ids (2007 "
                            "onwards, e.g. 1707.06347) are accepted"
                        )
                    else:
                        resolve_message = (
                            f"cannot resolve paper_id {paper_id!r} to an "
                            "OpenAlex work id (expected a W-id, DOI or arXiv "
                            "id in the new-style YYYY.NNNNN format)"
                        )
                    result.errors.append(
                        SourceFailure.from_message(
                            source="openalex",
                            operation="fetch_citing_papers",
                            message=resolve_message,
                            error_type="ValueError",
                        )
                    )
            if work_id is not None:
                try:
                    remaining = limit - len(merged) if limit is not None else None
                    oa_papers, result.resume_cursor = await _fetch_openalex(
                        client, work_id, limit=remaining, resume_from=resume_from
                    )
                    result.source_stats["openalex"] = len(oa_papers)
                    merge(oa_papers, "openalex")
                except Exception as exc:
                    result.errors.append(
                        SourceFailure.from_exception(
                            source="openalex",
                            operation="fetch_citing_papers",
                            exc=exc,
                        )
                    )

        # OpenCitations is a single unpaginated response; it runs whenever a
        # DOI is available, even when ``limit`` was already filled by
        # OpenAlex — the final truncation below applies to the merged result
        # (I-2: a requested source is never silently skipped).
        if wants_opencitations:
            if doi is None:
                result.errors.append(
                    SourceFailure.from_message(
                        source="opencitations",
                        operation="fetch_citing_papers",
                        message=(
                            f"OpenCitations is keyed by DOI and paper_id "
                            f"{paper_id!r} has no DOI; skipping"
                        ),
                        error_type="ValueError",
                    )
                )
            else:
                try:
                    coci_papers = await _fetch_opencitations(client, doi)
                    result.source_stats["opencitations"] = len(coci_papers)
                    merge(coci_papers, "opencitations")
                except Exception as exc:
                    result.errors.append(
                        SourceFailure.from_exception(
                            source="opencitations",
                            operation="fetch_citing_papers",
                            exc=exc,
                        )
                    )

        result.papers = list(merged.values())
        if limit is not None:
            result.papers = result.papers[:limit]
        for paper in result.papers:
            key = paper.doi if paper.doi else paper.citing_paper_id
            source = origin.get(key, "unknown")
            result.written_stats[source] = result.written_stats.get(source, 0) + 1
        return result
    finally:
        if own_client:
            await client.close()
