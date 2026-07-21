"""
Academic Intelligence — A modular Python library for academic data collection.

Provides multi-source paper/author/citation acquisition with evidence tracking,
confidence scoring, deduplication, and incremental updates.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.models import (
    Author,
    ChangeDetection,
    ChangeType,
    Citation,
    CollectionResult,
    Evidence,
    IncrementalUpdateResult,
    Paper,
)
from academic_intelligence.core.types import AntiCrawlStrategy, Config, SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.processors.incremental import IncrementalProcessor
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.google_scholar import GoogleScholarSource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource
from academic_intelligence.storage.base import BaseStorage
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from academic_intelligence.utils.http import HTTPClient

__all__ = [
    "AcademicIntelligence",
    "Author",
    "ChangeDetection",
    "ChangeType",
    "Citation",
    "CollectionResult",
    "Config",
    "Evidence",
    "IncrementalUpdateResult",
    "Paper",
    "AntiCrawlStrategy",
    "SourceType",
]

__version__ = "0.1.0"

# Alias for SKILL.md-style: `from academic_intelligence import errors`
from academic_intelligence.core import exceptions as errors  # noqa: E402

__all__.append("errors")


class AcademicIntelligence:
    """Main entry point for the Academic Intelligence library."""

    def __init__(
        self,
        config: Optional[Union[Config, Dict[str, Any]]] = None,
    ) -> None:
        """Initialize the Academic Intelligence engine.

        Args:
            config: Optional Config model or dictionary.
        """
        if config is None:
            self.config = Config()
        elif isinstance(config, Config):
            self.config = config
        else:
            self.config = Config.model_validate(config)

        # Environment variable fallbacks for secrets
        if not self.config.serpapi_key:
            self.config.serpapi_key = os.environ.get("SERPAPI_KEY")
        if not self.config.semantic_scholar_api_key:
            self.config.semantic_scholar_api_key = os.environ.get(
                "SEMANTIC_SCHOLAR_API_KEY"
            )
        if not self.config.openalex_email:
            self.config.openalex_email = os.environ.get("OPENALEX_EMAIL")

        self._http: Optional[HTTPClient] = None
        self._storage: Optional[BaseStorage] = None
        self._sources: Dict[str, BaseSource] = {}
        self._collector: Optional[MultiSourceCollector] = None
        self._connected = False

    async def connect(self) -> None:
        """Initialize HTTP client, sources, and storage."""
        if self._connected:
            return

        self._http = HTTPClient(
            strategy=self.config.anti_crawl,
            proxies=self.config.proxy_list(),
            timeout=self.config.timeout,
            enable_cache=self.config.cache_enabled,
        )
        await self._http.connect()

        self._sources = self._build_sources(self.config.sources)
        self._storage = self._build_storage()
        await self._storage.connect()

        self._collector = MultiSourceCollector(
            config=self.config,
            sources=list(self._sources.values()),
        )
        self._connected = True

    async def close(self) -> None:
        """Release all resources."""
        if self._http is not None:
            await self._http.close()
            self._http = None
        if self._storage is not None:
            await self._storage.close()
            self._storage = None
        for source in self._sources.values():
            close = getattr(source, "close", None)
            if close is not None:
                await close()
        self._sources.clear()
        self._collector = None
        self._connected = False

    async def __aenter__(self) -> AcademicIntelligence:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def _build_storage(self) -> BaseStorage:
        if self.config.storage_type == "json":
            return JSONStorage(self.config.storage_path)
        return SQLiteStorage(self.config.storage_path)

    def _build_sources(self, source_names: Sequence[str]) -> Dict[str, BaseSource]:
        mapping: Dict[str, BaseSource] = {}
        aliases = {
            "gs": "google_scholar",
            "ss": "semantic_scholar",
            "s2": "semantic_scholar",
            "oa": "openalex",
            "google_scholar": "google_scholar",
            "semantic_scholar": "semantic_scholar",
            "openalex": "openalex",
        }
        for raw in source_names:
            name = aliases.get(raw.lower().strip(), raw.lower().strip())
            if name in mapping:
                continue
            if name == "google_scholar":
                mapping[name] = GoogleScholarSource(
                    http_client=self._http,
                    serpapi_key=self.config.serpapi_key,
                )
            elif name == "semantic_scholar":
                mapping[name] = SemanticScholarSource(
                    http_client=self._http,
                    api_key=self.config.semantic_scholar_api_key,
                )
            elif name == "openalex":
                mapping[name] = OpenAlexSource(
                    http_client=self._http,
                    email=self.config.openalex_email,
                )
            # Unsupported sources are skipped silently for forward compatibility
        return mapping

    def _resolve_sources(
        self,
        sources: Optional[Sequence[str]],
    ) -> List[BaseSource]:
        if sources is None:
            return list(self._sources.values())
        if len(sources) == 1 and sources[0].lower() in {"all", "*"}:
            return list(self._sources.values())
        # Rebuild subset (share HTTP client)
        selected = self._build_sources(list(sources))
        return list(selected.values())

    async def _ensure(self) -> MultiSourceCollector:
        if not self._connected or self._collector is None:
            await self.connect()
        assert self._collector is not None
        return self._collector

    @property
    def storage(self) -> BaseStorage:
        """Access the storage backend (connect first)."""
        if self._storage is None:
            raise RuntimeError("Not connected; call await ai.connect() first")
        return self._storage

    async def collect_author_papers(
        self,
        name: str,
        sources: Optional[List[str]] = None,
        *,
        persist: bool = False,
    ) -> CollectionResult:
        """Collect all papers by a given author.

        Args:
            name: Author name to search for.
            sources: List of source names to query.
            persist: Whether to save results to storage.

        Returns:
            CollectionResult containing papers, authors, and metadata.
        """
        collector = await self._ensure()
        active = self._resolve_sources(sources)
        result = await collector.collect_author_papers(name, sources=active)
        if persist:
            await self._persist(result)
        return result

    async def collect_paper(
        self,
        query: str,
        sources: Optional[List[str]] = None,
        *,
        persist: bool = False,
        limit: int = 10,
    ) -> CollectionResult:
        """Collect paper metadata by title or DOI."""
        collector = await self._ensure()
        active = self._resolve_sources(sources)
        result = await collector.collect_paper(query, sources=active, limit=limit)
        if persist:
            await self._persist(result)
        return result

    async def collect_citations(
        self,
        paper_id: str,
        sources: Optional[List[str]] = None,
        *,
        persist: bool = False,
    ) -> CollectionResult:
        """Collect citations for a given paper."""
        collector = await self._ensure()
        active = self._resolve_sources(sources)
        result = await collector.collect_citations(paper_id, sources=active)
        if persist:
            await self._persist(result)
        return result

    async def query_papers(
        self,
        *,
        author: Optional[str] = None,
        year: Optional[int] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        venue: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Paper]:
        """Query stored papers."""
        await self._ensure()
        return await self.storage.query_papers(
            author=author,
            year=year,
            year_from=year_from,
            year_to=year_to,
            venue=venue,
            keyword=keyword,
            limit=limit,
            offset=offset,
        )

    async def query_authors(
        self,
        *,
        name: Optional[str] = None,
        affiliation: Optional[str] = None,
        interest: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Author]:
        """Query stored authors."""
        await self._ensure()
        return await self.storage.query_authors(
            name=name,
            affiliation=affiliation,
            interest=interest,
            limit=limit,
            offset=offset,
        )

    async def get_stats(self) -> Dict[str, Any]:
        """Return storage statistics."""
        await self._ensure()
        return await self.storage.get_stats()

    async def update_author_papers(
        self,
        name: str,
        sources: list[str] | None = None,
    ) -> IncrementalUpdateResult:
        """Incrementally update papers for an author.

        Flow:
            1. Load stored papers for the author
            2. Collect fresh data from sources
            3. Deduplicate / fuse new data
            4. Detect changes (new / updated / unchanged)
            5. Apply only necessary writes
            6. Record per-source last-update timestamps
        """
        await self._ensure()
        processor = IncrementalProcessor(self.storage)

        old_papers = await self.storage.query_papers(author=name, limit=10_000)

        collection = await self.collect_author_papers(
            name,
            sources=sources,
            persist=False,
        )
        deduper = Deduplicator()
        fused = deduper.deduplicate_papers(collection.papers)

        result = await processor.detect_changes(fused, old_papers)
        await processor.apply_changes(result)

        now = datetime.now(timezone.utc)
        for src in result.sources_used or self._source_names(sources):
            await self.storage.save_last_update_time(src, now)

        return result

    async def update_paper(
        self,
        paper_id: str,
        sources: list[str] | None = None,
    ) -> IncrementalUpdateResult:
        """Incrementally update a single paper by stored id.

        Loads the existing paper, re-collects metadata using its title (or DOI
        as query), detects field-level changes, and applies a confidence-weighted
        merge when updates are found.
        """
        await self._ensure()
        processor = IncrementalProcessor(self.storage)

        old = await self.storage.get_paper(paper_id)
        if old is None:
            return IncrementalUpdateResult(
                new=[],
                updated=[],
                unchanged=[],
                total_checked=0,
                sources_used=self._source_names(sources),
            )

        query = old.doi or old.title
        collection = await self.collect_paper(
            query,
            sources=sources,
            persist=False,
            limit=10,
        )
        deduper = Deduplicator()
        fused = deduper.deduplicate_papers(collection.papers)

        # Ensure newly collected matches inherit the stored id when possible
        aligned: List[Paper] = []
        for p in fused:
            if p.id is None:
                aligned.append(p.model_copy(update={"id": paper_id}))
            else:
                aligned.append(p)

        result = await processor.detect_changes(aligned, [old])
        await processor.apply_changes(result)

        now = datetime.now(timezone.utc)
        for src in result.sources_used or self._source_names(sources):
            await self.storage.save_last_update_time(src, now)

        return result

    def _source_names(self, sources: Optional[Sequence[str]]) -> List[str]:
        if sources is None:
            return list(self._sources.keys())
        return [s.lower().strip() for s in sources]

    async def _persist(self, result: CollectionResult) -> None:
        await self.storage.save_batch(
            authors=result.authors,
            papers=result.papers,
            citations=result.citations,
        )
