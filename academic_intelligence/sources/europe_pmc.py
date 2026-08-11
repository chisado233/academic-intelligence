"""Europe PMC data source adapter (biomedical metadata + OA full text).

Uses the Europe PMC REST API (free, no API key required)::

    https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=...&format=json&pageSize=N&resultType=core
    https://www.ebi.ac.uk/europepmc/webservices/rest/PMC{pmcid}/fullTextXML

Europe PMC indexes PubMed (MED) metadata plus the PMC open-access subset.
The adapter therefore complements :class:`PubMedSource` (NCBI E-utilities):
MED records provide metadata, PMC records additionally carry the *legal*
open-access full-text XML (``isOpenAccess=Y``) served from the
``fullTextXML`` endpoint — ``get_fulltext`` only fetches when the record is
marked open access (no paywall bypass).

Capabilities: ``search`` and ``get`` (by DOI / PMID / PMCID) are supported;
``fulltext`` is supported for open-access records; author-class and citation
operations are not provided (methods raise :class:`NotSupportedError`).

Verified field shapes (2026-08 live probe): search results under
``resultList.result[]`` carry ``id``/``source`` (``MED``/``PMC``/``PPR``),
``pmid``/``pmcid``/``doi``/``title``, ``authorList.author[]`` with
``fullName`` + ``authorAffiliationDetailsList.authorAffiliation[].affiliation``,
``pubYear`` (string), ``journalInfo.journal.title`` /
``medlineAbbreviation``, ``abstractText``, ``isOpenAccess`` (``Y``/``N``),
``inEPMC`` (``Y``/``N``) and ``fullTextUrlList.fullTextUrl[]``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

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
    normalize_pmid,
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

_API_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_SEARCH_PATH = "/search"
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")
# Europe PMC ``pageSize`` is capped at 1000 per request.
_MAX_PAGE_SIZE = 1000
# Model-level caps (Paper.title=500, Paper.abstract=20000): defensive
# truncation keeps an over-long field from dropping the whole record.
_TITLE_MAX = 500
_ABSTRACT_MAX = 20000
# ``abstractText`` often carries light HTML markup (``<p>`` / ``<title>``).
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_doi(doi: str) -> str:
    """Strip ``https://doi.org/`` / ``doi:`` prefixes from *doi*."""
    cleaned = doi.strip()
    for prefix in _DOI_PREFIXES:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def _strip_markup(value: Any) -> str | None:
    """Strip light HTML markup from an abstract/author string and collapse space."""
    if not isinstance(value, str):
        return None
    cleaned = _HTML_TAG_RE.sub(" ", value)
    collapsed = " ".join(cleaned.split())
    return collapsed or None


def _pmc_digits(pmcid: Any) -> str | None:
    """Return the bare digits of a PMCID (``PMC7292645`` -> ``7292645``)."""
    if not isinstance(pmcid, str):
        return None
    value = pmcid.strip()
    if value.upper().startswith("PMC"):
        value = value[3:]
    return value or None


class EuropePmcSource(BaseSource):
    """Europe PMC REST API source (biomedical metadata + OA full text).

    Capabilities: ``search`` / ``get`` (DOI/PMID/PMCID) / ``fulltext`` (OA
    records only); author-class and citation operations are unsupported and
    raise :class:`NotSupportedError`.
    """

    name = "europe_pmc"
    source_type = SourceType.EUROPE_PMC
    capabilities = {
        **BaseSource.capabilities,
        # Author-class and citation operations are not provided by Europe PMC.
        "get_author_papers": False,
        "get_author_profile": False,
        "get_citations": False,
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
        confidence: float = 0.90,
        requests_per_second: float = 1.0,
    ) -> None:
        self._http = http_client
        self._owns_client = http_client is None
        self.confidence = confidence
        self._requests_per_second = max(requests_per_second, 0.1)

    async def _client(self) -> HTTPClient:
        if self._http is None:
            base_delay = 1.0 / self._requests_per_second
            strategy = AntiCrawlStrategy(base_delay=base_delay, adaptive_delay=False)
            self._http = HTTPClient(
                strategy=strategy,
                rate_limiter=create_rate_limiter(
                    "fixed", requests_per_second=self._requests_per_second
                ),
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

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """GET *path* on the Europe PMC API with the standard error mapping."""
        client = await self._client()
        url = f"{_API_BASE}{path}"
        try:
            response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Europe PMC request timed out: {exc}",
                source_name=self.name,
            ) from exc
        except Exception as exc:
            if is_rate_limit_status(exc):
                raise RateLimitError(
                    "Europe PMC rate limit exceeded",
                    source_name=self.name,
                    retry_after=retry_after_from_error(exc),
                ) from exc
            raise SourceUnavailableError(
                f"Europe PMC request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "Europe PMC rate limit exceeded",
                source_name=self.name,
                retry_after=int(response.headers.get("Retry-After", "1") or 1),
            )
        if response.status_code == 404:
            # Unknown id / no full text available — caller decides.
            return response
        if response.status_code >= 400:
            raise SourceUnavailableError(
                f"Europe PMC HTTP {response.status_code}",
                source_name=self.name,
                context={"body": response.text[:500]},
            )
        return response

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        response = await self._get(path, params)
        if response.status_code == 404:
            return None
        try:
            return response.json()
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from Europe PMC: {exc}",
                source_name=self.name,
                raw_snippet=response.text[:300],
            ) from exc

    async def _get_text(self, path: str) -> str | None:
        response = await self._get(path)
        if response.status_code == 404:
            return None
        return response.text

    def _parse_authors(self, data: Any) -> list[AuthorRef]:
        """Parse Europe PMC ``authorList.author[]`` into :class:`AuthorRef`."""
        authors: list[AuthorRef] = []
        if not isinstance(data, dict):
            return authors
        for i, author in enumerate((data.get("author") or []), start=1):
            if not isinstance(author, dict):
                continue
            name = _strip_markup(author.get("fullName"))
            if not name:
                continue
            affiliation: str | None = None
            details = author.get("authorAffiliationDetailsList")
            if isinstance(details, dict):
                for aff in details.get("authorAffiliation") or []:
                    if isinstance(aff, dict):
                        affiliation = _strip_markup(aff.get("affiliation")) or affiliation
            authors.append(AuthorRef(name=name, position=i, affiliation=affiliation))
        return authors

    def _parse_year(self, data: Any) -> int | None:
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

    def _parse_oa_links(self, data: dict[str, Any]) -> tuple[str | None, str | None]:
        """Extract (pdf_url, first open-access full-text url) from ``fullTextUrlList``."""
        pdf_url: str | None = None
        oa_url: str | None = None
        urls = data.get("fullTextUrlList") or {}
        for entry in urls.get("fullTextUrl") or []:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            availability = str(entry.get("availability") or "").lower()
            style = str(entry.get("documentStyle") or "").lower()
            if availability == "open access":
                if oa_url is None:
                    oa_url = url.strip()
                if style == "pdf" and pdf_url is None:
                    pdf_url = url.strip()
        return pdf_url, oa_url

    def _parse_paper(self, data: dict[str, Any]) -> Paper | None:
        """Map one Europe PMC search result onto the :class:`Paper` model."""
        raw_title = data.get("title")
        title = str(raw_title).strip() if isinstance(raw_title, str) else ""
        if not title:
            return None
        title = title[:_TITLE_MAX]

        record_id = data.get("id")
        source = str(data.get("source") or "MED")
        pmid = normalize_pmid(str(data.get("pmid")) if data.get("pmid") else None)
        pmcid = data.get("pmcid") if isinstance(data.get("pmcid"), str) else None
        doi_raw = data.get("doi")
        doi = normalize_doi(doi_raw) if isinstance(doi_raw, str) else None
        is_open_access = str(data.get("isOpenAccess") or "N") == "Y"
        in_epmc = str(data.get("inEPMC") or "N") == "Y"

        year = self._parse_year(data.get("pubYear"))

        journal_info = data.get("journalInfo") or {}
        journal = journal_info.get("journal") or {}
        venue = None
        if isinstance(journal, dict):
            venue = _strip_markup(journal.get("title")) or _strip_markup(
                journal.get("medlineAbbreviation")
            )

        abstract = _strip_markup(data.get("abstractText"))
        if abstract:
            abstract = abstract[:_ABSTRACT_MAX]

        pdf_url, oa_url = self._parse_oa_links(data)

        # Canonical article page, e.g. https://europepmc.org/article/MED/33033895
        record_id_str = str(record_id) if record_id is not None else None
        article_page = (
            f"https://europepmc.org/article/{source}/{record_id_str}"
            if record_id_str
            else "https://europepmc.org"
        )

        # Full-text XML endpoint for open-access records with a PMCID.
        fulltext_url: str | None = None
        if is_open_access and pmcid is not None:
            digits = _pmc_digits(pmcid)
            if digits:
                fulltext_url = f"{_API_BASE}/PMC{digits}/fullTextXML"

        evidence = self._evidence(
            article_page,
            raw={
                "source": source,
                "record_id": record_id_str,
                "pmcid": pmcid,
                "is_open_access": is_open_access,
                "in_epmc": in_epmc,
                "fulltext_url": fulltext_url,
                "oa_url": oa_url,
            },
            source_id=pmcid or pmid or record_id_str,
        )

        return Paper(
            id=pmcid or pmid or record_id_str,
            title=title,
            authors=self._parse_authors(data.get("authorList")),
            year=year,
            venue=venue,
            abstract=abstract,
            doi=doi,
            pmid=pmid,
            url=article_page if article_page.startswith("http") else None,
            pdf_url=pdf_url,
            citations=None,
            keywords=[],
            evidence_list=[evidence],
        )

    def _parse_results(self, data: Any) -> list[Paper]:
        if not isinstance(data, dict):
            return []
        papers: list[Paper] = []
        for item in (data.get("resultList") or {}).get("result") or []:
            if not isinstance(item, dict):
                continue
            try:
                paper = self._parse_paper(item)
            except Exception as exc:
                logger.debug("Skip Europe PMC paper: %s", exc)
                continue
            if paper is not None:
                papers.append(paper)
        return papers

    # ------------------------------------------------------------------
    # Paper queries
    # ------------------------------------------------------------------
    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Search Europe PMC by free-text *query*.

        ``resultType=core`` is requested so the response carries the abstract,
        author list, journal info and full-text URLs needed for the
        :class:`~academic_intelligence.core.models.Paper` mapping.
        """
        q = query.strip()
        if not q:
            return []
        data = await self._get_json(
            _SEARCH_PATH,
            {
                "query": q,
                "format": "json",
                "pageSize": min(max(limit, 1), _MAX_PAGE_SIZE),
                "resultType": "core",
            },
        )
        return self._parse_results(data)[:limit]

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch a Europe PMC record by DOI (``DOI:"..."`` field query)."""
        cleaned = normalize_doi(_normalize_doi(doi))
        if cleaned is None:
            return None
        data = await self._get_json(
            _SEARCH_PATH,
            {
                "query": f'DOI:"{cleaned}"',
                "format": "json",
                "pageSize": 5,
                "resultType": "core",
            },
        )
        papers = self._parse_results(data)
        for paper in papers:
            if paper.doi and paper.doi.lower() == cleaned.lower():
                return paper
        return papers[0] if papers else None

    async def get_paper_by_pmid(self, pmid: str) -> Paper | None:
        """Fetch a Europe PMC record by PubMed ID (``EXT_ID:`` lookup).

        Args:
            pmid: PubMed ID (1-8 digits).
        """
        cleaned = normalize_pmid(pmid.strip() or None)
        if cleaned is None:
            return None
        data = await self._get_json(
            _SEARCH_PATH,
            {
                "query": f"EXT_ID:{cleaned}",
                "format": "json",
                "pageSize": 1,
                "resultType": "core",
            },
        )
        papers = self._parse_results(data)
        return papers[0] if papers else None

    async def get_paper_by_pmcid(self, pmcid: str) -> Paper | None:
        """Fetch a Europe PMC record by PubMed Central ID.

        Args:
            pmcid: PMCID, with or without the ``PMC`` prefix
                (``"PMC7292645"`` / ``"7292645"``).
        """
        digits = _pmc_digits(pmcid)
        if digits is None:
            return None
        data = await self._get_json(
            _SEARCH_PATH,
            {
                "query": f"PMCID:PMC{digits}",
                "format": "json",
                "pageSize": 1,
                "resultType": "core",
            },
        )
        papers = self._parse_results(data)
        return papers[0] if papers else None

    async def get_fulltext(self, paper: Paper) -> str | None:
        """Return the legal OA full-text XML for *paper*, or ``None``.

        Only fetched when the paper carries Europe PMC evidence marked
        ``is_open_access`` (the record's ``isOpenAccess=Y``) and has a PMCID;
        otherwise ``None`` is returned without a request — Europe PMC full
        text is served exclusively for the open-access subset (no paywall
        bypass).  HTTP 404 (full text not actually available) also yields
        ``None``.
        """
        for evidence in paper.evidence_list:
            if evidence.source is not SourceType.EUROPE_PMC:
                continue
            raw = evidence.raw_data or {}
            if raw.get("is_open_access") is not True:
                return None
            pmcid = raw.get("pmcid")
            if not isinstance(pmcid, str):
                return None
            digits = _pmc_digits(pmcid)
            if digits is None:
                return None
            return await self._get_text(f"/PMC{digits}/fullTextXML")
        return None

    # ------------------------------------------------------------------
    # Unsupported operations (Europe PMC has no author/citation endpoints)
    # ------------------------------------------------------------------
    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Not supported: Europe PMC exposes no author-works operation."""
        raise NotSupportedError(
            "Europe PMC does not support author-paper queries",
            source_name=self.name,
        )

    async def get_author_profile(self, author_name: str) -> Author | None:
        """Not supported: Europe PMC exposes no author profiles."""
        raise NotSupportedError(
            "Europe PMC does not support author profiles",
            source_name=self.name,
        )

    async def get_citations(self, paper_id: str) -> list[Citation]:
        """Not supported: Europe PMC exposes no citation graph."""
        raise NotSupportedError(
            "Europe PMC does not support citation queries",
            source_name=self.name,
        )
