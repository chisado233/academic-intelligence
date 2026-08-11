"""Locate legal open-access full-text links (fulltext/ locator).

Priority order (per upgrade technical-design §1.3):
Unpaywall -> CORE -> arXiv -> Europe PMC. Only *legal OA* links are ever
returned:

- Unpaywall (``is_oa=true`` locations) — the source only reports legal OA
  copies; ``host_type`` / license metadata is preserved for provenance.
- CORE — aggregator of legal OA full texts (requires the free ``CORE_API_KEY``).
- arXiv — the preprint's own ``arxiv.org/pdf/<id>`` PDF.
- Europe PMC — the adapter's own OA evidence (``fulltext_url`` / the
  ``fullTextXML`` endpoint) for records the upstream marks ``isOpenAccess=Y``;
  only open-access records qualify (E4).

Copyright red lines enforced here (functional-design §6):
- paywalled papers (Unpaywall ``is_oa=false``) yield *no* location — the
  pipeline then raises ``NoLegalOAFulltextError`` instead of bypassing;
- author self-archives are auto-accepted only when they are reachable via the
  trusted locators above; ResearchGate / Academia.edu (whose "author uploaded"
  status cannot be machine-verified) are always excluded as download sources.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from academic_intelligence.core.models import Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.fulltext.models import OALocation
from academic_intelligence.sources.europe_pmc import EuropePmcSource
from academic_intelligence.utils.http import HTTPClient

logger = logging.getLogger(__name__)

_UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}"
_CORE_SEARCH_API = "https://api.core.ac.uk/v3/search/works"

# Social platforms whose self-archived copies cannot be machine-verified as
# legal; never auto-download from them (functional-design §6.4).
_EXCLUDED_HOSTS = frozenset(
    {
        "researchgate.net",
        "www.researchgate.net",
        "academia.edu",
        "www.academia.edu",
    }
)

# Default locator priority — order matters (design §1.3).
DEFAULT_SOURCES: tuple[str, ...] = ("unpaywall", "core", "arxiv", "europe_pmc")


def _is_legal_oa_url(url: str) -> bool:
    """Return True when *url* is an absolute HTTP(S) URL on an allowed host."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = parsed.netloc.split(":")[0].lower()
    return host not in _EXCLUDED_HOSTS


class FulltextLocator:
    """Find a legal OA full-text link for a paper, in source priority order.

    Each source is tried in the order given by the caller (default
    Unpaywall -> CORE -> arXiv -> Europe PMC) and the first legal hit wins.
    A single source failure (network error, bad payload) fails soft: it is
    logged and the next source in priority is tried. ``None`` means no legal
    OA copy was found.
    """

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        unpaywall_email: str | None = None,
        core_api_key: str | None = None,
    ) -> None:
        """Initialize the locator.

        Args:
            http_client: Shared ``HTTPClient`` (must be connected by the
                caller). When omitted, Unpaywall/CORE lookups are skipped and
                only the arXiv path can resolve.
            unpaywall_email: Polite-pool email for the Unpaywall API.
                Defaults to the ``UNPAYWALL_EMAIL`` environment variable.
            core_api_key: Free CORE API key. Defaults to the ``CORE_API_KEY``
                environment variable.
        """
        self._http = http_client
        self._unpaywall_email = unpaywall_email or os.environ.get("UNPAYWALL_EMAIL")
        self._core_api_key = core_api_key or os.environ.get("CORE_API_KEY")

    async def locate(
        self,
        paper: Paper,
        sources: Sequence[str] = DEFAULT_SOURCES,
    ) -> OALocation | None:
        """Return the first legal OA location, or ``None`` when none exists.

        Args:
            paper: The paper to locate full text for. Uses ``paper.doi`` and
                ``paper.arxiv_id`` (no DOI -> only the arXiv path applies).
            sources: Locator order to try. Unknown names are ignored.

        Returns:
            The first legal ``OALocation``, or ``None`` if no source yielded a
            legal OA link.
        """
        for source in sources:
            if source not in {"unpaywall", "core", "arxiv", "europe_pmc"}:
                logger.debug("Unknown fulltext source %r; skipping", source)
                continue
            try:
                if source == "unpaywall":
                    location = await self._locate_unpaywall(paper)
                elif source == "core":
                    location = await self._locate_core(paper)
                elif source == "europe_pmc":
                    location = await self._locate_europe_pmc(paper)
                else:
                    location = self._locate_arxiv(paper)
            except Exception as exc:  # fail-soft per source (design §5)
                logger.warning(
                    "Fulltext locator %s failed for paper %r: %s",
                    source,
                    paper.id,
                    exc,
                )
                location = None
            if location is not None:
                return location
        return None

    async def _locate_unpaywall(self, paper: Paper) -> OALocation | None:
        """Query Unpaywall v2 for the paper's best legal OA location."""
        doi = paper.doi
        if not doi or not self._unpaywall_email or self._http is None:
            return None
        url = _UNPAYWALL_API.format(doi=doi)
        payload = await self._http.get_json(
            url,
            params={"email": self._unpaywall_email},
        )
        if not isinstance(payload, dict) or not payload.get("is_oa"):
            return None
        candidates: list[dict[str, Any]] = []
        best = payload.get("best_oa_location")
        if isinstance(best, dict):
            candidates.append(best)
        locations = payload.get("oa_locations") or []
        candidates.extend(
            location for location in locations if isinstance(location, dict)
        )
        for location in candidates:
            link = location.get("url_for_pdf") or location.get("url")
            if isinstance(link, str) and link and _is_legal_oa_url(link):
                license_value = location.get("license")
                return OALocation(
                    url=link,
                    source="unpaywall",
                    license=license_value if isinstance(license_value, str) else None,
                    host_type=(
                        location.get("host_type")
                        if isinstance(location.get("host_type"), str)
                        else None
                    ),
                )
        return None

    async def _locate_core(self, paper: Paper) -> OALocation | None:
        """Query CORE v3 search for the paper's OA download link."""
        doi = paper.doi
        if not doi or not self._core_api_key or self._http is None:
            return None
        payload = await self._http.get_json(
            _CORE_SEARCH_API,
            headers={"Authorization": f"Bearer {self._core_api_key}"},
            params={"q": f'doi:"{doi}"'},
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not results:
            return None
        first = results[0]
        if not isinstance(first, dict):
            return None
        link = first.get("downloadUrl")
        if not isinstance(link, str) or not link:
            links = first.get("links")
            if isinstance(links, dict):
                full_text = links.get("fullText")
                link = full_text if isinstance(full_text, str) else None
        if isinstance(link, str) and link and _is_legal_oa_url(link):
            return OALocation(url=link, source="core")
        return None

    async def _locate_europe_pmc(self, paper: Paper) -> OALocation | None:
        """Reuse the Europe PMC adapter's own OA full-text evidence (E4).

        Two layers: the paper's in-memory Europe PMC evidence first (zero
        requests — e.g. right after a ``source europe_pmc get``), then a live
        Europe PMC query for its OA status when the paper carries no Europe
        PMC evidence (e.g. ``paper fulltext <doi>`` resolved through another
        source).  Only records the upstream marks ``isOpenAccess=Y`` are used:
        Europe PMC full text is served exclusively for the open-access subset
        (no paywall bypass), so non-OA papers still yield *no* location.
        """
        if any(e.source is SourceType.EUROPE_PMC for e in paper.evidence_list):
            # The paper already knows its Europe PMC OA status — local verdict.
            return self._europe_pmc_location(paper)
        doi = paper.doi
        if not doi or self._http is None:
            return None
        source = EuropePmcSource(http_client=self._http)
        resolved = await source.get_paper_by_doi(doi)
        if resolved is None:
            return None
        return self._europe_pmc_location(resolved)

    @staticmethod
    def _europe_pmc_location(paper: Paper) -> OALocation | None:
        """Extract the Europe PMC fullTextXML location from *paper*'s evidence.

        Only ``is_open_access`` records with a fullTextXML URL qualify; a
        non-OA record yields ``None`` so paywalled papers are still explicitly
        rejected downstream.
        """
        for evidence in paper.evidence_list:
            if evidence.source is not SourceType.EUROPE_PMC:
                continue
            raw = evidence.raw_data or {}
            if raw.get("is_open_access") is not True:
                return None
            url = raw.get("fulltext_url")
            if isinstance(url, str) and url and _is_legal_oa_url(url):
                return OALocation(url=url, source="europe_pmc")
        return None

    @staticmethod
    def _locate_arxiv(paper: Paper) -> OALocation | None:
        """Derive the arXiv PDF URL directly from the paper's arXiv id."""
        arxiv_id = (paper.arxiv_id or "").strip()
        if not arxiv_id:
            return None
        url = f"https://arxiv.org/pdf/{arxiv_id}"
        return OALocation(url=url, source="arxiv")
