"""Tests for processors."""

from __future__ import annotations

from academic_intelligence.core.models import Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.processors.enricher import Enricher
from academic_intelligence.processors.validator import Validator


def _ev(source: SourceType = SourceType.SEMANTIC_SCHOLAR, conf: float = 0.8) -> Evidence:
    return Evidence(
        source=source,
        source_url="https://example.com",
        confidence=conf,
    )


def test_deduplicate_by_doi() -> None:
    p1 = Paper(
        title="Attention Is All You Need",
        authors=["Vaswani"],
        year=2017,
        doi="10.5555/3295222.3295349",
        evidence=_ev(SourceType.SEMANTIC_SCHOLAR, 0.9),
    )
    p2 = Paper(
        title="Attention is all you need",
        authors=["A. Vaswani", "N. Shazeer"],
        year=2017,
        doi="10.5555/3295222.3295349",
        abstract="Abstract text",
        evidence=_ev(SourceType.OPENALEX, 0.85),
    )
    merged = Deduplicator().deduplicate_papers([p1, p2])
    assert len(merged) == 1
    assert merged[0].abstract == "Abstract text"
    assert len(merged[0].authors) >= 1


def test_enricher_normalizes() -> None:
    paper = Paper(
        title="  Hello   World  ",
        authors=[" Ada  Lovelace ", "Ada Lovelace"],
        evidence=_ev(),
    )
    out = Enricher().enrich_papers([paper])[0]
    assert out.title == "Hello World"
    assert out.authors == ["Ada Lovelace"]


def test_validator_paper() -> None:
    paper = Paper(
        title="Valid",
        authors=["A"],
        year=2020,
        doi="10.1234/abc.def",
        url="https://example.com/p",
        evidence=_ev(),
    )
    result = Validator().validate_paper(paper)
    assert result.is_valid
    assert result.confidence_score > 0
