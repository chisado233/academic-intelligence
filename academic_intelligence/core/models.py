"""Academic Intelligence - Core Data Models

This module defines the Pydantic-based data models used throughout the
Academic Intelligence system. All models include evidence tracking for
provenance and confidence scoring.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from academic_intelligence.core.types import SourceType

# DOI pattern: 10.<registrant>/<suffix>
_DOI_RE = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _is_valid_url(value: str) -> bool:
    """Return True if *value* looks like an absolute HTTP(S) URL."""
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


class Evidence(BaseModel):
    """Evidence chain: source provenance for every data point.

    Attributes:
        source: The data source that provided this information.
        source_url: Original URL from which the data was collected.
        collected_at: Timestamp of data collection.
        confidence: Confidence score between 0.0 and 1.0.
        raw_data: Optional raw response data for auditability.
    """

    source: SourceType
    source_url: str
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)
    raw_data: Optional[Dict[str, Any]] = None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("source_url must not be empty")
        return v.strip()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Evidence:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class Author(BaseModel):
    """Scholar/author model with evidence tracking.

    Attributes:
        id: Unique identifier (optional, assigned by storage).
        name: Full name of the author.
        affiliation: Current institutional affiliation.
        email: Contact email address.
        homepage: Personal or institutional homepage URL.
        h_index: H-index metric.
        citations: Total citation count.
        interests: List of research interests/topics.
        profile_url: URL to the author's profile page.
        evidence: Source evidence and provenance.
    """

    id: Optional[str] = None
    name: str
    affiliation: Optional[str] = None
    email: Optional[str] = None
    homepage: Optional[str] = None
    h_index: Optional[int] = Field(default=None, ge=0)
    citations: Optional[int] = Field(default=None, ge=0)
    interests: List[str] = Field(default_factory=list)
    profile_url: Optional[str] = None
    evidence: Evidence

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        name = v.strip()
        if not name:
            raise ValueError("author name must not be empty")
        return name

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not _EMAIL_RE.match(v):
            raise ValueError(f"invalid email format: {v}")
        return v

    @field_validator("homepage", "profile_url")
    @classmethod
    def validate_optional_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not _is_valid_url(v):
            raise ValueError(f"invalid URL: {v}")
        return v

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Author:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class Paper(BaseModel):
    """Academic paper model with evidence tracking.

    Attributes:
        id: Unique identifier (optional, assigned by storage).
        title: Paper title.
        authors: List of author names.
        year: Publication year.
        venue: Journal or conference name.
        abstract: Paper abstract text.
        doi: Digital Object Identifier.
        url: Paper URL.
        pdf_url: Direct PDF link.
        citations: Citation count.
        keywords: List of keywords or tags.
        evidence: Source evidence and provenance.
    """

    id: Optional[str] = None
    title: str
    authors: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    venue: Optional[str] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    citations: Optional[int] = Field(default=None, ge=0)
    keywords: List[str] = Field(default_factory=list)
    evidence: Evidence

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        title = v.strip()
        if not title:
            raise ValueError("paper title must not be empty")
        return title

    @field_validator("year")
    @classmethod
    def validate_year(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        current = datetime.now(timezone.utc).year
        if v < 1800 or v > current + 1:
            raise ValueError(f"year must be between 1800 and {current + 1}, got {v}")
        return v

    @field_validator("doi")
    @classmethod
    def validate_doi(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        # Normalize: strip common prefixes
        cleaned = v.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix) :]
                break
        cleaned = cleaned.strip()
        if not _DOI_RE.match(cleaned):
            raise ValueError(f"invalid DOI format: {v}")
        return cleaned

    @field_validator("url", "pdf_url")
    @classmethod
    def validate_optional_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not _is_valid_url(v):
            raise ValueError(f"invalid URL: {v}")
        return v

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Paper:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class Citation(BaseModel):
    """Citation relationship model.

    Attributes:
        citing_paper_id: ID of the paper that cites.
        cited_paper_id: ID of the paper being cited.
        evidence: Source evidence and provenance.
    """

    citing_paper_id: str
    cited_paper_id: str
    evidence: Evidence

    @model_validator(mode="after")
    def validate_ids(self) -> Citation:
        if not self.citing_paper_id or not self.citing_paper_id.strip():
            raise ValueError("citing_paper_id must not be empty")
        if not self.cited_paper_id or not self.cited_paper_id.strip():
            raise ValueError("cited_paper_id must not be empty")
        if self.citing_paper_id == self.cited_paper_id:
            raise ValueError("self-citation is not allowed (citing == cited)")
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Citation:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class CollectionResult(BaseModel):
    """Container for collection operation results.

    Attributes:
        authors: List of collected authors.
        papers: List of collected papers.
        citations: List of collected citation relationships.
        errors: List of error messages encountered during collection.
        stats: Dictionary of collection statistics.
    """

    authors: List[Author] = Field(default_factory=list)
    papers: List[Paper] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    stats: Dict[str, Any] = Field(default_factory=dict)

    def merge(self, other: CollectionResult) -> CollectionResult:
        """Merge another result into this one (returns new instance)."""
        return CollectionResult(
            authors=self.authors + other.authors,
            papers=self.papers + other.papers,
            citations=self.citations + other.citations,
            errors=self.errors + other.errors,
            stats={**self.stats, **other.stats},
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CollectionResult:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to a JSON string."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, raw: str) -> CollectionResult:
        """Deserialize from a JSON string."""
        return cls.model_validate_json(raw)
