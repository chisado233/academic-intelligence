"""OpenAlex data source adapter.

Uses the public OpenAlex API: https://api.openalex.org
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from academic_intelligence.core.exceptions import (
    ParseError,
    RateLimitError,
    SourceUnavailableError,
)
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.utils.http import HTTPClient

logger = logging.getLogger(__name__)

_API_BASE = "https://api.openalex.org"


class OpenAlexSource(BaseSource):
    """OpenAlex API source."""

    name = "openalex"
    source_type = SourceType.OPENALEX

    def __init__(
        self,
        http_client: Optional[HTTPClient] = None,
        *,
        email: Optional[str] = None,
        confidence: float = 0.88,
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

    def _params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = dict(extra or {})
        if self.email:
            params["mailto"] = self.email
        return params

    def _evidence(self, url: str, raw: Optional[Dict[str, Any]] = None) -> Evidence:
        return Evidence(
            source=self.source_type,
            source_url=url,
            collected_at=datetime.now(timezone.utc),
            confidence=self.confidence,
            raw_data=raw,
        )

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        client = await self._client()
        url = f"{_API_BASE}{path}"
        try:
            response = await client.get(url, params=self._params(params))
        except Exception as exc:
            raise SourceUnavailableError(
                f"OpenAlex request failed: {exc}",
                source_name=self.name,
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "OpenAlex rate limit exceeded",
                source_name=self.name,
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

    def _parse_paper(self, data: Dict[str, Any]) -> Paper:
        title = (data.get("title") or data.get("display_name") or "").strip() or "Untitled"
        authorships = data.get("authorships") or []
        authors: List[str] = []
        for item in authorships:
            author = (item or {}).get("author") or {}
            name = author.get("display_name")
            if name:
                authors.append(str(name))

        ids = data.get("ids") or {}
        doi_raw = data.get("doi") or ids.get("doi")
        doi: Optional[str] = None
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
        keywords: List[str] = []
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

        # Validate DOI softly
        safe_doi = doi
        if doi:
            try:
                Paper.model_validate(
                    {
                        "title": title,
                        "doi": doi,
                        "evidence": self._evidence(url or "https://openalex.org"),
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
            evidence=self._evidence(url or "https://openalex.org", raw=data),
        )

    def _parse_author(self, data: Dict[str, Any]) -> Author:
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
            affiliation=affiliation,
            email=None,
            homepage=None,
            h_index=summary.get("h_index") if isinstance(summary, dict) else None,
            citations=data.get("cited_by_count"),
            interests=[],
            profile_url=profile_url if profile_url and profile_url.startswith("http") else None,
            evidence=self._evidence(profile_url or "https://openalex.org", raw=data),
        )

    async def search_papers(self, query: str, limit: int = 10) -> List[Paper]:
        """Search works on OpenAlex."""
        data = await self._get_json(
            "/works",
            params={"search": query, "per_page": min(limit, 50)},
        )
        if not data:
            return []
        papers: List[Paper] = []
        for item in data.get("results") or []:
            if isinstance(item, dict):
                try:
                    papers.append(self._parse_paper(item))
                except Exception as exc:
                    logger.debug("Skip OpenAlex paper: %s", exc)
            if len(papers) >= limit:
                break
        return papers

    async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
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

    async def get_author_papers(self, author_name: str) -> List[Paper]:
        """Search authors, then list their works."""
        search = await self._get_json(
            "/authors",
            params={"search": author_name, "per_page": 1},
        )
        if not search or not (search.get("results") or []):
            return await self.search_papers(author_name, limit=20)

        author = search["results"][0]
        author_id = author.get("id")
        if not author_id:
            return await self.search_papers(author_name, limit=20)

        # Filter works by author id
        data = await self._get_json(
            "/works",
            params={"filter": f"author.id:{author_id}", "per_page": 50},
        )
        if not data:
            return []
        papers: List[Paper] = []
        for item in data.get("results") or []:
            if isinstance(item, dict):
                try:
                    papers.append(self._parse_paper(item))
                except Exception as exc:
                    logger.debug("Skip author work: %s", exc)
        return papers

    async def get_author_profile(self, author_name: str) -> Optional[Author]:
        """Search author profile by name."""
        search = await self._get_json(
            "/authors",
            params={"search": author_name, "per_page": 1},
        )
        if not search or not (search.get("results") or []):
            return None
        item = search["results"][0]
        if not isinstance(item, dict):
            return None
        return self._parse_author(item)

    async def get_citations(self, paper_id: str) -> List[Citation]:
        """Get works that cite the given OpenAlex work id."""
        # Accept bare W-id or full URL
        work_id = paper_id.rstrip("/").split("/")[-1]
        data = await self._get_json(
            "/works",
            params={"filter": f"cites:{work_id}", "per_page": 50},
        )
        if not data:
            return []
        source_url = f"https://openalex.org/{work_id}"
        citations: List[Citation] = []
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
