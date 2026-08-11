"""Academic Intelligence - Core Data Models

This module defines the Pydantic-based data models used throughout the
Academic Intelligence system. All models include evidence tracking for
provenance and confidence scoring.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from academic_intelligence.core.exceptions import SourceFailure
from academic_intelligence.core.types import SourceType
from academic_intelligence.utils.normalize import normalize_nfc

# DOI pattern: 10.<registrant>/<suffix>
_DOI_RE = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
# ORCID iD: 4-4-4-4 digits, final char may be a check digit X
_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[0-9X]$", re.IGNORECASE)
# PMID (NCBI): 1-8 pure digits.
_PMID_RE = re.compile(r"^\d{1,8}$")


def _orcid_check_digit(value: str) -> str:
    """Compute the ISO/IEC 7064 MOD 11-2 check digit for an ORCID base.

    *value* must be the 15-digit base (hyphens removed, check digit
    excluded).  Returns ``"X"`` when the checksum works out to 10 (FIX-S S1).
    """
    total = 0
    for d in value[:15]:
        total = (total + int(d)) * 2
    check = (12 - total % 11) % 11
    return "X" if check == 10 else str(check)


def _is_valid_url(value: str) -> bool:
    """Return True if *value* looks like an absolute HTTP(S) URL."""
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def normalize_doi(value: str | None) -> str | None:
    """Normalize a DOI value, or return ``None`` when it is invalid.

    Strips the common ``https://doi.org/`` / ``http://doi.org/`` / ``doi:``
    prefixes and applies the ``10.<registrant>/<suffix>`` format check.  The
    :meth:`Paper.validate_doi` validator delegates here so the model and the
    source parsers (FIX-AB-3) share one definition — parsers validate DOIs
    with this lightweight helper instead of constructing a whole ``Paper``
    just to run its validator (the full ``model_validate`` dominated the
    pubmed/arxiv parse hot path).
    """
    if value is None or value == "":
        return None
    cleaned = value.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.strip()
    if not _DOI_RE.match(cleaned):
        return None
    return cleaned


def normalize_pmid(value: str | None) -> str | None:
    """Normalize a PMID value, or return ``None`` when it is invalid.

    NCBI PMIDs are 1-8 pure digits (FIX-S S2).  Like :func:`normalize_doi`,
    this is the shared field-level definition used by both the ``Paper``
    validator and the parsers (FIX-AB-3).
    """
    if value is None or value == "":
        return None
    cleaned = str(value).strip()
    if not _PMID_RE.match(cleaned):
        return None
    return cleaned


class Evidence(BaseModel):
    """Evidence chain: source provenance for every data point.

    Attributes:
        source: The data source that provided this information.
        source_id: The raw ID of the record within that source (arXiv ID,
            DOI, PMID, etc.). Optional.
        source_url: Original URL from which the data was collected.
        collected_at: Timestamp of data collection.
        confidence: Confidence score between 0.0 and 1.0.
        raw_data: Optional raw response data for auditability.
    """

    source: SourceType
    source_id: str | None = None
    source_url: str = Field(max_length=2000)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)
    raw_data: dict[str, Any] | None = None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("source_url must not be empty")
        return v.strip()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Evidence:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class AuthorRef(BaseModel):
    """Lightweight author reference inside a paper (points to an Author).

    Preserves authorship order and correspondence status without embedding
    the full author profile.

    Attributes:
        author_id: Resolved ``Author.id`` when the author has been
            disambiguated; ``None`` otherwise.
        name: The name exactly as it appears on the paper.
        position: Author position in the byline (1-based).
        is_corresponding: Whether this author is marked as corresponding.
        affiliation: Affiliation as printed on the paper, if available.
    """

    author_id: str | None = None
    name: str = Field(max_length=200)
    position: int = Field(default=1, ge=1)
    is_corresponding: bool = False
    affiliation: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        name = normalize_nfc(v.strip())
        if not name:
            raise ValueError("author reference name must not be empty")
        return name

    @field_validator("affiliation")
    @classmethod
    def normalize_affiliation(cls, v: str | None) -> str | None:
        # (FIX-W W3) NFC on the model write/read path keeps decomposed and
        # precomposed affiliation spellings interoperable.
        return normalize_nfc(v) if v else v

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthorRef:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class Author(BaseModel):
    """Scholar/author model with evidence tracking.

    Attributes:
        id: Unique identifier (optional, assigned by storage).
        name: Full name of the author.
        orcid: ORCID iD (``0000-0001-2345-6789`` format).
        semantic_scholar_id: Semantic Scholar author ID.
        openalex_id: OpenAlex author ID.
        aliases: Alternative names / name variants used for disambiguation.
        disambiguation_status: ``"auto"`` / ``"confirmed"`` / ``"ambiguous"``.
        coauthors: Co-author names collected from this author's bylines
            (disambiguation feature ``coauthor_overlap``).
        venues: Venue names of this author's papers (disambiguation feature
            ``venue_overlap``).
        active_years: Publication years of this author's papers (disambiguation
            feature ``year_range_overlap``).
        affiliation: Current institutional affiliation.
        email: Contact email address.
        homepage: Personal or institutional homepage URL.
        h_index: H-index metric.
        citations: Total citation count.
        interests: List of research interests/topics.
        profile_url: URL to the author's profile page.
        evidence_list: Source evidence and provenance for every data point.
            Each source that confirmed this author contributes one entry.
        evidence: Deprecated single-evidence alias (read ``primary_evidence``
            or ``evidence_list`` instead). Kept for backwards compatibility:
            when constructed with ``evidence=...`` it is folded into
            ``evidence_list`` automatically.
    """

    id: str | None = None
    name: str = Field(max_length=200)
    orcid: str | None = None
    semantic_scholar_id: str | None = None
    openalex_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    disambiguation_status: str = "auto"
    coauthors: list[str] = Field(default_factory=list)
    venues: list[str] = Field(default_factory=list)
    active_years: list[int] | None = None
    affiliation: str | None = None
    email: str | None = None
    homepage: str | None = None
    h_index: int | None = Field(default=None, ge=0)
    citations: int | None = Field(default=None, ge=0)
    interests: list[str] = Field(default_factory=list)
    profile_url: str | None = None
    evidence_list: list[Evidence] = Field(default_factory=list, max_length=500)
    evidence: Evidence | None = Field(default=None, exclude=True)
    synthetic_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        name = normalize_nfc(v.strip())
        if not name:
            raise ValueError("author name must not be empty")
        return name

    @field_validator("affiliation")
    @classmethod
    def normalize_affiliation(cls, v: str | None) -> str | None:
        # (FIX-W W3) NFC on the model write/read path keeps decomposed and
        # precomposed affiliation spellings interoperable.
        return normalize_nfc(v) if v else v

    @field_validator("interests")
    @classmethod
    def normalize_interests(cls, values: list[str]) -> list[str]:
        """Store research interests in the same canonical form as queries."""
        return [normalize_nfc(value) for value in values]

    @field_validator("orcid", mode="before")
    @classmethod
    def validate_orcid(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        value = str(v).strip()
        for prefix in ("https://orcid.org/", "http://orcid.org/", "orcid:"):
            if value.lower().startswith(prefix):
                value = value[len(prefix) :]
                break
        value = value.strip()
        if not _ORCID_RE.match(value):
            raise ValueError(f"invalid ORCID format: {v}")
        # ISO/IEC 7064 MOD 11-2 checksum (FIX-S S1): a well-formed ORCID whose
        # check digit does not match the first 15 digits is rejected instead
        # of being stored as-is.
        expected = _orcid_check_digit(value.replace("-", ""))
        if value[-1].upper() != expected:
            raise ValueError(f"invalid ORCID checksum: {v}")
        return value.upper()

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _EMAIL_RE.match(v):
            raise ValueError(f"invalid email format: {v}")
        return v

    @field_validator("homepage", "profile_url")
    @classmethod
    def validate_optional_url(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _is_valid_url(v):
            raise ValueError(f"invalid URL: {v}")
        return v

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_evidence(cls, data: Any) -> Any:
        """Fold the deprecated single ``evidence`` into ``evidence_list``."""
        if isinstance(data, dict):
            legacy = data.get("evidence")
            if legacy is not None and not data.get("evidence_list"):
                data["evidence_list"] = [legacy]
        return data

    @model_validator(mode="after")
    def _sync_evidence_compat(self) -> Author:
        # ``evidence`` mirrors the highest-confidence entry of evidence_list,
        # lifted to the persisted synthetic composite when present (I-6).
        # Always mirror (not only when unset): a stale legacy ``evidence``
        # value passed alongside ``evidence_list`` must never shadow the list
        # maximum (M1). ``score_paper``/``score_author`` write the composite
        # here via ``model_copy``, which bypasses validators, so a computed
        # composite is never overwritten by the mirror.
        if self.evidence_list:
            if self.synthetic_confidence is not None:
                primary = max(self.evidence_list, key=lambda e: e.confidence)
                self.evidence = primary.model_copy(
                    update={"confidence": self.synthetic_confidence}
                )
            else:
                self.evidence = max(
                    self.evidence_list, key=lambda e: e.confidence
                )
        return self

    @property
    def primary_evidence(self) -> Evidence | None:
        """Highest-confidence evidence for this author (or ``None``).

        Returns the composite written by :meth:`ConfidenceScorer.score_author`
        when present, otherwise the highest-confidence ``evidence_list`` entry
        (the ``_sync_evidence_compat`` mirror guarantees the deprecated
        ``evidence`` alias never holds a lower value than the list max).
        """
        if self.evidence is not None:
            return self.evidence
        if self.evidence_list:
            return max(self.evidence_list, key=lambda e: e.confidence)
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Author:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class Paper(BaseModel):
    """Academic paper model with evidence tracking.

    Attributes:
        id: Unique identifier (optional, assigned by storage).
        title: Paper title.
        authors: List of :class:`AuthorRef` entries (strings are coerced to
            ``AuthorRef(name=...)`` for backwards compatibility).
        year: Publication year.
        venue: Journal or conference name.
        venue_type: ``"journal"`` / ``"conference"`` / ``"preprint"``.
        publisher: Publishing entity (e.g. "Springer Nature"); taken from the
            source's own metadata or derived from the DOI prefix map (§3.1).
        abstract: Paper abstract text.
        doi: Digital Object Identifier.
        arxiv_id: arXiv ID if the paper is on arXiv.
        pmid: PubMed ID if the paper is indexed by PubMed.
        url: Paper URL.
        pdf_url: Direct PDF link.
        citations: Citation count.
        reference_count: Number of references.
        keywords: List of keywords or tags.
        fields_of_study: List of research fields (e.g. from Semantic Scholar).
        references: IDs of papers cited by this paper (graph relation).
        citations_list: IDs of papers citing this paper (graph relation).
        evidence_list: Source evidence and provenance for every data point.
            Each source that confirmed this paper contributes one entry.
        evidence: Deprecated single-evidence alias (read ``primary_evidence``
            or ``evidence_list`` instead). Kept for backwards compatibility:
            when constructed with ``evidence=...`` it is folded into
            ``evidence_list`` automatically.
    """

    id: str | None = None
    title: str = Field(max_length=500)
    authors: list[AuthorRef] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = Field(default=None, max_length=300)
    venue_type: str | None = None
    publisher: str | None = Field(default=None, max_length=300)
    abstract: str | None = Field(default=None, max_length=20000)
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    citations: int | None = Field(default=None, ge=0)
    reference_count: int | None = Field(default=None, ge=0)
    keywords: list[Annotated[str, StringConstraints(max_length=100)]] = Field(
        default_factory=list
    )
    fields_of_study: list[Annotated[str, StringConstraints(max_length=100)]] = Field(
        default_factory=list
    )
    references: list[str] | None = None
    citations_list: list[str] | None = None
    evidence_list: list[Evidence] = Field(default_factory=list, max_length=500)
    evidence: Evidence | None = Field(default=None, exclude=True)
    synthetic_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        title = normalize_nfc(v.strip())
        if not title:
            raise ValueError("paper title must not be empty")
        return title

    @field_validator("venue")
    @classmethod
    def normalize_venue(cls, v: str | None) -> str | None:
        # (FIX-W W3) NFC on the model write/read path keeps decomposed and
        # precomposed venue spellings interoperable for venue queries.
        return normalize_nfc(v) if v else v

    @field_validator("publisher")
    @classmethod
    def normalize_publisher(cls, v: str | None) -> str | None:
        # (WP2a) NFC on the write path keeps decomposed and precomposed
        # publisher spellings interoperable (same contract as venue).
        return normalize_nfc(v) if v else v

    @field_validator("abstract")
    @classmethod
    def normalize_abstract(cls, v: str | None) -> str | None:
        # (FIX-W W3) Keyword search covers the abstract too, so it follows the
        # same NFC contract as the title.
        return normalize_nfc(v) if v else v

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, v: list[str]) -> list[str]:
        # (FIX-W W3) NFC per keyword so decomposed spellings collide with
        # precomposed ones.
        return [normalize_nfc(k) for k in v]

    @field_validator("authors", mode="before")
    @classmethod
    def coerce_authors(cls, v: Any) -> list[AuthorRef]:
        """Accept plain names (strings) as well as AuthorRef / dict entries."""
        if v is None:
            return []
        result: list[AuthorRef] = []
        for i, entry in enumerate(v, start=1):
            if isinstance(entry, AuthorRef):
                result.append(entry)
            elif isinstance(entry, str):
                result.append(AuthorRef(name=entry, position=i))
            elif isinstance(entry, dict):
                result.append(AuthorRef.model_validate(entry))
            else:
                raise ValueError(f"invalid author entry: {entry!r}")
        return result

    @field_validator("year")
    @classmethod
    def validate_year(cls, v: int | None) -> int | None:
        if v is None:
            return None
        current = datetime.now(UTC).year
        if v < 1800 or v > current + 1:
            raise ValueError(f"year must be between 1800 and {current + 1}, got {v}")
        return v

    @field_validator("doi")
    @classmethod
    def validate_doi(cls, v: str | None) -> str | None:
        # (FIX-AB-3) Delegates to the shared field-level helper so the model
        # and the source parsers normalize DOIs identically.  The empty value
        # is None; any other value that fails normalization is invalid.
        normalized = normalize_doi(v)
        if v is not None and v != "" and normalized is None:
            raise ValueError(f"invalid DOI format: {v}")
        return normalized

    @field_validator("pmid", mode="before")
    @classmethod
    def validate_pmid(cls, v: Any) -> str | None:
        if v is None or v == "":
            return None
        # (FIX-AB-3) Shared field-level helper (same rules as before).
        normalized = normalize_pmid(str(v))
        if normalized is None:
            raise ValueError(f"invalid PMID format: {v}")
        return normalized

    @field_validator("url", "pdf_url")
    @classmethod
    def validate_optional_url(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _is_valid_url(v):
            raise ValueError(f"invalid URL: {v}")
        return v

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_evidence(cls, data: Any) -> Any:
        """Fold the deprecated single ``evidence`` into ``evidence_list``."""
        if isinstance(data, dict):
            legacy = data.get("evidence")
            if legacy is not None and not data.get("evidence_list"):
                data["evidence_list"] = [legacy]
        return data

    @model_validator(mode="after")
    def _sync_evidence_compat(self) -> Paper:
        # ``evidence`` mirrors the highest-confidence entry of evidence_list,
        # lifted to the persisted synthetic composite when present (I-6).
        # Always mirror (not only when unset): a stale legacy ``evidence``
        # value passed alongside ``evidence_list`` must never shadow the list
        # maximum (M1). ``score_paper``/``score_author`` write the composite
        # here via ``model_copy``, which bypasses validators, so a computed
        # composite is never overwritten by the mirror.
        if self.evidence_list:
            if self.synthetic_confidence is not None:
                primary = max(self.evidence_list, key=lambda e: e.confidence)
                self.evidence = primary.model_copy(
                    update={"confidence": self.synthetic_confidence}
                )
            else:
                self.evidence = max(
                    self.evidence_list, key=lambda e: e.confidence
                )
        return self

    @property
    def primary_evidence(self) -> Evidence | None:
        """Highest-confidence evidence for this paper (or ``None``).

        Returns the composite written by :meth:`ConfidenceScorer.score_paper`
        when present, otherwise the highest-confidence ``evidence_list`` entry
        (the ``_sync_evidence_compat`` mirror guarantees the deprecated
        ``evidence`` alias never holds a lower value than the list max).
        """
        if self.evidence is not None:
            return self.evidence
        if self.evidence_list:
            return max(self.evidence_list, key=lambda e: e.confidence)
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Paper:
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

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Citation:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class CollectionResult(BaseModel):
    """Container for collection operation results.

    Attributes:
        authors: List of collected authors.
        papers: List of collected papers.
        citations: List of collected citation relationships.
        errors: List of error messages encountered during collection.
        warnings: List of data-quality warnings (e.g. multi-source field
            conflicts detected while merging duplicate records).
        stats: Dictionary of collection statistics.
    """

    authors: list[Author] = Field(default_factory=list)
    papers: list[Paper] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    errors: list[SourceFailure | str] = Field(default_factory=list)
    warnings: list[SourceFailure | str] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)

    def merge(self, other: CollectionResult) -> CollectionResult:
        """Merge another result into this one (returns new instance)."""
        return CollectionResult(
            authors=self.authors + other.authors,
            papers=self.papers + other.papers,
            citations=self.citations + other.citations,
            errors=self.errors + other.errors,
            warnings=self.warnings + other.warnings,
            stats={**self.stats, **other.stats},
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectionResult:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to a JSON string."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, raw: str) -> CollectionResult:
        """Deserialize from a JSON string."""
        return cls.model_validate_json(raw)


class ChangeType(StrEnum):
    """Classification of a paper record change during incremental update."""

    NEW = "new"  # New record
    UPDATED = "updated"  # Field-level update
    UNCHANGED = "unchanged"  # No meaningful change
    DELETED = "deleted"  # Record removal (optional)


class ChangeDetection(BaseModel):
    """Result of comparing one stored paper against a newly collected version."""

    paper_id: str
    change_type: ChangeType
    changed_fields: list[str] = Field(default_factory=list)
    old_values: dict[str, Any] = Field(default_factory=dict)
    new_values: dict[str, Any] = Field(default_factory=dict)
    confidence_delta: float = 0.0
    # Optional full paper payloads used when applying merges
    old_paper: Paper | None = None
    new_paper: Paper | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeDetection:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class IncrementalUpdateResult(BaseModel):
    """Aggregate result of an incremental update pass.

    ``warnings`` carries multi-source field conflicts (e.g. ``"year conflict:
    openalex=2025 vs arxiv=2017"``) detected while confidence-merging updated
    records during :meth:`~academic_intelligence.processors.incremental.IncrementalProcessor.apply_changes`
    (D-6).
    """

    new: list[Paper] = Field(default_factory=list)
    updated: list[ChangeDetection] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)  # paper_id list
    total_checked: int = 0
    sources_used: list[str] = Field(default_factory=list)
    warnings: list[SourceFailure | str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IncrementalUpdateResult:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to a JSON string."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, raw: str) -> IncrementalUpdateResult:
        """Deserialize from a JSON string."""
        return cls.model_validate_json(raw)


class ExpandStats(BaseModel):
    """Statistics collected during one graph expansion pass.

    Attributes:
        nodes_found: Number of newly discovered neighbor entities.
        edges_found: Number of newly discovered relationships.
        cache_hits: Number of neighbors already present in the session graph
            (discovered in a previous expand) — no duplicate work was needed.
        fetched_new: Number of entities newly fetched from data sources.
        failed: Number of relation expansions that failed (source unavailable,
            capability unsupported, etc.). Failures never block the pass.
        failures: Human-readable reasons for each failure, one entry per
            counted failure (``len(failures) == failed``). Entries carry the
            underlying exception message when one was raised, otherwise a
            descriptive reason (e.g. "no stored record and not a backfillable
            id").
        truncated: Whether the pass stopped early because of the depth or
            node-count limits.
        depth_reached: Number of BFS levels actually traversed.
    """

    nodes_found: int = 0
    edges_found: int = 0
    cache_hits: int = 0
    fetched_new: int = 0
    failed: int = 0
    failures: list[str] = Field(default_factory=list)
    truncated: bool = False
    depth_reached: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExpandStats:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)


class ExpandResult(BaseModel):
    """Result of expanding one entity's relationships in the knowledge graph.

    Attributes:
        center_id: The entity id that was expanded.
        nodes: Newly discovered entities as dicts with at least ``id``,
            ``type`` and ``loaded`` keys (plus ``title``/``name``/``year``
            when known).
        edges: Newly discovered relationships as dicts with ``source``,
            ``target`` and ``relation`` keys.
        stats: Aggregated :class:`ExpandStats` for the pass.
    """

    center_id: str
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    stats: ExpandStats = Field(default_factory=ExpandStats)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExpandResult:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to a JSON string."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, raw: str) -> ExpandResult:
        """Deserialize from a JSON string."""
        return cls.model_validate_json(raw)
