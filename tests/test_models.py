"""Tests for core Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from academic_intelligence.core.models import (
    Author,
    Citation,
    CollectionResult,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import SourceType


def _evidence() -> Evidence:
    return Evidence(
        source=SourceType.SEMANTIC_SCHOLAR,
        source_url="https://www.semanticscholar.org/paper/abc",
        confidence=0.9,
    )


def test_paper_valid_doi_normalization() -> None:
    paper = Paper(
        title="Attention Is All You Need",
        authors=["Vaswani"],
        year=2017,
        doi="https://doi.org/10.48550/arXiv.1706.03762",
        evidence=_evidence(),
    )
    assert paper.doi == "10.48550/arXiv.1706.03762"


def test_paper_invalid_year() -> None:
    with pytest.raises(ValidationError):
        Paper(title="x", year=1200, evidence=_evidence())


def test_paper_empty_title() -> None:
    with pytest.raises(ValidationError):
        Paper(title="   ", evidence=_evidence())


def test_citation_self_not_allowed() -> None:
    with pytest.raises(ValidationError):
        Citation(
            citing_paper_id="a",
            cited_paper_id="a",
            evidence=_evidence(),
        )


def test_collection_result_json_roundtrip() -> None:
    paper = Paper(title="Test Paper", authors=["A"], year=2020, evidence=_evidence())
    result = CollectionResult(papers=[paper], stats={"n": 1})
    raw = result.to_json()
    restored = CollectionResult.from_json(raw)
    assert len(restored.papers) == 1
    assert restored.papers[0].title == "Test Paper"
    assert restored.stats["n"] == 1


def test_author_invalid_email() -> None:
    with pytest.raises(ValidationError):
        Author(name="Ada", email="not-an-email", evidence=_evidence())
