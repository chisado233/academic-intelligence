"""Academic Intelligence — Data Validator

Quality-assurance layer: completeness, format, and confidence scoring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from academic_intelligence.core.exceptions import DataValidationError
from academic_intelligence.core.models import Author, Citation, Evidence, Paper


@dataclass
class ValidatorConfig:
    """Runtime configuration for the validation engine."""

    min_confidence: float = 0.5
    require_doi: bool = False
    strict_email: bool = True
    max_title_length: int = 500
    allowed_sources: set[str] = field(default_factory=set)


@dataclass
class ValidationResult:
    """Outcome of validating a single record."""

    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence_score: float = 0.0
    field_issues: dict[str, list[str]] = field(default_factory=dict)

    def add_error(self, field_name: str, message: str) -> None:
        self.is_valid = False
        self.errors.append(f"[{field_name}] {message}")
        self.field_issues.setdefault(field_name, []).append(message)

    def add_warning(self, field_name: str, message: str) -> None:
        self.warnings.append(f"[{field_name}] {message}")
        self.field_issues.setdefault(field_name, []).append(message)


class ValidatorProtocol(Protocol):
    def validate_paper(self, paper: Paper) -> ValidationResult: ...

    def validate_author(self, author: Author) -> ValidationResult: ...

    def validate_citation(self, citation: Citation) -> ValidationResult: ...


class Validator:
    """Default validation engine for Academic Intelligence records."""

    def __init__(self, config: ValidatorConfig | None = None) -> None:
        self.config: ValidatorConfig = config or ValidatorConfig()
        self._doi_pattern: re.Pattern[str] = re.compile(
            r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$",
            re.IGNORECASE,
        )
        self._email_pattern: re.Pattern[str] = re.compile(
            r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
        )

    def validate_paper(self, paper: Paper) -> ValidationResult:
        result = ValidationResult()

        if not paper.title or not paper.title.strip():
            result.add_error("title", "title is required")
        elif len(paper.title) > self.config.max_title_length:
            result.add_warning(
                "title",
                f"title exceeds {self.config.max_title_length} characters",
            )

        if not paper.authors:
            result.add_warning("authors", "no authors listed")

        if paper.year is not None:
            current = datetime.now(UTC).year
            if paper.year < 1800 or paper.year > current + 1:
                result.add_error("year", f"implausible year: {paper.year}")

        doi_err = self._check_doi(paper.doi)
        if doi_err:
            result.add_error("doi", doi_err)
        elif self.config.require_doi and not paper.doi:
            result.add_error("doi", "DOI is required by configuration")

        for field_name in ("url", "pdf_url"):
            url_val = getattr(paper, field_name)
            url_err = self._check_url(url_val)
            if url_err:
                result.add_warning(field_name, url_err)

        if paper.citations is not None and paper.citations < 0:
            result.add_error("citations", "citations must be non-negative")

        evidences = self._evidence_entries(paper)
        if not evidences:
            result.add_error("evidence", "evidence is required")
        for ev in evidences:
            self._validate_evidence(ev, result)
        primary = paper.primary_evidence
        result.confidence_score = self._score_record(
            primary,
            completeness=self._paper_completeness(paper),
        )
        if result.confidence_score < self.config.min_confidence:
            result.add_warning(
                "confidence",
                f"score {result.confidence_score:.2f} below min {self.config.min_confidence}",
            )
        return result

    def validate_author(self, author: Author) -> ValidationResult:
        result = ValidationResult()

        if not author.name or not author.name.strip():
            result.add_error("name", "name is required")

        if (
            author.email
            and self.config.strict_email
            and not self._email_pattern.match(author.email)
        ):
            result.add_error("email", f"invalid email: {author.email}")

        for field_name in ("homepage", "profile_url"):
            url_err = self._check_url(getattr(author, field_name))
            if url_err:
                result.add_warning(field_name, url_err)

        if author.h_index is not None and author.h_index < 0:
            result.add_error("h_index", "h_index must be non-negative")
        if author.citations is not None and author.citations < 0:
            result.add_error("citations", "citations must be non-negative")

        evidences = self._evidence_entries(author)
        if not evidences:
            result.add_error("evidence", "evidence is required")
        for ev in evidences:
            self._validate_evidence(ev, result)
        result.confidence_score = self._score_evidence(author.primary_evidence)
        return result

    def validate_citation(self, citation: Citation) -> ValidationResult:
        result = ValidationResult()
        if not citation.citing_paper_id:
            result.add_error("citing_paper_id", "required")
        if not citation.cited_paper_id:
            result.add_error("cited_paper_id", "required")
        if (
            citation.citing_paper_id
            and citation.cited_paper_id
            and citation.citing_paper_id == citation.cited_paper_id
        ):
            result.add_error("citation", "self-citation not allowed")

        self._validate_evidence(citation.evidence, result)
        result.confidence_score = self._score_evidence(citation.evidence)
        return result

    def validate_papers(
        self,
        papers: list[Paper],
        *,
        raise_on_invalid: bool = False,
    ) -> list[ValidationResult]:
        results = [self.validate_paper(p) for p in papers]
        if raise_on_invalid:
            for p, r in zip(papers, results, strict=True):
                if not r.is_valid:
                    raise DataValidationError(
                        f"Invalid paper: {p.title}",
                        record_id=p.id,
                        details={"errors": r.errors},
                    )
        return results

    def filter_valid_papers(self, papers: list[Paper]) -> list[Paper]:
        return [p for p in papers if self.validate_paper(p).is_valid]

    def _validate_evidence(self, evidence: Evidence, result: ValidationResult) -> None:
        if not evidence.source_url:
            result.add_error("evidence.source_url", "required")
        if evidence.confidence < 0 or evidence.confidence > 1:
            result.add_error("evidence.confidence", "must be in [0, 1]")
        if (
            self.config.allowed_sources
            and evidence.source.value not in self.config.allowed_sources
        ):
            result.add_error(
                "evidence.source",
                f"source {evidence.source.value} not in allowed set",
            )

    @staticmethod
    def _evidence_entries(record: Any) -> list[Evidence]:
        """Return the evidence entries of a record (evidence_list first)."""
        if getattr(record, "evidence_list", None):
            return list(record.evidence_list)
        legacy = getattr(record, "evidence", None)
        return [legacy] if legacy is not None else []

    def _score_evidence(self, evidence: Evidence | None) -> float:
        if evidence is None:
            return 0.0
        score = evidence.confidence
        if evidence.raw_data:
            score = min(1.0, score + 0.02)
        # Mild recency boost if collected within 30 days
        try:
            age = datetime.now(UTC) - evidence.collected_at.replace(
                tzinfo=evidence.collected_at.tzinfo or UTC
            )
            if age.days <= 30:
                score = min(1.0, score + 0.01)
        except Exception:
            pass
        return max(0.0, min(1.0, score))

    def _score_record(self, evidence: Evidence | None, completeness: float) -> float:
        base = self._score_evidence(evidence)
        return max(0.0, min(1.0, 0.7 * base + 0.3 * completeness))

    @staticmethod
    def _paper_completeness(paper: Paper) -> float:
        fields = [
            paper.title,
            paper.authors,
            paper.year,
            paper.venue,
            paper.abstract,
            paper.doi,
            paper.url,
            paper.citations,
        ]
        filled = sum(1 for f in fields if f is not None and f != [] and f != "")
        return filled / len(fields)

    def _check_url(self, url: str | None) -> str | None:
        if url is None or url == "":
            return None
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return f"invalid URL: {url}"
        except Exception:
            return f"invalid URL: {url}"
        return None

    def _check_doi(self, doi: str | None) -> str | None:
        if doi is None or doi == "":
            return None
        if not self._doi_pattern.match(doi):
            return f"invalid DOI format: {doi}"
        return None
