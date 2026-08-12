"""Collector base module for multi-source academic data collection."""

from __future__ import annotations

import abc
import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from academic_intelligence.budget import BudgetKind, BudgetManager
from academic_intelligence.core.exceptions import (
    AllSourcesFailedError,
    CollectorError,
    SourceFailure,
)
from academic_intelligence.core.models import CollectionResult
from academic_intelligence.core.types import Config
from academic_intelligence.processors.deduplicator import Deduplicator, SimilarityConfig
from academic_intelligence.processors.disambiguator import (
    AuthorDisambiguator,
    DisambiguationConfig,
)
from academic_intelligence.processors.enricher import Enricher
from academic_intelligence.processors.validator import Validator, ValidatorConfig
from academic_intelligence.sources.arxiv import _parse_arxiv_id
from academic_intelligence.sources.base import BaseSource

logger = logging.getLogger(__name__)

# (IM-2) Locally-estimated per-request cost for USD/credit-class sources
# (design §1.4 post-metering).  A single metered request's cost is not
# knowable up front, so the collector accumulates a proportionate
# estimate: the OpenAlex free-tier scale (~1.0 USD / day for roughly
# 100k requests) puts one request at ~1e-5 of the daily limit.
# req-class sources consume exactly 1.0 (one request).  The primary
# breaker for USD-class sources remains the billing/rate-limit signal
# fed through BudgetManager.report_failure.
_METERED_COST_PER_REQUEST = 1e-5


def _request_cost(budget_manager: BudgetManager, source: str) -> float:
    """Return the post-request consume cost for *source*."""
    if budget_manager.semantics_for(source) is BudgetKind.METERED:
        return _METERED_COST_PER_REQUEST
    return 1.0

# OpenAlex work id query shape: bare ``W123`` or the full openalex URL.  The
# collector routes these to sources that expose a by-id lookup (FIX-B1 F3) so
# placeholder ids from the citation graph can be backfilled; sources without
# the capability keep their original search path.
_WORK_ID_RE = re.compile(r"^(?:https?://openalex\.org/)?(W\d+)/?$", re.IGNORECASE)

# (Q4) Suspicious DOI prefixes observed on polluted records — e.g. the
# "Attention Is All You Need" -> DOI 10.65215/2q58a426 contamination that
# OpenAlex ranked top-1 for the canonical 2017 paper (P35 Q4).  A record
# carrying one of these prefixes is flagged in ``CollectionResult.warnings``
# instead of being silently trusted (cross-source quality gate).
_SUSPICIOUS_DOI_PREFIXES = frozenset({"10.65215"})


def _suspicious_doi(doi: str | None) -> str | None:
    """Return the suspicious DOI prefix carried by *doi*, or ``None``.

    Prefix matching (``doi.lower().startswith(prefix)``) so ``10.65215/abc``
    and ``10.65215/xyz`` are both caught by the single ``10.65215`` entry.
    """
    if not doi:
        return None
    lower = doi.strip().lower()
    for prefix in _SUSPICIOUS_DOI_PREFIXES:
        if lower.startswith(prefix):
            return prefix
    return None


def _year_anomaly(year: int | None) -> str | None:
    """Return a description when *year* is implausible, else ``None``.

    ``Paper`` validation already bounds years to [1800, current+1], so the
    anomalies reachable here are records published in the future (year ==
    current+1) — a strong signal the record was polluted or mis-labeled.
    """
    if year is None:
        return None
    current = datetime.now(UTC).year
    if year > current:
        return f"publication year {year} is in the future (current year {current})"
    if year < 1800:
        return f"publication year {year} predates scholarly publication"
    return None


def _source_supports(source: BaseSource, operation: str) -> bool:
    """Read a declared capability, falling back to legacy duck typing."""
    supports = getattr(source, "supports", None)
    if callable(supports):
        return bool(supports(operation))
    return callable(getattr(source, operation, None))


def _source_capabilities(source: BaseSource) -> dict[str, bool]:
    """Return declared or method-derived capabilities for collector stats."""
    declared = getattr(source, "capabilities", None)
    if isinstance(declared, Mapping):
        return {str(name): bool(value) for name, value in declared.items()}
    return {
        operation: callable(getattr(source, operation, None))
        for operation in BaseSource.capabilities
    }


class BaseCollector(abc.ABC):
    """Abstract collector with multi-source orchestration helpers."""

    def __init__(
        self,
        config: dict[str, Any] | Config | None = None,
        sources: Sequence[BaseSource] | None = None,
        budget_manager: BudgetManager | None = None,
    ) -> None:
        if isinstance(config, Config):
            self.config_model: Config = config
            self.config: dict[str, Any] = config.model_dump()
        else:
            self.config = dict(config or {})
            self.config_model = Config.model_validate(self.config) if config else Config()

        self.stats: dict[str, Any] = {
            "requests_total": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "items_collected": 0,
        }
        self._sources: list[BaseSource] = list(sources or [])
        # (IM-2) Per-source quota enforcement (design §1.4).  When
        # configured, requests are pre-checked, consumed, and error
        # signals fed back through the BudgetManager; over-limit
        # sources are skipped fail-soft (never fatal).
        self.budget_manager = budget_manager
        self.deduplicator = Deduplicator(
            SimilarityConfig(title_threshold=self.config_model.deduplication_threshold)
        )
        self.author_disambiguator = AuthorDisambiguator(
            DisambiguationConfig(
                auto_merge_threshold=self.config_model.auto_merge_threshold,
                ambiguous_threshold=self.config_model.ambiguous_threshold,
            )
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

    def get_stats(self) -> dict[str, Any]:
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
        sources: Sequence[BaseSource] | None = None,
        **kwargs: Any,
    ) -> CollectionResult:
        """Call *method_name* on each source concurrently and aggregate."""
        active = list(sources if sources is not None else self._sources)
        if not active:
            raise CollectorError("No data sources registered")

        semaphore = asyncio.Semaphore(self.config_model.max_concurrent_sources)
        result = CollectionResult()
        sources_used: list[str] = []
        sources_failed: list[str] = []
        failures: dict[str, SourceFailure] = {}
        # (IM-2) Sources skipped by the budget layer (fail-soft,
        # design §1.4): recorded for reporting, never fatal — an
        # all-budget-skip collection must not raise
        # AllSourcesFailedError.
        budget_skipped: list[str] = []

        async def _call(source: BaseSource) -> None:
            method = getattr(source, method_name, None)
            if method is None or not _source_supports(source, method_name):
                failure = SourceFailure.from_message(
                    source=source.name,
                    operation=method_name,
                    error_type="UnsupportedOperation",
                    message=f"operation {method_name} is not supported",
                    permanent=True,
                )
                sources_failed.append(source.name)
                failures[source.name] = failure
                result.errors.append(failure)
                self._update_stats(False)
                return
            async with semaphore:
                try:
                    # (IM-2) Budget pre-flight gate: req/rps-class
                    # sources are checked before the request; a denied
                    # source is skipped fail-soft (design §1.4) —
                    # recorded as a budget skip, never fatal.
                    if self.budget_manager is not None:
                        decision = await self.budget_manager.check(source.name)
                        if not decision.allowed:
                            budget_skipped.append(source.name)
                            result.warnings.append(
                                f"budget skip for {source.name}: {decision.reason}"
                            )
                            self._update_stats(False)
                            return
                    try:
                        data = await method(*args, **kwargs)
                    finally:
                        # (IM-2) Post-request metering: consume on
                        # success AND failure (the request was
                        # attempted) — req-class counts the request,
                        # USD-class accumulates the locally-estimated
                        # cost (design §1.4 post-metering).
                        if self.budget_manager is not None:
                            await self.budget_manager.consume(
                                source.name,
                                _request_cost(self.budget_manager, source.name),
                            )
                    self._update_stats(True, items=len(data) if isinstance(data, list) else 1)
                    sources_used.append(source.name)
                    if method_name in {
                        "search_papers",
                        "get_author_papers",
                        "get_citing_papers",
                    }:
                        result.papers.extend(data or [])
                    elif method_name in {
                        "get_paper_by_doi",
                        "get_paper_by_id",
                        "get_paper_by_arxiv_id",
                    }:
                        if data is not None:
                            result.papers.append(data)
                    elif method_name == "get_author_profile":
                        if data is not None:
                            result.authors.append(data)
                    elif method_name == "get_citations":
                        result.citations.extend(data or [])
                except Exception as exc:
                    logger.warning("Source %s failed on %s: %s", source.name, method_name, exc)
                    failure = SourceFailure.from_exception(
                        source=source.name,
                        operation=method_name,
                        exc=exc,
                    )
                    # (IM-2) Feed the error signal into the budget
                    # layer: USD-class sources trip their circuit
                    # breaker on 402/429 (billing/rate-limit signals),
                    # recovering at the next UTC day boundary;
                    # req-class sources just record the signal.
                    if self.budget_manager is not None:
                        await self.budget_manager.report_failure(
                            source.name, http_status=failure.http_status
                        )
                    sources_failed.append(source.name)
                    failures[source.name] = failure
                    result.errors.append(failure)
                    self._update_stats(False)

        await asyncio.gather(*[_call(s) for s in active])

        if not sources_used and sources_failed:
            raise AllSourcesFailedError(
                f"All sources failed for {method_name}",
                query=str(args[0]) if args else "",
                sources_attempted=[s.name for s in active],
                failures=failures,
            )

        # (Q4) Cross-source quality gate: flag suspicious records (polluted
        # DOI prefixes, implausible years) so contamination such as the
        # "Attention Is All You Need" -> DOI 10.65215/2q58a426 top-1 is
        # surfaced in ``CollectionResult.warnings`` instead of being silently
        # trusted.  Scans the raw collected records before the dedup/enrich
        # pipeline merges individual source records away.
        for paper in result.papers:
            prefix = _suspicious_doi(paper.doi)
            if prefix is not None:
                result.warnings.append(
                    f"suspicious DOI prefix {prefix!r} on {paper.title!r} "
                    f"(doi={paper.doi})"
                )
            year_note = _year_anomaly(paper.year)
            if year_note is not None:
                result.warnings.append(
                    f"suspicious record {paper.title!r}: {year_note}"
                )

        # Process pipeline
        if result.papers:
            result.papers = self.enricher.cross_validate_papers(result.papers)
            result.papers = self.deduplicator.deduplicate_papers(result.papers)
            # I-10: field-conflict warnings found during the merge ride along
            # on the result instead of being silently swallowed.
            result.warnings.extend(self.deduplicator.pop_warnings())
            result.papers = self.enricher.enrich_papers(result.papers)
            result.papers = self.validator.filter_valid_papers(result.papers)

        if result.authors:
            # 2026-08-12 决策：作者消歧不做进采集管道（自动阈值合并不可靠——
            # 中文同名场景实测失败）。CLI 只返回原始作者 + evidence，
            # 身份判断交给 agent 方法论（SKILL §11.2）或 paper author 命令
            # （ID 直连 + 候选对比 + 人工 confirm）。
            result.authors = self.enricher.enrich_authors(result.authors)

        confidences = [
            p.primary_evidence.confidence if p.primary_evidence is not None else 0.0
            for p in result.papers
        ]
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        result.stats = {
            **self.get_stats(),
            "sources_used": sources_used,
            "sources_failed": sources_failed,
            "source_failures": [failure.to_dict() for failure in failures.values()],
            "budget_skipped": budget_skipped,
            "budget": [
                {"source": e.source, "kind": e.kind, "detail": e.detail}
                for e in self.budget_manager.pop_events()
            ]
            if self.budget_manager is not None
            else [],
            "source_capabilities": {
                source.name: _source_capabilities(source) for source in active
            },
            "avg_confidence": avg_conf,
            "paper_count": len(result.papers),
            "author_count": len(result.authors),
            "citation_count": len(result.citations),
            "dedup": self.deduplicator.get_stats(),
            # I-13: distinguish "all sources succeeded but nothing matched"
            # (garbage query, unknown DOI) from a failed collection.
            "empty": not (result.papers or result.authors or result.citations),
        }
        return result


class MultiSourceCollector(BaseCollector):
    """Concrete multi-source collector for papers, authors, and citations."""

    async def collect(self, query: str, **kwargs: Any) -> CollectionResult:
        """Default collect: search papers by free-text query."""
        limit = int(kwargs.get("limit", 10))
        # (FIX-M M7) Negative limit rejected up front (matches the storage
        # query ValueError contract); the source adapters would otherwise
        # silently clamp it to 1.
        if limit < 0:
            raise ValueError("limit must be >= 0")
        return await self._run_on_sources("search_papers", query, limit=limit)

    async def collect_author_papers(
        self,
        name: str,
        *,
        sources: Sequence[BaseSource] | None = None,
    ) -> CollectionResult:
        """Collect papers and profile for an author."""
        papers_result = await self._run_on_sources(
            "get_author_papers",
            name,
            sources=sources,
        )
        profile_failed = False
        try:
            profile_result = await self._run_on_sources(
                "get_author_profile",
                name,
                sources=sources,
            )
            papers_result.authors = profile_result.authors
            papers_result.errors.extend(profile_result.errors)
            papers_result.warnings.extend(profile_result.warnings)
            papers_result.stats["author_count"] = len(papers_result.authors)
        except AllSourcesFailedError as exc:
            profile_failed = True
            papers_result.errors.append(str(exc))

        # (FIX-T F3 / T6) Surface the silent keyword-search fallback: several
        # sources (OpenAlex, Semantic Scholar) degrade a no-match author
        # search into a free-text paper search, returning plausibly-related
        # but off-target papers (e.g. a misspelled author name yields papers
        # about nothing to do with the person).  Detection point: papers came
        # back but no author profile was found anywhere and the profile pass
        # did not itself fail — that combination means the papers are almost
        # certainly the keyword-search fallback, not a real author's works.
        if (
            not profile_failed
            and papers_result.papers
            and not papers_result.authors
        ):
            papers_result.warnings.append(
                f"author not found for {name!r}, fell back to keyword search; "
                "results may be unrelated — check the spelling or try a "
                "different source"
            )
        return papers_result

    async def collect_paper(
        self,
        query: str,
        *,
        sources: Sequence[BaseSource] | None = None,
        limit: int = 10,
    ) -> CollectionResult:
        """Collect paper by DOI, arXiv id, OpenAlex work id, or free-text search.

        ``W\\d+`` (or a full ``https://openalex.org/W...`` URL) queries are
        routed to ``get_paper_by_id`` for sources that expose the capability
        so placeholder ids can be backfilled; the remaining sources keep
        their original search path.

        Complete arXiv identifiers are routed only to sources exposing
        ``get_paper_by_arxiv_id``; free text that merely contains an arXiv id
        keeps the normal search path.
        """
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
        work_match = _WORK_ID_RE.match(cleaned)
        if work_match:
            return await self._collect_work_by_id(
                work_match.group(1),
                raw_query=cleaned,
                sources=sources,
                limit=limit,
            )
        arxiv_id = _parse_arxiv_id(cleaned)
        if arxiv_id is not None:
            active = list(sources if sources is not None else self._sources)
            capable = [
                source
                for source in active
                if _source_supports(source, "get_paper_by_arxiv_id")
            ]
            result = await self._run_on_sources(
                "get_paper_by_arxiv_id",
                arxiv_id,
                sources=capable or active,
            )
            if limit == 0:
                result.papers.clear()
                result.stats["paper_count"] = 0
                result.stats["empty"] = True
            return result
        return await self._run_on_sources(
            "search_papers",
            cleaned,
            limit=limit,
            sources=sources,
        )

    async def _collect_work_by_id(
        self,
        work_id: str,
        *,
        raw_query: str,
        sources: Sequence[BaseSource] | None,
        limit: int,
    ) -> CollectionResult:
        """Run a work-id query: by-id on capable sources, search on the rest."""
        active = list(sources if sources is not None else self._sources)
        id_capable = [s for s in active if hasattr(s, "get_paper_by_id")]
        search_only = [s for s in active if not hasattr(s, "get_paper_by_id")]

        results: list[CollectionResult] = []
        if id_capable:
            results.append(
                await self._run_on_sources(
                    "get_paper_by_id",
                    work_id,
                    sources=id_capable,
                )
            )
        if search_only:
            try:
                results.append(
                    await self._run_on_sources(
                        "search_papers",
                        raw_query,
                        limit=limit,
                        sources=search_only,
                    )
                )
            except AllSourcesFailedError as exc:
                # A W-id is not a meaningful search term for sources without a
                # by-id lookup; a failed search must not mask a successful
                # by-id result, so surface it as a recorded error instead.
                results.append(
                    CollectionResult(errors=[f"work-id search fallback: {exc}"])
                )

        if not results:
            raise CollectorError("No data sources registered")
        merged = results[0]
        for result in results[1:]:
            merged = merged.merge(result)
        return merged

    async def collect_citations(
        self,
        paper_id: str,
        *,
        sources: Sequence[BaseSource] | None = None,
    ) -> CollectionResult:
        """Collect citation relationships for a paper id.

        Sources that also expose ``get_citing_papers`` contribute full citing
        Paper records so the citing works can be persisted and placeholder
        nodes are reduced at the source (FIX-B1 F4).  Sources without the
        capability keep the original citations-only behavior.
        """
        result = await self._run_on_sources(
            "get_citations",
            paper_id,
            sources=sources,
        )
        active = list(sources if sources is not None else self._sources)
        capable = [s for s in active if hasattr(s, "get_citing_papers")]
        if not capable:
            return result
        try:
            papers_result = await self._run_on_sources(
                "get_citing_papers",
                paper_id,
                sources=capable,
            )
        except AllSourcesFailedError as exc:
            result.errors.append(str(exc))
            return result
        result.papers.extend(papers_result.papers)
        result.errors.extend(papers_result.errors)
        result.warnings.extend(papers_result.warnings)
        # (Q1) Recompute the aggregate stats on the merged result instead of
        # keeping the first pass's stats — the ``get_citations`` pass runs
        # over an empty paper list, so its ``avg_confidence`` is 0.0 and its
        # ``paper_count`` is 0 (previously paper_count was hand-patched and
        # the citing-pass stats were discarded).  The citing papers are
        # re-deduped as one set so the ``dedup`` stats also reflect the final
        # result.
        if result.papers:
            result.papers = self.deduplicator.deduplicate_papers(result.papers)
            result.warnings.extend(self.deduplicator.pop_warnings())
        confidences = [
            p.primary_evidence.confidence if p.primary_evidence is not None else 0.0
            for p in result.papers
        ]
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        result.stats.update(
            {
                "avg_confidence": avg_conf,
                "paper_count": len(result.papers),
                "dedup": self.deduplicator.get_stats(),
                "empty": not (result.papers or result.authors or result.citations),
            }
        )
        return result
