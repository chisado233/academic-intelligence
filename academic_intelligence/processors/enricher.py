"""Information enrichment processor for academic data.

Fills missing fields via cross-source agreement heuristics and lightweight
local enrichment strategies (no external I/O required by default).
"""

from __future__ import annotations

import abc
import logging
import re
from collections.abc import Sequence
from typing import Any, Protocol

from academic_intelligence.core.constants import DEFAULT_MIN_CONFIDENCE
from academic_intelligence.core.exceptions import EnrichmentError
from academic_intelligence.core.models import Author, AuthorRef, Citation, Paper

logger = logging.getLogger(__name__)

_DOI_IN_TEXT = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


class EnrichmentStrategy(Protocol):
    """Enhancement strategy protocol."""

    def __call__(self, paper: Paper) -> Paper: ...


class BaseEnrichmentStrategy(abc.ABC):
    """Abstract enrichment strategy."""

    def __init__(self, name: str, priority: int = 10) -> None:
        self.name: str = name
        self.priority: int = priority

    @abc.abstractmethod
    def enrich(self, paper: Paper) -> Paper: ...

    def __call__(self, paper: Paper) -> Paper:
        return self.enrich(paper)


class VenueNormalizationStrategy(BaseEnrichmentStrategy):
    """Normalize venue strings (strip excess whitespace / trailing years)."""

    def __init__(self, priority: int = 5) -> None:
        super().__init__(name="venue_normalize", priority=priority)

    def enrich(self, paper: Paper) -> Paper:
        if not paper.venue:
            return paper
        venue = re.sub(r"\s+", " ", paper.venue).strip()
        venue = re.sub(r",\s*(19|20)\d{2}\s*$", "", venue).strip()
        if venue != paper.venue:
            return paper.model_copy(update={"venue": venue})
        return paper


class DoiExtractionStrategy(BaseEnrichmentStrategy):
    """Extract DOI from URL or abstract when doi field is missing."""

    def __init__(self, priority: int = 6) -> None:
        super().__init__(name="doi_extract", priority=priority)

    def enrich(self, paper: Paper) -> Paper:
        if paper.doi:
            return paper
        candidates = " ".join(filter(None, [paper.url, paper.pdf_url, paper.abstract]))
        match = _DOI_IN_TEXT.search(candidates)
        if match:
            return paper.model_copy(update={"doi": match.group(0)})
        return paper


class PdfFromUrlStrategy(BaseEnrichmentStrategy):
    """Promote arXiv abs links / .pdf URLs into pdf_url when missing."""

    def __init__(self, priority: int = 7) -> None:
        super().__init__(name="pdf_from_url", priority=priority)

    def enrich(self, paper: Paper) -> Paper:
        if paper.pdf_url:
            return paper
        url = paper.url or ""
        if url.lower().endswith(".pdf"):
            return paper.model_copy(update={"pdf_url": url})
        if "arxiv.org/abs/" in url:
            pdf = url.replace("/abs/", "/pdf/") + ".pdf"
            return paper.model_copy(update={"pdf_url": pdf})
        return paper


class TitleNormalizeStrategy(BaseEnrichmentStrategy):
    """Collapse excess whitespace in paper titles."""

    def __init__(self, priority: int = 4) -> None:
        super().__init__(name="title_normalize", priority=priority)

    def enrich(self, paper: Paper) -> Paper:
        if not paper.title:
            return paper
        title = re.sub(r"\s+", " ", paper.title).strip()
        if title != paper.title:
            return paper.model_copy(update={"title": title})
        return paper


class AuthorListNormalizeStrategy(BaseEnrichmentStrategy):
    """Normalize author name whitespace and drop exact duplicates."""

    def __init__(self, priority: int = 8) -> None:
        super().__init__(name="author_normalize", priority=priority)

    def enrich(self, paper: Paper) -> Paper:
        if not paper.authors:
            return paper
        cleaned: list[AuthorRef] = []
        seen: set[str] = set()
        for ref in paper.authors:
            if not ref.name or not ref.name.strip():
                continue
            name = re.sub(r"\s+", " ", ref.name).strip()
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(ref.model_copy(update={"name": name, "position": len(cleaned) + 1}))
        if cleaned != paper.authors:
            return paper.model_copy(update={"authors": cleaned})
        return paper


# Keep old class names as aliases for skeleton compatibility
class VenueTierEnrichmentStrategy(VenueNormalizationStrategy):
    """Compatibility alias — venue normalization (tier lookup optional later)."""

    def __init__(self, priority: int = 5) -> None:
        super().__init__(priority=priority)
        self.name = "venue_tier"


class CitationCountEnrichmentStrategy(BaseEnrichmentStrategy):
    """No-op local placeholder; multi-source merge handles citation counts."""

    def __init__(self, priority: int = 9) -> None:
        super().__init__(name="citation_count", priority=priority)

    def enrich(self, paper: Paper) -> Paper:
        return paper


class PdfLinkEnrichmentStrategy(PdfFromUrlStrategy):
    def __init__(self, priority: int = 7) -> None:
        super().__init__(priority=priority)
        self.name = "pdf_link"


class AffiliationEnrichmentStrategy(BaseEnrichmentStrategy):
    """No-op local placeholder for affiliation enrichment."""

    def __init__(self, priority: int = 10) -> None:
        super().__init__(name="affiliation", priority=priority)

    def enrich(self, paper: Paper) -> Paper:
        return paper


class Enricher:
    """Information enrichment processor."""

    def __init__(
        self,
        strategies: Sequence[BaseEnrichmentStrategy] | None = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        self.strategies: list[BaseEnrichmentStrategy] = list(
            strategies or self._default_strategies()
        )
        self.strategies.sort(key=lambda s: s.priority)
        self.min_confidence: float = min_confidence
        self.stats: dict[str, Any] = {
            "papers_processed": 0,
            "papers_enriched": 0,
            "papers_failed": 0,
            "fields_added": 0,
        }

    def register(self, strategy: BaseEnrichmentStrategy) -> None:
        """Register a strategy (replaces same-name if present)."""
        self.strategies = [s for s in self.strategies if s.name != strategy.name]
        self.strategies.append(strategy)
        self.strategies.sort(key=lambda s: s.priority)

    def unregister(self, name: str) -> None:
        self.strategies = [s for s in self.strategies if s.name != name]

    def enrich_papers(self, papers: list[Paper]) -> list[Paper]:
        results: list[Paper] = []
        for paper in papers:
            results.append(self._enrich_single(paper))
        return results

    def enrich_authors(self, authors: list[Author]) -> list[Author]:
        """Normalize author fields (local heuristics)."""
        results: list[Author] = []
        for author in authors:
            name = re.sub(r"\s+", " ", author.name).strip()
            interests = list(dict.fromkeys(author.interests))  # dedupe preserve order
            if name != author.name or interests != author.interests:
                results.append(author.model_copy(update={"name": name, "interests": interests}))
            else:
                results.append(author)
        return results

    def enrich_citations(self, citations: list[Citation]) -> list[Citation]:
        return list(citations)

    def cross_validate_papers(self, papers: list[Paper]) -> list[Paper]:
        """Boost confidence slightly when DOI + year + title all present."""
        out: list[Paper] = []
        for p in papers:
            primary = p.primary_evidence
            if primary is None:
                out.append(p)
                continue
            score = primary.confidence
            if p.doi:
                score = min(1.0, score + 0.05)
            if p.year:
                score = min(1.0, score + 0.02)
            if p.abstract:
                score = min(1.0, score + 0.02)
            if score != primary.confidence:
                ev = primary.model_copy(update={"confidence": score})
                out.append(p.model_copy(update={"evidence": ev}))
            else:
                out.append(p)
        return out

    def _enrich_single(self, paper: Paper) -> Paper:
        self.stats["papers_processed"] += 1
        current: Paper = paper
        fields_before = self._count_filled_fields(current)

        for strategy in self.strategies:
            try:
                current = strategy(current)
            except EnrichmentError as exc:
                logger.warning(
                    "Strategy %s failed for paper %r: %s",
                    strategy.name,
                    paper.title,
                    exc,
                )
                self.stats["papers_failed"] += 1
                continue
            except NotImplementedError:
                continue

        fields_after = self._count_filled_fields(current)
        if fields_after > fields_before:
            self.stats["papers_enriched"] += 1
            self.stats["fields_added"] += fields_after - fields_before

        return current

    @staticmethod
    def _count_filled_fields(paper: Paper) -> int:
        filled = 0
        for key, value in paper.model_dump().items():
            if key in {"id", "evidence", "evidence_list"}:
                continue
            if value is not None and value != [] and value != {}:
                filled += 1
        return filled

    @staticmethod
    def _default_strategies() -> list[BaseEnrichmentStrategy]:
        return [
            TitleNormalizeStrategy(),
            VenueNormalizationStrategy(),
            DoiExtractionStrategy(),
            PdfFromUrlStrategy(),
            AuthorListNormalizeStrategy(),
            CitationCountEnrichmentStrategy(),
            AffiliationEnrichmentStrategy(),
        ]

    def get_stats(self) -> dict[str, Any]:
        return dict(self.stats)

    def reset_stats(self) -> None:
        self.stats = {
            "papers_processed": 0,
            "papers_enriched": 0,
            "papers_failed": 0,
            "fields_added": 0,
        }


def enrich_papers(
    papers: list[Paper],
    strategies: Sequence[BaseEnrichmentStrategy] | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[Paper]:
    enricher = Enricher(strategies=strategies, min_confidence=min_confidence)
    return enricher.enrich_papers(papers)
