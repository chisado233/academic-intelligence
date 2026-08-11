"""Tests for core Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from academic_intelligence.core.models import (
    Author,
    AuthorRef,
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


# ---------------------------------------------------------------------------
# I-8: input size limits
# ---------------------------------------------------------------------------


def test_paper_overlong_title_rejected() -> None:
    with pytest.raises(ValidationError):
        Paper(title="x" * 501, evidence=_evidence())


def test_paper_overlong_abstract_rejected() -> None:
    with pytest.raises(ValidationError):
        Paper(title="T", abstract="a" * 20001, evidence=_evidence())


def test_paper_overlong_venue_rejected() -> None:
    with pytest.raises(ValidationError):
        Paper(title="T", venue="v" * 301, evidence=_evidence())


def test_paper_overlong_keyword_rejected() -> None:
    with pytest.raises(ValidationError):
        Paper(title="T", keywords=["k" * 101], evidence=_evidence())


def test_author_overlong_name_rejected() -> None:
    with pytest.raises(ValidationError):
        Author(name="n" * 201, evidence=_evidence())


def test_author_ref_overlong_name_rejected() -> None:
    with pytest.raises(ValidationError):
        AuthorRef(name="n" * 201)


def test_evidence_overlong_source_url_rejected() -> None:
    with pytest.raises(ValidationError):
        Evidence(source=SourceType.OPENALEX, source_url="https://e.com/" + "u" * 2000)


def test_paper_evidence_list_capped() -> None:
    with pytest.raises(ValidationError):
        Paper(title="T", evidence_list=[_evidence() for _ in range(501)])


def test_author_evidence_list_capped() -> None:
    with pytest.raises(ValidationError):
        Author(name="Ada", evidence_list=[_evidence() for _ in range(501)])
