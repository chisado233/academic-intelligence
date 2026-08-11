"""
Academic Intelligence — A modular Python library for academic data collection.

Provides multi-source paper/author/citation acquisition with evidence tracking,
confidence scoring, deduplication, and incremental updates.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from academic_intelligence.budget import BudgetManager
from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core import exceptions as errors
from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    ChangeDetection,
    ChangeType,
    Citation,
    CollectionResult,
    Evidence,
    ExpandResult,
    ExpandStats,
    IncrementalUpdateResult,
    Paper,
)
from academic_intelligence.core.types import AntiCrawlStrategy, Config, SourceType
from academic_intelligence.graph import KnowledgeGraph
from academic_intelligence.graph.traversal import expand_from_graph
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.processors.disambiguator import AuthorDisambiguator
from academic_intelligence.processors.incremental import (
    IncrementalProcessor,
    author_entity_key,
)
from academic_intelligence.processors.scorer import ConfidenceScorer
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.core_ import CoreSource
from academic_intelligence.sources.crossref import CrossrefSource
from academic_intelligence.sources.europe_pmc import EuropePmcSource
from academic_intelligence.sources.google_scholar import GoogleScholarSource
from academic_intelligence.sources.ieee import IEEESource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.sources.opencitations import OpenCitationsSource
from academic_intelligence.sources.pubmed import PubMedSource
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource
from academic_intelligence.sources.unpaywall import UnpaywallSource
from academic_intelligence.storage.base import BaseStorage
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.http import HTTPClient

logger = logging.getLogger(__name__)

__all__ = [
    "AcademicIntelligence",
    "Author",
    "AuthorRef",
    "ChangeDetection",
    "ChangeType",
    "Citation",
    "CollectionResult",
    "Config",
    "Evidence",
    "ExpandResult",
    "ExpandStats",
    "IncrementalUpdateResult",
    "Paper",
    "ConfidenceScorer",
    "AuthorDisambiguator",
    "AntiCrawlStrategy",
    "SourceType",
    # Public symbols referenced by SKILL.md (FIX-S S4): `from
    # academic_intelligence import *` must expose the same surface the docs
    # describe, not just the core models.
    "Deduplicator",
    "KnowledgeGraph",
    "IncrementalProcessor",
    "GoogleScholarSource",
    "SemanticScholarSource",
    "OpenAlexSource",
    "ArxivSource",
    "PubMedSource",
    "IEEESource",
    "BaseSource",
    "BaseStorage",
    "HTTPClient",
    "JSONStorage",
    "SQLiteStorage",
    "Cache",
    "expand_from_graph",
    "author_entity_key",
    "errors",
]

__version__ = "0.1.0"

# Source name aliases used by _build_sources / _source_names (B4 wiring).
_SOURCE_ALIASES = {
    "gs": "google_scholar",
    "ss": "semantic_scholar",
    "s2": "semantic_scholar",
    "oa": "openalex",
    "arxiv": "arxiv",
    "pubmed": "pubmed",
    "ieee": "ieee",
    "google_scholar": "google_scholar",
    "semantic_scholar": "semantic_scholar",
    "openalex": "openalex",
    # --- crawler upgrade 2026-08: new free sources ---
    "crossref": "crossref",
    "unpaywall": "unpaywall",
    "europe_pmc": "europe_pmc",
    "epmc": "europe_pmc",
    "opencitations": "opencitations",
    "coci": "opencitations",
    "core": "core",
}


def _canonical_source(raw: str) -> str:
    """Map a source identifier (or alias) to its canonical name."""
    return _SOURCE_ALIASES.get(raw.lower().strip(), raw.lower().strip())


def _secret_value(value: SecretStr | None) -> str | None:
    """Unwrap a Config secret field for adapter constructors (I-7)."""
    return value.get_secret_value() if value else None


class AcademicIntelligence:
    """Main entry point for the Academic Intelligence library."""

    def __init__(
        self,
        config: Config | dict[str, Any] | None = None,
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

        # Environment variable fallbacks for secrets (I-7: SecretStr fields).
        if not self.config.serpapi_key:
            env_serpapi = os.environ.get("SERPAPI_KEY")
            if env_serpapi:
                self.config.serpapi_key = SecretStr(env_serpapi)
        if not self.config.semantic_scholar_api_key:
            env_s2 = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
            if env_s2:
                self.config.semantic_scholar_api_key = SecretStr(env_s2)
        if not self.config.openalex_email:
            env_openalex = os.environ.get("OPENALEX_EMAIL")
            if env_openalex:
                self.config.openalex_email = SecretStr(env_openalex)
        if not self.config.ieee_api_key:
            env_ieee = os.environ.get("IEEE_API_KEY")
            if env_ieee:
                self.config.ieee_api_key = SecretStr(env_ieee)

        self._http: HTTPClient | None = None
        self._storage: BaseStorage | None = None
        self._sources: dict[str, BaseSource] = {}
        self._collector: MultiSourceCollector | None = None
        self._budget_manager: BudgetManager | None = None
        self._graph: KnowledgeGraph | None = None
        self._connected = False
        self._connect_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Initialize HTTP client, sources, and storage."""
        async with self._connect_lock:
            # Another caller may have completed initialization while this one
            # was waiting for the lifecycle lock.
            if self._connected:
                return
            try:
                self._http = HTTPClient(
                    strategy=self.config.anti_crawl,
                    proxies=self.config.proxy_list(),
                    timeout=self.config.timeout,
                    enable_cache=self.config.cache_enabled,
                    requests_per_second=self.config.rate_limit,
                    max_concurrent_requests=self.config.max_concurrent_requests,
                    # P17: a Cache instance must be handed in, otherwise
                    # ``enable_cache`` is a no-op and every request hits the network.
                    # Y-2: thread the Cache's existing disk persistence through the
                    # config so repeated queries across sessions hit the on-disk
                    # cache; ``persist_path=None`` falls back to the Cache default.
                    cache=(
                        Cache(
                            ttl=self.config.cache_ttl,
                            persistent=self.config.cache_persistent,
                            persist_path=self.config.cache_path,
                        )
                        if self.config.cache_enabled
                        else None
                    ),
                )
                await self._http.connect()

                self._sources = self._build_sources(self.config.sources)
                self._storage = self._build_storage()
                await self._storage.connect()

                # (IM-2/IM-3) Budget layer wiring: Config.budget overrides
                # the budget layer's DEFAULT_BUDGETS per source (an empty
                # config falls back to the defaults); the SQLite backend
                # doubles as the budget_usage store when it is the active
                # backend (JSON backend falls back to the in-memory store).
                # The manager is injected into the collector so requests are
                # pre-checked/consumed and error signals are fed back
                # (fail-soft skip, design §1.4).
                self._budget_manager = BudgetManager(
                    budgets=(
                        list(self.config.budget.values())
                        if self.config.budget
                        else None
                    ),
                    store=(
                        self._storage
                        if isinstance(self._storage, SQLiteStorage)
                        else None
                    ),
                )

                self._collector = MultiSourceCollector(
                    config=self.config,
                    sources=list(self._sources.values()),
                    budget_manager=self._budget_manager,
                )
                if self._graph is None:
                    self._graph = KnowledgeGraph(cache_size=self.config.graph_cache_size)
                self._connected = True
            except BaseException:
                # Cancellation is also a failed initialization and must not
                # strand a live HTTP client or storage handle.
                await self._rollback_connect()
                raise

    async def _rollback_connect(self) -> None:
        """Release everything initialized before a ``connect()`` failure."""
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                logger.debug(
                    "Error while closing HTTP client during connect rollback",
                    exc_info=True,
                )
            finally:
                self._http = None
        if self._storage is not None:
            try:
                await self._storage.close()
            except Exception:
                logger.debug(
                    "Error while closing storage during connect rollback",
                    exc_info=True,
                )
            finally:
                self._storage = None
        self._sources.clear()
        self._collector = None
        self._budget_manager = None
        self._connected = False

    async def close(self) -> None:
        """Release every resource, even when an earlier close operation fails."""
        close_errors: list[Exception] = []
        sources = list(self._sources.values())
        storage = self._storage
        http = self._http
        try:
            # Sources may own source-specific resources while sharing the
            # facade HTTP client, so close them before the shared client.
            for source in sources:
                close = getattr(source, "close", None)
                if close is None:
                    continue
                try:
                    await close()
                except Exception as exc:
                    close_errors.append(exc)
            if storage is not None:
                try:
                    await storage.close()
                except Exception as exc:
                    close_errors.append(exc)
            if http is not None:
                try:
                    await http.close()
                except Exception as exc:
                    close_errors.append(exc)
        finally:
            self._sources.clear()
            self._storage = None
            self._http = None
            self._collector = None
            self._budget_manager = None
            self._connected = False
        if close_errors:
            raise ExceptionGroup(
                "Errors while closing AcademicIntelligence", close_errors
            )

    async def __aenter__(self) -> AcademicIntelligence:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def _build_storage(self) -> BaseStorage:
        if self.config.storage_type == "json":
            return JSONStorage(self.config.storage_path)
        # (FIX-AE F2 / AE-2) Thread Config.sqlite_busy_timeout through to the
        # storage constructor so the SQLite busy_timeout is tunable instead
        # of hardcoded (P50 round-32 V1.4: 10s sits right at the 32-writer
        # contention boundary).
        return SQLiteStorage(
            self.config.storage_path,
            busy_timeout=self.config.sqlite_busy_timeout,
        )

    def _build_sources(self, source_names: Sequence[str]) -> dict[str, BaseSource]:
        mapping: dict[str, BaseSource] = {}
        for raw in source_names:
            name = _canonical_source(raw)
            if name in mapping:
                continue
            if name == "google_scholar":
                if not self.config.enable_google_scholar:
                    continue
                mapping[name] = GoogleScholarSource(
                    http_client=self._http,
                    serpapi_key=_secret_value(self.config.serpapi_key),
                )
            elif name == "semantic_scholar":
                mapping[name] = SemanticScholarSource(
                    http_client=self._http,
                    api_key=_secret_value(self.config.semantic_scholar_api_key),
                )
            elif name == "openalex":
                mapping[name] = OpenAlexSource(
                    http_client=self._http,
                    email=_secret_value(self.config.openalex_email),
                )
            elif name == "arxiv":
                mapping[name] = ArxivSource(http_client=self._http)
            elif name == "pubmed":
                mapping[name] = PubMedSource(http_client=self._http)
            elif name == "ieee":
                if not self.config.ieee_api_key:
                    logger.warning(
                        "IEEE Xplore registered without an API key; set "
                        "Config.ieee_api_key or the IEEE_API_KEY environment "
                        "variable, otherwise IEEE queries will fail at runtime "
                        "(IEEE degrades gracefully: single-source failures never "
                        "block the other sources)."
                    )
                mapping[name] = IEEESource(
                    http_client=self._http,
                    api_key=_secret_value(self.config.ieee_api_key),
                )
            elif name == "crossref":
                mapping[name] = CrossrefSource(
                    http_client=self._http,
                    mailto=_secret_value(self.config.crossref_mailto),
                )
            elif name == "unpaywall":
                mapping[name] = UnpaywallSource(
                    http_client=self._http,
                    email=_secret_value(self.config.unpaywall_email),
                )
            elif name == "europe_pmc":
                mapping[name] = EuropePmcSource(http_client=self._http)
            elif name == "opencitations":
                mapping[name] = OpenCitationsSource(http_client=self._http)
            elif name == "core":
                mapping[name] = CoreSource(
                    http_client=self._http,
                    api_key=_secret_value(self.config.core_api_key),
                )
            # Unsupported sources are skipped silently for forward compatibility
        return mapping

    def _resolve_sources(
        self,
        sources: Sequence[str] | None,
    ) -> list[BaseSource]:
        if sources is None:
            return list(self._sources.values())
        if len(sources) == 1 and sources[0].lower() in {"all", "*"}:
            return list(self._sources.values())
        # Rebuild subset (share HTTP client)
        selected = self._build_sources(list(sources))
        return list(selected.values())

    def source_capabilities(
        self,
        sources: Sequence[str] | None = None,
    ) -> dict[str, dict[str, bool]]:
        """Return declared source capabilities without opening connections."""
        if self._sources:
            active = self._resolve_sources(sources)
        else:
            requested = list(sources) if sources is not None else list(self.config.sources)
            active = list(self._build_sources(requested).values())
        return {source.name: dict(source.capabilities) for source in active}

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

    @property
    def budget_manager(self) -> BudgetManager | None:
        """Access the session budget manager (None before ``connect()``).

        ``paper budget`` / ``paper sources status`` (WP5 CLI) render
        ``budget_manager.status()`` and the collector's per-run budget
        report.
        """
        return self._budget_manager

    async def collect_author_papers(
        self,
        name: str,
        sources: list[str] | None = None,
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
            # (FIX-G F3) Record the (entity, source) sync timestamp so an
            # immediate update_author_papers is gated as fresh (G6).
            await self._record_entity_sync("author", author_entity_key(name), sources)
        return result

    async def collect_paper(
        self,
        query: str,
        sources: list[str] | None = None,
        *,
        persist: bool = False,
        limit: int = 10,
    ) -> CollectionResult:
        """Collect paper metadata by title or DOI."""
        # (FIX-M M7) Reject a negative limit up front, matching the
        # ``query_papers`` / ``query_authors`` ValueError contract — a
        # negative value would otherwise be silently clamped to 1 by the
        # source adapters.
        if limit < 0:
            raise ValueError("limit must be >= 0")
        collector = await self._ensure()
        active = self._resolve_sources(sources)
        result = await collector.collect_paper(query, sources=active, limit=limit)
        if persist:
            saved_ids = await self._persist(result)
            # (FIX-G F3) Record the paper entity sync so an immediate
            # update_paper is gated as fresh (G6). Saved ids align with
            # ``result.papers`` in input order.
            for paper_id in saved_ids.get("papers", []):
                await self._record_entity_sync("paper", paper_id, sources)
        return result

    async def collect_citations(
        self,
        paper_id: str,
        sources: list[str] | None = None,
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
        author: str | None = None,
        year: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        venue: str | None = None,
        keyword: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "id",
        after: str | None = None,
        cursor: str | None = None,
    ) -> list[Paper]:
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
            order_by=order_by,
            after=after,
            cursor=cursor,
        )

    async def query_authors(
        self,
        *,
        name: str | None = None,
        affiliation: str | None = None,
        interest: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "id",
        after: str | None = None,
        cursor: str | None = None,
    ) -> list[Author]:
        """Query stored authors."""
        await self._ensure()
        return await self.storage.query_authors(
            name=name,
            affiliation=affiliation,
            interest=interest,
            limit=limit,
            offset=offset,
            order_by=order_by,
            after=after,
            cursor=cursor,
        )

    async def get_stats(self) -> dict[str, Any]:
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
            6. Record per-(entity, source) and per-source last-update timestamps
        """
        await self._ensure()
        processor = IncrementalProcessor(self.storage)
        active_names = self._source_names(sources)
        author_key = author_entity_key(name)

        # Stale-refresh strategy (3A v2 §9.2, per-entity): only re-pull when
        # this author's recorded last-update time is older than
        # paper_refresh_days (or never synced). Per-(entity, source) gating
        # (I-5): syncing another author never blocks this one.
        if not await processor.is_stale(
            active_names,
            refresh_days=self.config.author_refresh_days,
            entity_type="author",
            entity_id=author_key,
        ):
            return IncrementalUpdateResult(
                new=[],
                updated=[],
                unchanged=[],
                total_checked=0,
                sources_used=active_names,
            )

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

        # (FIX-H F4 / H5) Only record the (entity, source) sync when the
        # collection actually succeeded — a fully-failed pass (e.g. a
        # misspelled author name) must not mark the entity as synced, which
        # would lock it out of retries for refresh_days.  Source errors ride
        # along on ``warnings`` so failures stay observable.
        if collection.errors:
            result.warnings.extend(collection.errors)
        succeeded = list(collection.stats.get("sources_used") or [])
        if result.total_checked > 0 or succeeded:
            now = datetime.now(UTC)
            for src in result.sources_used or succeeded:
                await self.storage.save_entity_sync("author", author_key, src, now)
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
        active_names = self._source_names(sources)

        # Stale-refresh strategy (3A v2 §9.2, per-entity): skip when this
        # paper entity was synced within the window (I-5).
        if not await processor.is_stale(
            active_names,
            refresh_days=self.config.paper_refresh_days,
            entity_type="paper",
            entity_id=paper_id,
        ):
            return IncrementalUpdateResult(
                new=[],
                updated=[],
                unchanged=[],
                total_checked=0,
                sources_used=active_names,
            )

        old = await self.storage.get_paper(paper_id)
        if old is None:
            return IncrementalUpdateResult(
                new=[],
                updated=[],
                unchanged=[],
                total_checked=0,
                sources_used=active_names,
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
        aligned: list[Paper] = []
        for p in fused:
            if p.id is None:
                aligned.append(p.model_copy(update={"id": paper_id}))
            else:
                aligned.append(p)

        result = await processor.detect_changes(aligned, [old])
        await processor.apply_changes(result)

        # (FIX-H F4 / H5) Same gating as ``update_author_papers``: a failed
        # collection must not record the paper entity sync, and source errors
        # are surfaced on ``warnings``.
        if collection.errors:
            result.warnings.extend(collection.errors)
        succeeded = list(collection.stats.get("sources_used") or [])
        if result.total_checked > 0 or succeeded:
            now = datetime.now(UTC)
            for src in result.sources_used or succeeded:
                await self.storage.save_entity_sync("paper", paper_id, src, now)
                await self.storage.save_last_update_time(src, now)

        return result

    def _source_names(self, sources: Sequence[str] | None) -> list[str]:
        if sources is None or any(
            source.lower().strip() in {"all", "*"} for source in sources
        ):
            return list(self._sources)
        return [_canonical_source(s) for s in sources]

    def _ensure_graph(self) -> KnowledgeGraph:
        """Return the session knowledge graph, creating it lazily if needed."""
        if self._graph is None:
            self._graph = KnowledgeGraph(cache_size=self.config.graph_cache_size)
        return self._graph

    def save_graph_snapshot(self, path: str | os.PathLike[str]) -> None:
        """Save the current session graph as a versioned atomic snapshot."""
        self._ensure_graph().save_snapshot(path)

    def load_graph_snapshot(self, path: str | os.PathLike[str]) -> None:
        """Replace the session graph with a validated snapshot."""
        self._graph = KnowledgeGraph.load_snapshot(
            path,
            cache_size=self.config.graph_cache_size,
        )

    # ------------------------------------------------------------------
    # Graph layer (3A v2 §7 / §10.1)
    # ------------------------------------------------------------------

    async def expand(
        self,
        entity_id: str,
        relations: list[str] | None = None,
        depth: int = 1,
        fetch_missing: bool = True,
        sources: list[str] | None = None,
    ) -> ExpandResult:
        """Expand *entity_id*'s relationships in the session knowledge graph.

        Relations are resolved storage-first (``get_references`` /
        ``get_citations`` / ``get_author_papers`` / ``get_coauthors``);
        storage misses are fetched from the data sources when
        ``fetch_missing`` is true.  Depth is clamped to
        ``Config.max_expand_depth`` and discovery is bounded by
        ``Config.max_expand_nodes`` (``stats.truncated`` is set on limits).

        Args:
            entity_id: Paper or author id to expand from.
            relations: Relation names to expand (``"references"``,
                ``"citations"``, ``"authors"``, ``"papers"``,
                ``"coauthors"``); ``None`` expands all applicable ones.
            depth: Number of BFS levels (default 1, max
                ``Config.max_expand_depth``).
            fetch_missing: Whether to fetch storage misses from the sources.
            sources: Optional source names to restrict fetching.

        Returns:
            ExpandResult with the newly discovered nodes/edges and stats.
        """
        await self._ensure()
        collector = await self._ensure()
        active = self._resolve_sources(sources)
        return await expand_from_graph(
            self._ensure_graph(),
            self.storage,
            collector,
            entity_id,
            relations=relations,
            depth=depth,
            fetch_missing=fetch_missing,
            sources=active,
            max_nodes=self.config.max_expand_nodes,
            max_depth=self.config.max_expand_depth,
        )

    async def subgraph(self, center_id: str, radius: int = 2) -> dict[str, Any]:
        """Return the subgraph around *center_id* within *radius* as a dict.

        The traversal is undirected (edges are followed both ways), matching
        ego-graph semantics.  Returns an empty subgraph when the center is not
        resident in the session graph (expand it first).

        Args:
            center_id: Center entity id.
            radius: Reachability radius (default 2).

        Returns:
            Serializable dict with ``nodes`` / ``edges`` / ``center`` /
            ``radius`` / counts.
        """
        await self._ensure()
        graph = self._ensure_graph()
        sub = graph.to_subgraph(center_id, radius)
        payload = sub.export_json()
        payload["center"] = center_id
        payload["radius"] = radius
        return payload

    async def path(self, source_id: str, target_id: str) -> list[str]:
        """Return the shortest directed association path between two entities.

        Uses BFS over the session graph (following edge direction).  Returns
        ``[]`` when no directed path exists or either endpoint is missing.

        Args:
            source_id: Start entity id.
            target_id: Target entity id.

        Returns:
            List of entity ids from *source_id* to *target_id* inclusive.
        """
        await self._ensure()
        return self._ensure_graph().shortest_path(source_id, target_id)

    async def _persist(self, result: CollectionResult) -> dict[str, list[str]]:
        """Persist a collection result; returns the saved ids in input order."""
        return await self.storage.save_batch(
            authors=result.authors,
            papers=result.papers,
            citations=result.citations,
        )

    async def _record_entity_sync(
        self,
        entity_type: str,
        entity_id: str,
        sources: Sequence[str] | None,
    ) -> None:
        """Record the (entity, source) sync timestamps for a persist (FIX-G F3).

        Keeps the stale gate (``update_author_papers`` / ``update_paper``)
        from re-pulling data that was just collected and persisted (G6).
        """
        now = datetime.now(UTC)
        for source in self._source_names(sources):
            await self.storage.save_entity_sync(entity_type, entity_id, source, now)
