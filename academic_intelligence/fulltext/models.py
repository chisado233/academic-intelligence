"""Full-text pipeline data models.

Defines the ``OALocation`` (a legal open-access link found by the locator),
``Segment`` (one paragraph of extracted text with its page number) and
``FullText`` (the result of the whole pipeline) contracts.

These models are intentionally dependency-light (pydantic only) so the
storage layer can import them without a cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["OALocation", "Segment", "FullText"]


class OALocation(BaseModel):
    """A legal open-access full-text link located for a paper.

    Attributes:
        url: Absolute HTTP(S) URL of the OA full text (PDF preferred).
        source: Which locator found it — ``"unpaywall"`` / ``"core"`` /
            ``"arxiv"``.
        license: License reported by the source (e.g. ``"cc-by"``), when
            known.
        host_type: Unpaywall ``host_type`` classification (publisher /
            repository / ...), when the source reports one.
    """

    url: str
    source: str
    license: str | None = None
    host_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")


class Segment(BaseModel):
    """One paragraph of extracted full text with provenance.

    Attributes:
        heading: Text of the nearest preceding heading line (font-based
            heuristic), or ``None`` when the paragraph has no detected
            heading.
        text: Paragraph text (wrapped lines joined with a single space).
        page: 1-based PDF page number the paragraph appears on.
    """

    heading: str | None = None
    text: str
    page: int = Field(ge=1)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")


class FullText(BaseModel):
    """Result of the full-text pipeline for one paper.

    Attributes:
        paper_id: Paper record id the text belongs to (papers.id reference).
        source: Full-text source that supplied the file
            (``"unpaywall"`` / ``"core"`` / ``"arxiv"``).
        oa_license: License of the OA copy, when known.
        file_path: Local path of the cached PDF.
        paragraph_count: Number of paragraphs (== ``len(segments)``).
        segments: Paragraph segments with heading / text / page.
        collected_at: UTC timestamp of collection.
    """

    paper_id: str
    source: str
    oa_license: str | None = None
    file_path: str | None = None
    paragraph_count: int = Field(ge=0)
    segments: list[Segment] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")
