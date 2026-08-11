"""Confidence scoring for academic data records.

Implements the 3A v2 design (``docs/superpowers/specs/2026-07-26-technical-design-v2.md``
§6.3): a per-source baseline confidence table, a multi-source confirmation
bonus, and field-level adjustments (DOI exact match, PDF link availability,
data staleness).

Usage::

    from academic_intelligence.processors.scorer import ConfidenceScorer

    scorer = ConfidenceScorer()
    composite = scorer.score(paper.evidence_list)      # composite evidence
    paper = scorer.score_paper(paper)                  # recalc + write back
"""

from __future__ import annotations

from datetime import UTC, datetime

from academic_intelligence.core.models import Author, Evidence, Paper
from academic_intelligence.core.types import SourceType

# ---------------------------------------------------------------------------
# Baseline confidence per source (3A v2 §6.3)
# ---------------------------------------------------------------------------
SOURCE_BASELINE_CONFIDENCE: dict[SourceType, float] = {
    SourceType.OPENALEX: 0.90,
    SourceType.SEMANTIC_SCHOLAR: 0.88,
    SourceType.ARXIV: 0.95,
    SourceType.PUBMED: 0.92,
    SourceType.IEEE: 0.85,
    SourceType.GOOGLE_SCHOLAR: 0.75,
    # CORE 0.85 (upgrade technical-design §3): aggregator source.
    SourceType.CORE: 0.85,
    # Unpaywall 0.85 (upgrade technical-design §3): only locates OA links,
    # no metadata authority of its own.
    SourceType.UNPAYWALL: 0.85,
    SourceType.OPEN_CITATIONS: 0.85,  # new: pure citation-graph source (design §3)
    SourceType.CROSSREF: 0.90,  # new: DOI registration authority (design §3)
    # Europe PMC 0.90 (upgrade technical-design §3): official OA archive.
    SourceType.EUROPE_PMC: 0.90,
}
"""Single-source baseline confidence keyed by :class:`SourceType`."""

DEFAULT_BASELINE_CONFIDENCE: float = 0.50
"""Fallback baseline for unknown sources."""

MULTI_SOURCE_BONUS: float = 0.05
"""Confidence added per additional confirming source (capped at 1.0)."""

DOI_EXACT_MATCH_BONUS: float = 0.05
"""Bonus when the record carries an exact DOI."""

PDF_LINK_BONUS: float = 0.03
"""Bonus when the record has a verifiable PDF link."""

STALE_PENALTY: float = 0.10
"""Penalty when no evidence was collected within the staleness window."""

STALE_MAX_AGE_DAYS: int = 730
"""Staleness window: data older than 2 years is penalized."""

MAX_CONFIDENCE: float = 1.0
"""Upper bound for any composite confidence score."""

MIN_CONFIDENCE: float = 0.0


def _source_key(evidence: Evidence) -> str:
    """Stable identity for de-duplicating evidences by source."""
    return evidence.source.value


class ConfidenceScorer:
    """Computes composite confidence scores for records and evidence lists.

    Attributes:
        baseline: Mapping ``SourceType -> float`` used as the single-source
            baseline (defaults to :data:`SOURCE_BASELINE_CONFIDENCE`).
    """

    def __init__(
        self,
        baseline: dict[SourceType, float] | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        """Initialize the scorer.

        Args:
            baseline: Optional override of the source baseline table.
            now: Injectable "current time" for deterministic staleness tests.
        """
        self.baseline: dict[SourceType, float] = dict(
            baseline or SOURCE_BASELINE_CONFIDENCE
        )
        self._now: datetime | None = now

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _current_time(self) -> datetime:
        return self._now or datetime.now(UTC)

    def _unique_by_source(self, evidence_list: list[Evidence]) -> list[Evidence]:
        """Return one evidence per source (highest confidence wins)."""
        best: dict[str, Evidence] = {}
        for ev in evidence_list:
            key = _source_key(ev)
            if key not in best or ev.confidence > best[key].confidence:
                best[key] = ev
        return list(best.values())

    def baseline_for(self, source: SourceType) -> float:
        """Return the baseline confidence for a single source."""
        return self.baseline.get(source, DEFAULT_BASELINE_CONFIDENCE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(self, evidence_list: list[Evidence]) -> Evidence:
        """Generate a single composite evidence from an evidence list.

        The composite confidence is::

            min(1.0, max_source_baseline + 0.05 * (n_sources - 1))

        where ``n_sources`` is the number of distinct sources in the list.

        Args:
            evidence_list: Evidence entries confirming the same record.

        Returns:
            A new :class:`Evidence` carrying the composite score. Its
            ``raw_data`` keeps the per-source provenance (``individual``,
            ``merged_from``, ``base_confidence``, ``multi_source_bonus``).

        Raises:
            ValueError: If *evidence_list* is empty.
        """
        if not evidence_list:
            raise ValueError("cannot score an empty evidence list")
        unique = self._unique_by_source(evidence_list)
        base = max(self.baseline_for(e.source) for e in unique)
        n_sources = len(unique)
        conf = min(
            MAX_CONFIDENCE,
            base + MULTI_SOURCE_BONUS * (n_sources - 1),
        )
        primary = max(unique, key=lambda e: e.confidence)
        raw: dict[str, object] = {
            "merged_from": sorted({e.source.value for e in unique}),
            "source_urls": [e.source_url for e in unique],
            "individual": [e.model_dump(mode="json") for e in unique],
            "base_confidence": base,
            "multi_source_bonus": MULTI_SOURCE_BONUS * (n_sources - 1),
        }
        return Evidence(
            source=primary.source,
            source_id=primary.source_id,
            source_url=primary.source_url,
            collected_at=max(e.collected_at for e in unique),
            confidence=conf,
            raw_data=raw,
        )

    def _field_adjustments(
        self,
        conf: float,
        *,
        doi: str | None,
        pdf_url: str | None,
        newest_collected_at: datetime,
    ) -> float:
        """Apply DOI / PDF / staleness adjustments to a composite score."""
        if doi:
            conf = min(MAX_CONFIDENCE, conf + DOI_EXACT_MATCH_BONUS)
        if pdf_url:
            conf = min(MAX_CONFIDENCE, conf + PDF_LINK_BONUS)
        collected = newest_collected_at.replace(
            tzinfo=newest_collected_at.tzinfo or UTC
        )
        age = self._current_time() - collected
        if age.days > STALE_MAX_AGE_DAYS:
            conf = max(MIN_CONFIDENCE, conf - STALE_PENALTY)
        return max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, conf))

    def score_paper(self, paper: Paper) -> Paper:
        """Recompute the composite confidence of a paper and write it back.

        Starts from :meth:`score` (multi-source baseline) and applies the
        field-level adjustments from 3A v2 §6.3:

        - DOI exact match ``+0.05``
        - PDF link present ``+0.03``
        - no evidence collected in the last 2 years ``-0.10``

        The composite evidence is written to the deprecated ``evidence``
        alias (and thus exposed through ``paper.primary_evidence``); the
        per-source ``evidence_list`` entries are left untouched.

        Args:
            paper: The paper to re-score.

        Returns:
            A copy of *paper* with the composite confidence updated. Papers
            with an empty ``evidence_list`` are returned unchanged.
        """
        if not paper.evidence_list:
            return paper
        composite = self.score(paper.evidence_list)
        newest = max(e.collected_at for e in paper.evidence_list)
        conf = self._field_adjustments(
            composite.confidence,
            doi=paper.doi,
            pdf_url=paper.pdf_url,
            newest_collected_at=newest,
        )
        composite = composite.model_copy(update={"confidence": conf})
        # I-6: persist the composite alongside the per-source evidence list so
        # it survives to_dict()/from_dict() and the JSON backend. model_copy
        # bypasses validators, so the mirror never overwrites it.
        return paper.model_copy(
            update={"evidence": composite, "synthetic_confidence": conf}
        )

    def score_author(self, author: Author) -> Author:
        """Recompute the composite confidence of an author and write it back.

        Applies the multi-source baseline only (authors do not have DOI /
        PDF fields); staleness is still penalized.

        Args:
            author: The author to re-score.

        Returns:
            A copy of *author* with the composite confidence updated.
        """
        if not author.evidence_list:
            return author
        composite = self.score(author.evidence_list)
        newest = max(e.collected_at for e in author.evidence_list)
        conf = self._field_adjustments(
            composite.confidence,
            doi=None,
            pdf_url=None,
            newest_collected_at=newest,
        )
        composite = composite.model_copy(update={"confidence": conf})
        # I-6: persist the composite alongside the per-source evidence list so
        # it survives to_dict()/from_dict() and the JSON backend.
        return author.model_copy(
            update={"evidence": composite, "synthetic_confidence": conf}
        )


__all__ = [
    "ConfidenceScorer",
    "SOURCE_BASELINE_CONFIDENCE",
    "DEFAULT_BASELINE_CONFIDENCE",
    "MULTI_SOURCE_BONUS",
    "DOI_EXACT_MATCH_BONUS",
    "PDF_LINK_BONUS",
    "STALE_PENALTY",
    "STALE_MAX_AGE_DAYS",
    "MAX_CONFIDENCE",
]
