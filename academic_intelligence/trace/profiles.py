"""Trace-profiles primitive: batch author profile enrichment via OpenAlex.

Given the flattened author rows produced by ``trace-authors`` (Task 2), fetch
per-author disambiguation features from the OpenAlex API:

- ``GET https://api.openalex.org/authors/{id}`` — institution / h-index /
  research topics / works count
- ``GET https://api.openalex.org/works?filter=author.id:{id}&sort=cited_by_count:desc&per-page=5``
  — representative works, capped client-side at 5

Politeness comes from the shared ``HTTPClient`` (adaptive rate limiter, default
≈ 1 rps) and from batching: ``batch_size`` bounds the number of authors whose
requests are in flight at once (authors within a batch are gathered; a batch of
1 is strictly sequential).

Rows without an ``author_id`` are **not** auto-matched against OpenAlex — an
unchecked name search risks merging distinct people.  They yield a placeholder
``AuthorProfile`` (``author_id=None`` + empty fields) for the agent methodology
to handle explicitly.  A single author's failure is recorded in that author's
``errors`` and never blocks the batch.

When the OpenAlex profile fetch fails *transiently* (HTTP 429/5xx, timeout,
transport error), the author falls back to a Semantic Scholar name search —
placeholder data only, flagged with ``source="s2"`` and annotated in ``errors``
(homonym risk; a 404 / parse failure is a permanent answer and does not fall
back).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from academic_intelligence.utils.http import HTTPClient

_AUTHOR_BASE_URL = "https://api.openalex.org/authors"
_WORKS_URL_TEMPLATE = (
    "https://api.openalex.org/works?filter=author.id:{author_id}"
    "&sort=cited_by_count:desc&per-page=5"
)
_MAX_TOP_WORKS = 5
# Semantic Scholar fallback endpoints (public API, unauthenticated).
_S2_AUTHOR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/author/search"
_S2_AUTHOR_URL_TEMPLATE = "https://api.semanticscholar.org/graph/v1/author/{author_id}"
_S2_AUTHOR_FIELDS = "name,affiliations,paperCount,hIndex,citationCount"
# HTTP statuses whose OpenAlex author failure triggers the S2 fallback
# (transient, quota-shaped failures; permanent ones like 404 do not).
_FALLBACK_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class AuthorRow:
    """A flattened citing-author row (Task 2 contract, mirrored locally).

    ``fetch_profiles`` only reads the documented attributes, so any object
    exposing ``author_name`` / ``appears_in`` / ``affiliation`` / ``author_id``
    — including the canonical ``academic_intelligence.trace.authors.AuthorRow``
    written by the parallel ``trace-authors`` single — is accepted.  The local
    definition keeps this module importable before that single lands.
    """

    author_name: str
    appears_in: list[str]
    affiliation: str | None = None
    author_id: str | None = None


@dataclass(frozen=True)
class AuthorProfile:
    """Enriched author profile used for agent disambiguation / screening.

    ``top_works`` entries carry ``{title, venue, year, cited_by_count}`` and
    are sorted by ``cited_by_count`` descending, capped at 5.  ``errors`` holds
    per-author failure strings; a non-empty list means the fields are partial
    (typically all empty) but the batch proceeded.

    ``source`` records where the profile data came from: ``"openalex"``
    (primary) or ``"s2"`` — a Semantic Scholar name-match fallback used when
    the OpenAlex profile fetch failed transiently.  An ``"s2"`` profile is
    placeholder quality (homonym risk) and the fallback is recorded in
    ``errors``.
    """

    author_name: str
    author_id: str | None
    source: str = "openalex"
    institution: str | None = None
    h_index: int | None = None
    fields: list[str] = field(default_factory=list)
    works_count: int | None = None
    top_works: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def fetch_profiles(
    author_rows: list[AuthorRow],
    *,
    batch_size: int = 20,
    http: HTTPClient | None = None,
) -> list[AuthorProfile]:
    """Fetch OpenAlex profiles for *author_rows*, one entry per input row.

    Args:
        author_rows: Flattened author rows from ``trace-authors``.  Rows with
            ``author_id=None`` are never matched automatically; they produce a
            placeholder profile (``author_id=None``, empty fields).  An author
            whose OpenAlex profile fetch fails transiently (HTTP 429/5xx,
            timeout, transport error) falls back to a Semantic Scholar name
            match — see :func:`_s2_fallback`.
        batch_size: Number of authors fetched concurrently per batch (≥ 1).
        http: Optional ``HTTPClient`` to use.  A client created internally is
            closed before returning; a caller-provided client is left open
            (caller owns its lifecycle).

    Returns:
        One ``AuthorProfile`` per input row, in input order.  Rows sharing the
        same author ID (including ``https://openalex.org/A…`` vs bare ``A…``
        spellings) reuse a single fetch.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    owns_client = http is None
    client = http if http is not None else HTTPClient()
    await client.connect()
    try:
        # Unique (author_name, original_id) pairs in first-appearance order;
        # rows without an ID never reach the network.  Dedup keys on the
        # normalized ID so "https://openalex.org/A…" and bare "A…" spellings
        # of the same author are fetched once; the profile keeps the original
        # spelling verbatim (input fidelity).
        unique: list[tuple[str, str]] = []
        seen: set[str] = set()
        for row in author_rows:
            if row.author_id is None:
                continue
            norm_id = _normalize_author_id(row.author_id)
            if norm_id not in seen:
                seen.add(norm_id)
                unique.append((row.author_name, row.author_id))

        profile_by_id: dict[str, AuthorProfile] = {}
        for start in range(0, len(unique), batch_size):
            batch = unique[start : start + batch_size]
            fetched = await asyncio.gather(
                *(_fetch_one(client, name, orig_id) for name, orig_id in batch)
            )
            profile_by_id.update(
                zip(
                    (_normalize_author_id(orig) for _, orig in batch),
                    fetched,
                    strict=True,
                )
            )

        return [
            _placeholder(row)
            if row.author_id is None
            else profile_by_id[_normalize_author_id(row.author_id)]
            for row in author_rows
        ]
    finally:
        if owns_client:
            await client.close()


def _placeholder(row: AuthorRow) -> AuthorProfile:
    """Profile for a row without an author ID (no auto-search, empty fields)."""
    return AuthorProfile(author_name=row.author_name, author_id=None)


def _normalize_author_id(author_id: str) -> str:
    """Strip known OpenAlex URL prefixes so ``authors/{id}`` stays valid.

    OpenAlex authorships expose ``author.id`` as a full URL
    (``https://openalex.org/A…``); the API path expects the bare ID.  Unknown
    shapes pass through unchanged.
    """
    for prefix in ("https://api.openalex.org/authors/", "https://openalex.org/"):
        if author_id.startswith(prefix):
            return author_id[len(prefix) :]
    return author_id


async def _fetch_one(client: HTTPClient, author_name: str, author_id: str) -> AuthorProfile:
    """Fetch one author's profile + top works; never raises (errors recorded).

    *author_id* is stored on the profile verbatim; URL construction uses the
    normalized form so full-URL and bare spellings both resolve.

    When the OpenAlex profile fetch fails *transiently* (HTTP 429/5xx,
    timeout, transport error), the profile falls back to a Semantic Scholar
    name search (``source="s2"``, placeholder quality).  Permanent failures
    (404 not found, parse errors) are real answers and do not fall back.
    """
    fetch_id = _normalize_author_id(author_id)
    errors: list[str] = []
    source = "openalex"
    institution: str | None = None
    h_index: int | None = None
    fields: list[str] = []
    works_count: int | None = None
    try:
        data = await _fetch_json(client, f"{_AUTHOR_BASE_URL}/{fetch_id}")
        institution, h_index, fields, works_count = _parse_profile(data)
    except Exception as exc:
        errors.append(f"profile fetch failed: {_format_error(exc)}")
        if _is_fallback_worthy(exc):
            s2_result, s2_error = await _s2_fallback(client, author_name)
            if s2_result is not None:
                source = "s2"
                institution, h_index, works_count = s2_result
                errors.append(
                    "s2 fallback: profile from Semantic Scholar name search "
                    "(homonym risk — requires agent disambiguation)"
                )
            else:
                errors.append(
                    f"s2 fallback failed: {s2_error or 'no Semantic Scholar author found'}"
                )

    top_works: list[dict[str, Any]] = []
    if not errors:
        try:
            works_data = await _fetch_json(client, _WORKS_URL_TEMPLATE.format(author_id=fetch_id))
            top_works = _parse_top_works(works_data)
        except Exception as exc:
            errors.append(f"top works fetch failed: {_format_error(exc)}")

    return AuthorProfile(
        author_name=author_name,
        author_id=author_id,
        source=source,
        institution=institution,
        h_index=h_index,
        fields=fields,
        works_count=works_count,
        top_works=top_works,
        errors=errors,
    )


def _is_fallback_worthy(exc: BaseException) -> bool:
    """Whether an OpenAlex author failure should trigger the S2 fallback.

    Mirrors the citing primitive's contract: transient, quota-shaped
    failures (HTTP 429/5xx, timeouts, transport errors) fall back; permanent
    answers (404 not found, parse errors) do not.
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and len(seen) < 16:
        if id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            if current.response.status_code in _FALLBACK_HTTP_STATUSES:
                return True
        elif isinstance(current, httpx.TransportError):
            return True
        current = current.__cause__
    return False


async def _s2_fallback(
    client: HTTPClient,
    author_name: str,
) -> tuple[tuple[str | None, int | None, int | None] | None, str | None]:
    """Best-effort Semantic Scholar author lookup by name (never raises).

    Returns ``((institution, h_index, works_count), failure_reason)`` —
    exactly one side is ``None``.  A name match is inherently ambiguous, so
    callers must treat a hit as placeholder data and annotate it; the
    failure reason (when the search/profile fetch itself failed) is
    returned for the error record.
    """
    try:
        search_data = await _fetch_json(
            client,
            f"{_S2_AUTHOR_SEARCH_URL}?query={quote(author_name, safe='')}"
            "&limit=1&fields=authorId,name",
        )
    except Exception as exc:
        return None, _format_error(exc)
    items = search_data.get("data")
    if not isinstance(items, list) or not items:
        return None, "no Semantic Scholar author found"
    first = items[0]
    if not isinstance(first, dict):
        return None, "malformed Semantic Scholar author search result"
    author_id = first.get("authorId")
    if not isinstance(author_id, str) or not author_id:
        return None, "Semantic Scholar author search returned no author id"
    try:
        data = await _fetch_json(
            client,
            f"{_S2_AUTHOR_URL_TEMPLATE.format(author_id=author_id)}?fields={_S2_AUTHOR_FIELDS}",
        )
    except Exception as exc:
        return None, _format_error(exc)
    try:
        parsed = _parse_s2_author(data)
    except Exception as exc:
        return None, _format_error(exc)
    return parsed, None


def _parse_s2_author(data: dict[str, Any]) -> tuple[str | None, int | None, int | None]:
    """Extract (institution, h_index, works_count) from an S2 author doc.

    S2 serves ``affiliations`` as a list of strings (first is taken, with
    dict entries tolerated); ``hIndex`` / ``paperCount`` may be absent on
    sparse author records.
    """
    affiliation: str | None = None
    affiliations = data.get("affiliations")
    if isinstance(affiliations, list):
        for entry in affiliations:
            if isinstance(entry, str) and entry:
                affiliation = entry
                break
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name:
                    affiliation = name
                    break
    h_index_raw = data.get("hIndex")
    h_index = int(h_index_raw) if h_index_raw is not None else None
    works_raw = data.get("paperCount")
    works_count = int(works_raw) if works_raw is not None else None
    return affiliation, h_index, works_count


async def _fetch_json(client: HTTPClient, url: str) -> dict[str, Any]:
    """GET *url* and return the parsed JSON object (errors propagate)."""
    response = await client.get(url)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object from {url}, got {type(data).__name__}")
    return data


def _format_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _parse_profile(
    data: dict[str, Any],
) -> tuple[str | None, int | None, list[str], int | None]:
    """Extract (institution, h_index, fields, works_count) from an author doc.

    OpenAlex serves the institution under ``last_known_institutions`` /
    ``affiliations[].institution`` and the h-index under ``summary_stats.h_index``;
    the flat ``institutions`` / ``h_index`` shapes documented in the dispatch
    contract are kept as fallbacks so both payload shapes parse.
    """
    candidates: list[Any] = []
    for key in ("last_known_institutions", "affiliations", "institutions"):
        source = data.get(key)
        if isinstance(source, list):
            candidates.extend(source)
    institution: str | None = None
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        display = entry.get("display_name")
        if display is None:
            nested = entry.get("institution")
            if isinstance(nested, dict):
                display = nested.get("display_name")
        if display:
            institution = str(display)
            break

    stats = data.get("summary_stats")
    h_index = stats.get("h_index") if isinstance(stats, dict) else None
    if h_index is None:
        h_index = data.get("h_index")
    if h_index is not None:
        h_index = int(h_index)

    fields = [
        topic["display_name"]
        for topic in data.get("topics") or []
        if isinstance(topic, dict) and topic.get("display_name")
    ]

    works_count = data.get("works_count")
    if works_count is not None:
        works_count = int(works_count)

    return institution, h_index, fields, works_count


def _parse_top_works(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a works response into top-5 dicts sorted by citations desc."""
    works: list[dict[str, Any]] = []
    for work in data.get("results") or []:
        if not isinstance(work, dict):
            continue
        venue: str | None = None
        location = work.get("primary_location")
        if isinstance(location, dict):
            source = location.get("source")
            if isinstance(source, dict) and source.get("display_name"):
                venue = str(source["display_name"])
        year = work.get("publication_year")
        if year is not None:
            year = int(year)
        cited = work.get("cited_by_count")
        if cited is None:
            cited = 0
        works.append(
            {
                "title": str(work.get("title") or ""),
                "venue": venue,
                "year": year,
                "cited_by_count": int(cited),
            }
        )
    works.sort(key=lambda w: int(w["cited_by_count"]), reverse=True)
    return works[:_MAX_TOP_WORKS]
