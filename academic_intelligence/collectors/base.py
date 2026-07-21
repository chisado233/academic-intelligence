"""Collector base module for multi-source academic data collection."""

from __future__ import annotations

import abc
import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence

from academic_intelligence.core.exceptions import AllSourcesFailedError, CollectorError
from academic_intelligence.core.models import CollectionResult
from academic_intelligence.core.types import Config
from academic_intelligence.processors.deduplicator import Deduplicator, SimilarityConfig
from academic_intelligence.processors.enricher import Enricher
from academic_intelligence.processors.validator import Validator, ValidatorConfig
from academic_intelligence.sources.base import BaseSource

logger = logging.getLogger(__name__)


class BaseCollector(abc.ABC):
    """Abstract collector with multi-source orchestration helpers."""

    def __init__(
        self,
        config: Optional[Dict[str, Any] | Config] = None,
        sources: Optional[Sequence[BaseSource]] = None,
    ) -> None:
        if isinstance(config, Config):
            self.config_model: Config = config
            self.config: Dict[str, Any] = config.model_dump()
        else:
            self.config = dict(config or {})
            self.config_model = Config.model_validate(self.config) if config else Config()

        self.stats: Dict[str, Any] = {
            "requests_total": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "items_collected": 0,
        }
        self._sources: List[BaseSource] = list(sources or [])
        self.deduplicator = Deduplicator(
            SimilarityConfig(title_threshold=self.config_model.deduplication_threshold)
        )
        self.enricher = Enricher(min_confidence=self.config_model.min_confidence)
        self.validator = Validator(
            ValidatorConfig(min_confidence=self.config_model.min_confidence)
        )

    def setup(self) -> None:
        """Initialize collector (default: no-op if sources already provided)."""
        return None

    def teardown(self) -> None:
        """Release resources."""
        return None

    def __enter__(self) -> BaseCollector:
        self.setup()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.teardown()

    @abc.abstractmethod
    async def collect(self, query: str, **kwargs: Any) -> CollectionResult:
        """Execute collection for *query*."""
        ...

    def get_stats(self) -> Dict[str, Any]:
        return dict(self.stats)

    def reset_stats(self) -> None:
        self.stats = {
            "requests_total": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "items_collected": 0,
        }

    def _register_source(self, source: BaseSource) -> None:
        self._sources.append(source)

    def _update_stats(self, success: bool, items: int = 0) -> None:
        self.stats["requests_total"] += 1
        if success:
            self.stats["requests_success"] += 1
        else:
            self.stats["requests_failed"] += 1
        self.stats["items_collected"] += items

    async def _run_on_sources(
        self,
        method_name: str,
        *args: Any,
        sources: Optional[Sequence[BaseSource]] = None,
        **kwargs: Any,
    ) -> CollectionResult:
        """Call *method_name* on each source concurrently and aggregate."""
        active = list(sources if sources is not None else self._sources)
        if not active:
            raise CollectorError("No data sources registered")

        semaphore = asyncio.Semaphore(self.config_model.max_concurrent_sources)
        result = CollectionResult()
        sources_used: List[str] = []
        sources_failed: List[str] = []

        async def _call(source: BaseSource) -> None:
            method = getattr(source, method_name, None)
            if method is None:
                sources_failed.append(source.name)
                self._update_stats(False)
                return
            async with semaphore:
                try:
                    data = await method(*args, **kwargs)
                    self._update_stats(True, items=len(data) if isinstance(data, list) else 1)
                    sources_used.append(source.name)
                    if method_name in {
                        "search_papers",
                        "get_author_papers",
                    }:
                        result.papers.extend(data or [])
                    elif method_name == "get_paper_by_doi":
                        if data is not None:
                            result.papers.append(data)
                    elif method_name == "get_author_profile":
                        if data is not None:
                            result.authors.append(data)
                    elif method_name == "get_citations":
                        result.citations.extend(data or [])
                except Exception as exc:
                    logger.warning("Source %s failed on %s: %s", source.name, method_name, exc)
                    sources_failed.append(source.name)
                    result.errors.append(f"{source.name}: {exc}")
                    self._update_stats(False)

        await asyncio.gather(*[_call(s) for s in active])

        if not sources_used and sources_failed:
            raise AllSourcesFailedError(
                f"All sources failed for {method_name}",
                query=str(args[0]) if args else "",
                sources_attempted=[s.name for s in active],
            )

        # Process pipeline
        if result.papers:
            result.papers = self.enricher.cross_validate_papers(result.papers)
            result.papers = self.deduplicator.deduplicate_papers(result.papers)
            result.papers = self.enricher.enrich_papers(result.papers)
            result.papers = self.validator.filter_valid_papers(result.papers)

        if result.authors:
            result.authors = self.deduplicator.deduplicate_authors(result.authors)
            result.authors = self.enricher.enrich_authors(result.authors)

        confidences = [p.evidence.confidence for p in result.papers]
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        result.stats = {
            **self.get_stats(),
            "sources_used": sources_used,
            "sources_failed": sources_failed,
            "avg_confidence": avg_conf,
            "paper_count": len(result.papers),
            "author_count": len(result.authors),
            "citation_count": len(result.citations),
            "dedup": self.deduplicator.get_stats(),
        }
        return result


class MultiSourceCollector(BaseCollector):
    """Concrete multi-source collector for papers, authors, and citations."""

    async def collect(self, query: str, **kwargs: Any) -> CollectionResult:
        """Default collect: search papers by free-text query."""
        limit = int(kwargs.get("limit", 10))
        return await self._run_on_sources("search_papers", query, limit=limit)

    async def collect_author_papers(
        self,
        name: str,
        *,
        sources: Optional[Sequence[BaseSource]] = None,
    ) -> CollectionResult:
        """Collect papers and profile for an author."""
        papers_result = await self._run_on_sources(
            "get_author_papers",
            name,
            sources=sources,
        )
        try:
            profile_result = await self._run_on_sources(
                "get_author_profile",
                name,
                sources=sources,
            )
            papers_result.authors = profile_result.authors
            papers_result.errors.extend(profile_result.errors)
            papers_result.stats["author_count"] = len(papers_result.authors)
        except AllSourcesFailedError as exc:
            papers_result.errors.append(str(exc))
        return papers_result

    async def collect_paper(
        self,
        query: str,
        *,
        sources: Optional[Sequence[BaseSource]] = None,
        limit: int = 10,
    ) -> CollectionResult:
        """Collect paper by DOI or free-text search."""
        cleaned = query.strip()
        is_doi = cleaned.lower().startswith("10.") or "doi.org/" in cleaned.lower()
        if is_doi:
            doi = cleaned
            for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
                if doi.lower().startswith(prefix):
                    doi = doi[len(prefix) :].strip()
                    break
            return await self._run_on_sources(
                "get_paper_by_doi",
                doi,
                sources=sources,
            )
        return await self._run_on_sources(
            "search_papers",
            cleaned,
            limit=limit,
            sources=sources,
        )

    async def collect_citations(
        self,
        paper_id: str,
        *,
        sources: Optional[Sequence[BaseSource]] = None,
    ) -> CollectionResult:
        """Collect citation relationships for a paper id."""
        return await self._run_on_sources(
            "get_citations",
            paper_id,
            sources=sources,
        )
