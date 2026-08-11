"""Author identity domain models (WP6).

Typed payloads exchanged by the :mod:`academic_intelligence.identity`
resolver / fetcher layers:

- :class:`RepresentativePaper` — one representative work of an author
  (used by ``profile``, sorted by citation count desc);
- :class:`AuthorProfile` — the full source profile (institution /
  h-index / homepage / interests / representative papers) plus the
  evidence chain that produced it;
- :class:`AuthorCandidate` — one same-name candidate from a source search
  or the global identity table, carrying the disambiguation features and
  the composite score against the queried author;
- :class:`ResolveResult` — the outcome of ``Resolver.resolve(paper_id,
  name)`` (match kind + profile + candidate comparison table + evidence
  chain);
- :class:`ConfirmResult` — the outcome of ``Resolver.confirm(...)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

#: Allowed authority systems for an author id (aligns with the storage
#: ``source`` column of ``author_identity_global``).
AUTHOR_ID_SOURCES = ("openalex", "s2", "orcid")


class RepresentativePaper(BaseModel):
    """One representative work of an author (``profile`` output)."""

    title: str
    year: int | None = None
    cited_by_count: int = 0
    venue: str | None = None
    work_id: str | None = None
    doi: str | None = None


class AuthorProfile(BaseModel):
    """Full author profile fetched from one source (design §9 / §1.5).

    ``representative_papers`` are sorted by ``cited_by_count`` descending
    (Q3: DeepSeek-AI 代表作按引用数客观排序).  ``evidence`` carries the
    provenance chain (source / url / id / confidence) that produced the
    profile.
    """

    name: str
    author_id: str
    source: str
    affiliation: str | None = None
    homepage: str | None = None
    h_index: int | None = None
    citations: int | None = None
    paper_count: int | None = None
    interests: list[str] = Field(default_factory=list)
    profile_url: str | None = None
    representative_papers: list[RepresentativePaper] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict (CLI ``--output`` / JSON rendering)."""
        return self.model_dump(mode="json")


class AuthorCandidate(BaseModel):
    """One same-name candidate with disambiguation context.

    ``candidate_id`` is the source-qualified id (``"openalex:A123"`` /
    ``"s2:12345"``) that ``confirm`` accepts as its first argument.
    ``score`` / ``verdict`` are the disambiguation outcome against the
    queried author (or the queried name for ``search``); ``features``
    carries the per-feature breakdown of the composite score.
    """

    candidate_id: str
    source: str
    name: str
    affiliation: str | None = None
    interests: list[str] = Field(default_factory=list)
    coauthors: list[str] = Field(default_factory=list)
    active_years: list[int] = Field(default_factory=list)
    venues: list[str] = Field(default_factory=list)
    h_index: int | None = None
    citations: int | None = None
    paper_count: int | None = None
    profile_url: str | None = None
    score: float | None = None
    verdict: str | None = None
    features: dict[str, float] = Field(default_factory=dict)
    paper_match: bool = False
    evidence: list[dict[str, Any]] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict (CLI candidate table / ``--output``)."""
        return self.model_dump(mode="json")


class ResolveResult(BaseModel):
    """Outcome of ``Resolver.resolve(paper_id, name)``.

    ``match``:

    - ``"confirmed"`` — a confirmed ``author_identity_global`` row was hit
      directly (I8 cross-paper reuse);
    - ``"id_linked"`` — the paper's ``AuthorRef`` carries an authority id
      and the source profile was fetched (branch A);
    - ``"auto"`` — branch B best candidate scores ``>= 0.85`` (判同);
    - ``"ambiguous"`` — best candidate in the ``0.60 .. 0.85`` band (D3:
      candidates listed, never hard-merged);
    - ``"different"`` — best candidate ``< 0.60`` (不同人);
    - ``"not_found"`` — no candidate found at all.

    ``evidence_chain`` is the ordered provenance list for the resolution
    (source URLs / ids / confidence) so the user can audit every claim.
    """

    paper_id: str
    author_name: str
    match: str = "not_found"
    profile: AuthorProfile | None = None
    candidates: list[AuthorCandidate] = Field(default_factory=list)
    evidence_chain: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict (CLI ``--output`` / JSON rendering)."""
        return self.model_dump(mode="json")


class ConfirmResult(BaseModel):
    """Outcome of ``Resolver.confirm(candidate_id, paper_id, name)``.

    Confirming writes the ``author_identity_global`` row
    (``status="confirmed"``) plus the paper-level ``author_identity``
    evidence link; a later ``resolve`` of the same name returns the
    confirmed identity directly (I8).
    """

    author_name: str
    author_id: str
    source: str
    paper_id: str
    confirmed_by: str
    status: str = "confirmed"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict (CLI ``--output`` / JSON rendering)."""
        return self.model_dump(mode="json")


def evidence_entry(
    source: str,
    source_url: str,
    *,
    source_id: str | None = None,
    confidence: float = 0.85,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build one evidence-chain entry (JSON-compatible)."""
    return {
        "source": source,
        "source_url": source_url,
        "source_id": source_id,
        "collected_at": datetime.now(UTC).isoformat(),
        "confidence": confidence,
        "detail": detail,
    }
