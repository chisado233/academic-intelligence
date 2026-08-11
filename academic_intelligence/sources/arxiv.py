"""arXiv data source adapter.

Uses the public arXiv API (Atom XML):
http://export.arxiv.org/api/query

Rate limit guidance: at most one request every 3 seconds.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

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
    normalize_doi,
)
from academic_intelligence.core.types import AntiCrawlStrategy, SourceType
from academic_intelligence.sources.base import (
    BaseSource,
    is_rate_limit_status,
    retry_after_from_error,
)
from academic_intelligence.utils.http import HTTPClient
from academic_intelligence.utils.rate_limiter import create_rate_limiter

logger = logging.getLogger(__name__)

_API_BASE = "http://export.arxiv.org/api/query"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
# arXiv ID patterns: YYMM.number[vN] or archive/YYMMNNN[vN].
_ARXIV_ID_RE = re.compile(
    r"((?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?)",
    re.IGNORECASE,
)
_ARXIV_ID_PREFIXES = (
    "arxiv:",
    "https://arxiv.org/abs/",
    "http://arxiv.org/abs/",
    "https://arxiv.org/pdf/",
    "http://arxiv.org/pdf/",
)
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")
# arXiv-native DOI namespace (Y-1): the API's ``doi:"..."`` field search does
# not match these even though the Atom metadata carries them.
_ARXIV_DOI_PREFIX = "10.48550/arxiv."

# Noise segments commonly appended to arXiv journal_ref strings (volume /
# issue / pages / year / ISSN / DOI); the venue field should keep only the
# journal name (FIX-L F3).  The volume:pages alternation also covers the
# compact journal citation form (``Med Image Anal. 42:60-88``,
# ``J. Neurosci. 33(4):1234-1245``) whose noise carries no keyword (FIX-M M2).
_JOURNAL_REF_NOISE_RE = re.compile(
    r"ISSN\s*[:\s]?\d{4}[-–]\d{3,4}[Xx]?"
    r"|\b(?:volume|vol|issue|no)\.?\s*\d+[A-Za-z]?"
    r"|\bpp?\.?\s*\d+\s*[-–,to]+\s*\d+"
    r"|\bpp?\.?\s*\d+\b"
    r"|\d+(?:\(\d+\))?\s*:\s*\d+(?:-\d+)?"
    r"|\(\s*(?:19|20)\d{2}\s*\)"
    r"|\b(?:19|20)\d{2}\b"
    r"|\bDOI\s*:?\s*10\.\S+",
    re.IGNORECASE,
)


def _clean_journal_ref(raw: str) -> str:
    """Extract the bare journal name from an arXiv ``journal_ref`` string.

    arXiv journal_ref values append volume / issue / pages / year / ISSN
    noise, e.g. ``"Medical Image Analysis, Volume 71, 2021, 102062, ISSN
    1361-8415"``. The venue field should carry the bare journal name, so the
    noise is stripped and the leading segment kept.
    """
    s = raw.strip()
    if not s:
        return ""
    s = _JOURNAL_REF_NOISE_RE.sub(" ", s)
    # Keep the leading segment (the journal name).
    s = re.split(r"[,;|]", s, maxsplit=1)[0]
    # Drop a trailing bare volume/article number, e.g. "Phys. Rev. Lett. 124".
    s = re.sub(r"\s+\d+(?:\s*\([^)]*\))?\s*$", "", s)
    return " ".join(s.split())


def _normalize_doi(doi: str) -> str:
    cleaned = doi.strip()
    for prefix in _DOI_PREFIXES:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def _arxiv_id_from_doi(doi: str) -> str | None:
    """Extract the bare arXiv ID from an arXiv-native DOI (Y-1).

    arXiv registers preprints under ``10.48550/arXiv.<id>``; the API's
    ``doi:"..."`` field search never matches those DOIs even though the Atom
    metadata carries them, so callers must route to the ``id_list`` lookup
    instead.  Returns ``None`` for any other DOI.
    """
    if not doi.lower().startswith(_ARXIV_DOI_PREFIX):
        return None
    return doi[len(_ARXIV_DOI_PREFIX) :].strip() or None


def _parse_arxiv_id(value: str) -> str | None:
    """Return a complete arXiv identifier, rejecting embedded free text."""
    cleaned = value.strip().rstrip("/")
    lowered = cleaned.lower()
    for prefix in _ARXIV_ID_PREFIXES:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip().rstrip("/")
            break
    if cleaned.lower().endswith(".pdf"):
        cleaned = cleaned[:-4]
    match = _ARXIV_ID_RE.fullmatch(cleaned)
    return match.group(1) if match else None


def _canonical_arxiv_id(value: str) -> str | None:
    """Normalize an arXiv identifier for equality, ignoring its version."""
    parsed = _parse_arxiv_id(value)
    if parsed is None:
        return None
    return re.sub(r"v\d+$", "", parsed, flags=re.IGNORECASE)


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return " ".join(el.text.split())


def _safe_doi(doi: str | None, title: str, evidence: Evidence) -> str | None:
    # (FIX-AB-3) Lightweight field-level DOI validation instead of a full
    # ``Paper.model_validate`` per entry (dominated the parse hot path).
    # ``title`` / ``evidence`` kept for signature stability; the old
    # full-model guard validated only the DOI.
    return normalize_doi(doi)


class ArxivSource(BaseSource):
    """arXiv API source (Atom XML).

    Respects the recommended 3-second inter-request interval via a fixed
    :class:`~academic_intelligence.utils.rate_limiter.RateLimiter`.
    """

    name = "arxiv"
    source_type = SourceType.ARXIV
    capabilities = {
        **BaseSource.capabilities,
        # C1 revision: author ops are real here, citation graph is not.
        "get_author_papers": True,
        "get_author_profile": True,
        "get_citations": False,
        "get_paper_by_arxiv_id": True,
    }

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        confidence: float = 0.95,
        min_interval_seconds: float = 3.0,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self.confidence = confidence
        self.min_interval_seconds = max(min_interval_seconds, 0.1)

    async def _client(self) -> HTTPClient:
        if self._http is None:
            rps = 1.0 / self.min_interval_seconds
            strategy = AntiCrawlStrategy(base_delay=self.min_interval_seconds, adaptive_delay=False)
            self._http = HTTPClient(
                strategy=strategy,
                rate_limiter=create_rate_limiter("fixed", requests_per_second=rps),
                enable_cache=True,
            )
            await self._http.connect()
        return self._http

    async def close(self) -> None:
        """Close owned HTTP client."""
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

    async def _query(self, search_query: str, *, start: int = 0, max_results: int = 10) -> str:
        client = await self._client()
        params = {
            "search_query": search_query,
            "start": start,
            "max_results": min(max(max_results, 1), 2000),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        try:
            response = await client.get(_API_BASE, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"arXiv request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    "arXiv rate limit exceeded",
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"arXiv request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "arXiv rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "3") or 3),
            )
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"arXiv HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        return response.text

    def _parse_entry(self, entry: ET.Element) -> Paper | None:
        title = _text(entry.find("atom:title", _ATOM_NS))
        if not title:
            return None

        abstract = _text(entry.find("atom:summary", _ATOM_NS)) or None
        published = _text(entry.find("atom:published", _ATOM_NS))
        year: int | None = None
        if published and len(published) >= 4 and published[:4].isdigit():
            year = int(published[:4])

        authors: list[AuthorRef] = []
        for author_el in entry.findall("atom:author", _ATOM_NS):
            name = _text(author_el.find("atom:name", _ATOM_NS))
            if name:
                authors.append(AuthorRef(name=name, position=len(authors) + 1))

        # Prefer abs HTML link, then atom:id
        url: str | None = None
        pdf_url: str | None = None
        for link in entry.findall("atom:link", _ATOM_NS):
            rel = link.get("rel") or ""
            href = link.get("href") or ""
            title_attr = (link.get("title") or "").lower()
            link_type = (link.get("type") or "").lower()
            if not href:
                continue
            if title_attr == "pdf" or link_type == "application/pdf":
                pdf_url = href.replace("http://", "https://", 1)
            elif rel in ("alternate", "") and "arxiv.org/abs/" in href:
                url = href.replace("http://", "https://", 1)

        entry_id = _text(entry.find("atom:id", _ATOM_NS))
        arxiv_id: str | None = None
        if entry_id:
            # http://arxiv.org/abs/2301.00001v1 -> 2301.00001v1
            if "/abs/" in entry_id:
                arxiv_id = entry_id.split("/abs/", 1)[1].rstrip("/")
            else:
                arxiv_id = entry_id.rstrip("/").rsplit("/", 1)[-1]
            if not url:
                bare = arxiv_id.split("v")[0] if "v" in arxiv_id else arxiv_id
                url = f"https://arxiv.org/abs/{bare}"
            if not pdf_url:
                bare = arxiv_id.split("v")[0] if "v" in arxiv_id and arxiv_id[-1].isdigit() else arxiv_id
                # Keep version in pdf if present
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

        doi_el = entry.find("arxiv:doi", _ATOM_NS)
        doi_raw = _text(doi_el) if doi_el is not None else ""
        # Also check journal_ref / comment for DOI occasionally omitted

        categories: list[str] = []
        primary = entry.find("arxiv:primary_category", _ATOM_NS)
        if primary is not None and primary.get("term"):
            categories.append(primary.get("term") or "")
        for cat in entry.findall("atom:category", _ATOM_NS):
            term = cat.get("term")
            if term and term not in categories:
                categories.append(term)

        journal = entry.find("arxiv:journal_ref", _ATOM_NS)
        venue = _clean_journal_ref(_text(journal)) if journal is not None else None
        if not venue and categories:
            venue = f"arXiv:{categories[0]}"

        source_url = url or "https://arxiv.org"
        evidence = self._evidence(
            source_url,
            raw={
                "id": entry_id,
                "arxiv_id": arxiv_id,
                "categories": categories,
                "published": published,
            },
            source_id=arxiv_id,
        )
        safe_doi = _safe_doi(doi_raw or None, title, evidence)

        return Paper(
            id=arxiv_id,
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            abstract=abstract,
            doi=safe_doi,
            arxiv_id=arxiv_id,
            url=url if url and url.startswith("http") else None,
            pdf_url=pdf_url if pdf_url and pdf_url.startswith("http") else None,
            citations=None,
            keywords=categories,
            evidence_list=[evidence],
        )

    def _parse_feed(self, xml_text: str) -> list[Paper]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ParseError(
                f"Invalid Atom XML from arXiv: {exc}",
                source_name=self.name,
                raw_snippet=xml_text[:300],
            ) from exc

        papers: list[Paper] = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            try:
                paper = self._parse_entry(entry)
                if paper is not None:
                    papers.append(paper)
            except Exception as exc:
                logger.debug("Skip arXiv entry parse error: %s", exc)
        return papers

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Search arXiv papers.

        Free-text is mapped to ``all:`` field search. Callers may also pass
        raw arXiv query syntax (e.g. ``ti:transformer AND cat:cs.LG``).
        """
        q = query.strip()
        if not q:
            return []
        # If caller already provided field prefixes, use as-is
        search_query = (
            q if re.search(r"\b(all|ti|au|abs|co|jr|cat|rn):", q) else f"all:{q}"
        )

        xml_text = await self._query(search_query, max_results=min(limit, 2000))
        papers = self._parse_feed(xml_text)
        return papers[:limit]

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch a paper by DOI.

        arXiv-native DOIs (``10.48550/arXiv.<id>``, case-insensitive) are
        routed to the ``id_list`` lookup: the API's ``doi:"..."`` field search
        returns nothing for them even though the metadata carries the DOI
        (Y-1).  All other DOIs keep the ``doi:"..."`` field search.
        """
        cleaned = _normalize_doi(doi)
        if not cleaned:
            return None
        arxiv_id = _arxiv_id_from_doi(cleaned)
        if arxiv_id is not None:
            return await self.get_paper_by_arxiv_id(arxiv_id)
        xml_text = await self._query(f'doi:"{cleaned}"', max_results=5)
        papers = self._parse_feed(xml_text)
        for paper in papers:
            if paper.doi and paper.doi.lower() == cleaned.lower():
                return paper
        return papers[0] if papers else None

    async def get_paper_by_arxiv_id(self, arxiv_id: str) -> Paper | None:
        """Fetch a single paper by arXiv ID (convenience helper)."""
        parsed = _parse_arxiv_id(arxiv_id)
        if parsed is None:
            return None
        requested = _canonical_arxiv_id(parsed)
        # ``id_list`` provides exact identifier semantics for modern and old IDs.
        client = await self._client()
        params = {"id_list": parsed, "max_results": 1}
        try:
            response = await client.get(_API_BASE, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"arXiv request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    "arXiv rate limit exceeded",
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"arXiv request failed: {exc}",
                source_name=self.name,
            ) from exc
        if response.status_code == 429:
            raise RateLimitError(
                "arXiv rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "3") or 3),
            )
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"arXiv HTTP {response.status_code}",
                source_name=self.name,
            )
        papers = self._parse_feed(response.text)
        for paper in papers:
            candidate = paper.arxiv_id or paper.id
            if candidate is not None and _canonical_arxiv_id(candidate) == requested:
                return paper
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Search papers by author name using the ``au:`` field."""
        name = author_name.strip()
        if not name:
            return []
        # arXiv author search prefers last-name-first or free form
        xml_text = await self._query(f'au:"{name}"', max_results=50)
        return self._parse_feed(xml_text)

    async def get_author_profile(self, author_name: str) -> Author | None:
        """Build a lightweight author profile from search results.

        arXiv has no dedicated author profile endpoint; we aggregate from
        recent papers matching the author name.
        """
        name = author_name.strip()
        if not name:
            return None
        papers = await self.get_author_papers(name)
        if not papers:
            return None

        # Prefer an exact (case-insensitive) author match from paper lists
        matched_name = name
        interests: list[str] = []
        for paper in papers:
            for ref in paper.authors:
                if ref.name.lower() == name.lower() or name.lower() in ref.name.lower():
                    matched_name = ref.name
                    break
            for kw in paper.keywords:
                if kw and kw not in interests:
                    interests.append(kw)
            if len(interests) >= 10:
                break

        profile_url = f"https://arxiv.org/search/?query={quote_plus(matched_name)}&searchtype=author"
        return Author(
            id=None,
            name=matched_name,
            affiliation=None,
            email=None,
            homepage=None,
            h_index=None,
            citations=None,
            interests=interests[:10],
            profile_url=profile_url,
            evidence_list=[self._evidence(profile_url, raw={"paper_count": len(papers)})],
        )

    async def get_citations(self, paper_id: str) -> list[Citation]:
        """arXiv does not expose citation links; returns empty list."""
        return []
