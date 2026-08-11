"""Trace-authors primitive: flatten citing-paper authors (Task 2).

Mechanically expands every citing paper into one :class:`AuthorRow` per
author and merges rows for the *same* author across papers.  The merge
key is the OpenAlex ``author_id`` when the authorship carries one,
otherwise the exact author name — **no** name normalization, similarity
matching, or disambiguation is ever performed (fuzzy merging is
deliberately out of scope; rows are keyed exactly as they appear in the
source data).

Output rows appear in first-seen (input) order; ``appears_in`` preserves
paper input order with duplicate paper ids collapsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class CitingPaper(Protocol):
    """Structural contract of the Task 1 citing-paper record (frozen).

    Mirrors the interface frozen by Task 1
    (``academic_intelligence/trace/citing.py``) so this module does not
    depend on that module's internals — any object exposing these
    attributes satisfies it structurally.
    """

    citing_paper_id: str
    doi: str | None
    title: str | None
    year: int | None
    venue: str | None
    authors_raw: list[str]
    authors_detail: list[dict[Any, Any]]


@dataclass
class AuthorRow:
    """One flattened author row, merged across citing papers.

    ``author_name`` / ``affiliation`` / ``author_id`` describe the first
    paper (input order) where the author was seen; ``appears_in`` lists
    every citing paper the author appears in.
    """

    author_name: str
    appears_in: list[str] = field(default_factory=list)
    affiliation: str | None = None
    author_id: str | None = None


@dataclass
class _AuthorAccumulator:
    """Mutable merge state for one author key (internal)."""

    author_name: str
    affiliation: str | None
    author_id: str | None
    appears_in: list[str] = field(default_factory=list)

    def to_row(self) -> AuthorRow:
        """Snapshot the accumulator as an output row."""
        return AuthorRow(
            author_name=self.author_name,
            appears_in=list(self.appears_in),
            affiliation=self.affiliation,
            author_id=self.author_id,
        )


def _str_value(mapping: Any, key: str) -> str | None:
    """Read a non-empty string field from an untyped mapping, or ``None``."""
    if isinstance(mapping, dict):
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _author_attr(entry: dict[Any, Any], key: str) -> str | None:
    """Read ``author.<key>`` from an OpenAlex authorship entry."""
    return _str_value(entry.get("author"), key)


def _institution(entry: dict[Any, Any]) -> str | None:
    """Read ``institutions[0].display_name`` from an authorship entry."""
    institutions = entry.get("institutions")
    if isinstance(institutions, list) and institutions:
        return _str_value(institutions[0], "display_name")
    return None


def _author_slots(
    paper: CitingPaper, affiliation_filter: str | None
) -> list[tuple[str, str | None, str | None]]:
    """Extract ``(name, affiliation, author_id)`` slots of one paper.

    ``authors_detail`` is the primary source; a slot missing its detail
    entry (or missing ``author.display_name``) falls back to
    ``authors_raw``.  When ``affiliation_filter`` is set, occurrences
    whose affiliation is missing or does not contain the substring are
    dropped (mechanical substring filter, applied per occurrence).
    """
    detail = paper.authors_detail
    raw = paper.authors_raw
    slots: list[tuple[str, str | None, str | None]] = []
    for i in range(max(len(detail), len(raw))):
        raw_name = raw[i] if i < len(raw) else None
        entry = detail[i] if i < len(detail) else None
        if not isinstance(entry, dict):
            entry = None
        name = _author_attr(entry, "display_name") if entry is not None else None
        if name is None and raw_name:
            name = raw_name
        if not name:
            continue
        affiliation = _institution(entry) if entry is not None else None
        if affiliation_filter is not None and (
            affiliation is None or affiliation_filter not in affiliation
        ):
            continue
        author_id = _author_attr(entry, "id") if entry is not None else None
        slots.append((name, affiliation, author_id))
    return slots


def flatten_authors(
    citing_papers: list[CitingPaper],
    *,
    affiliation_filter: str | None = None,
) -> list[AuthorRow]:
    """Flatten authors of ``citing_papers`` into one row per author.

    Each author of each paper yields one row, merged across papers: the
    merge key is ``author_id`` when the authorship carries one, otherwise
    the exact ``author_name`` (two rows are *never* merged on fuzzy
    criteria).  For a merged row, ``author_name`` / ``affiliation`` /
    ``author_id`` are taken from the first paper (input order) where the
    author was seen; ``appears_in`` collects every citing paper id.

    ``affiliation_filter`` is a mechanical case-sensitive substring
    filter applied per author-paper occurrence *before* merging:
    occurrences whose affiliation is missing or does not contain the
    substring are dropped.  It is a filter tool, not a disambiguation
    judgment.
    """
    accumulators: dict[tuple[str, str], _AuthorAccumulator] = {}
    for paper in citing_papers:
        for author_name, affiliation, author_id in _author_slots(paper, affiliation_filter):
            key = ("id", author_id) if author_id is not None else ("name", author_name)
            acc = accumulators.get(key)
            if acc is None:
                acc = _AuthorAccumulator(
                    author_name=author_name,
                    affiliation=affiliation,
                    author_id=author_id,
                )
                accumulators[key] = acc
            if paper.citing_paper_id not in acc.appears_in:
                acc.appears_in.append(paper.citing_paper_id)
    return [acc.to_row() for acc in accumulators.values()]
