"""Boundary tests for Pydantic data models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from academic_intelligence.core.models import Author, Evidence, Paper
from academic_intelligence.core.types import SourceType


pytestmark = [pytest.mark.boundary]


def _evidence(confidence: float = 0.8) -> Evidence:
    return Evidence(
        source=SourceType.ARXIV,
        source_url="https://arxiv.org",
        collected_at=datetime.now(timezone.utc),
        confidence=confidence,
    )


class TestModelBoundary:
    """Data model boundary tests."""

    def test_paper_empty_title(self) -> None:
        """Empty / whitespace-only title is rejected."""
        with pytest.raises(ValidationError):
            Paper(title="", authors=["Test Author"], evidence=_evidence())
        with pytest.raises(ValidationError):
            Paper(title="   ", authors=["Test Author"], evidence=_evidence())

    def test_paper_very_long_title(self) -> None:
        """Very long titles are rejected (I-8 max length 500)."""
        with pytest.raises(ValidationError):
            Paper(title="A" * 501, authors=["Test Author"], evidence=_evidence())
        # a 500-char title is still accepted
        ok = Paper(title="A" * 500, authors=["Test Author"], evidence=_evidence())
        assert len(ok.title) == 500

    def test_paper_invalid_year(self) -> None:
        """Year far in the future is rejected."""
        with pytest.raises(ValidationError):
            Paper(title="Test", authors=["A"], year=3000, evidence=_evidence())

    def test_paper_year_too_old(self) -> None:
        """Year before 1800 is rejected."""
        with pytest.raises(ValidationError):
            Paper(title="Test", authors=["A"], year=1799, evidence=_evidence())

    def test_paper_negative_citations(self) -> None:
        """Negative citation counts are rejected."""
        with pytest.raises(ValidationError):
            Paper(title="Test", authors=["A"], citations=-1, evidence=_evidence())

    def test_paper_invalid_doi(self) -> None:
        """Malformed DOI is rejected."""
        with pytest.raises(ValidationError):
            Paper(
                title="Test",
                authors=["A"],
                doi="not-a-doi",
                evidence=_evidence(),
            )

    def test_paper_invalid_url(self) -> None:
        """Non-HTTP URL is rejected."""
        with pytest.raises(ValidationError):
            Paper(
                title="Test",
                authors=["A"],
                url="ftp://example.com/x",
                evidence=_evidence(),
            )

    def test_author_invalid_email(self) -> None:
        """Invalid email format is rejected."""
        with pytest.raises(ValidationError):
            Author(name="Test", email="invalid_email", evidence=_evidence())

    def test_author_empty_name(self) -> None:
        """Empty author name is rejected."""
        with pytest.raises(ValidationError):
            Author(name="", evidence=_evidence())
        with pytest.raises(ValidationError):
            Author(name="  ", evidence=_evidence())

    def test_author_negative_h_index(self) -> None:
        """Negative h-index is rejected."""
        with pytest.raises(ValidationError):
            Author(name="Test", h_index=-1, evidence=_evidence())

    def test_evidence_invalid_confidence(self) -> None:
        """Confidence outside [0, 1] is rejected."""
        with pytest.raises(ValidationError):
            Evidence(
                source=SourceType.ARXIV,
                source_url="https://arxiv.org",
                collected_at=datetime.now(timezone.utc),
                confidence=1.5,
            )
        with pytest.raises(ValidationError):
            Evidence(
                source=SourceType.ARXIV,
                source_url="https://arxiv.org",
                confidence=-0.1,
            )

    def test_evidence_empty_source_url(self) -> None:
        """Empty source_url is rejected."""
        with pytest.raises(ValidationError):
            Evidence(
                source=SourceType.ARXIV,
                source_url="",
                confidence=0.5,
            )

    def test_paper_boundary_year_current_plus_one(self) -> None:
        """year = current + 1 is allowed; current + 2 is not."""
        current = datetime.now(timezone.utc).year
        ok = Paper(
            title="Near future preprint",
            authors=["A"],
            year=current + 1,
            evidence=_evidence(),
        )
        assert ok.year == current + 1
        with pytest.raises(ValidationError):
            Paper(
                title="Too far",
                authors=["A"],
                year=current + 2,
                evidence=_evidence(),
            )
