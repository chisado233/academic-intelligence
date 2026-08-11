"""Tests for the trace-authors flattening primitive (Task 2).

Covers the frozen interface of
``academic_intelligence.trace.authors.flatten_authors``:

- one row per author across papers, with ``appears_in`` aggregation
- mechanical merge key: OpenAlex ``author_id`` when present, otherwise
  exact string-equal name (no fuzzy matching / disambiguation)
- affiliation extraction from ``authors_detail[].institutions``
- ``affiliation_filter`` substring filtering (applied per occurrence)
- fallback to ``authors_raw`` when detail is missing
- empty input and ``None`` field rows
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from academic_intelligence.trace.authors import CitingPaper, flatten_authors


@dataclass
class _FakePaper:
    """Minimal structurally-valid :class:`CitingPaper` for tests."""

    citing_paper_id: str
    authors_raw: list[str]
    authors_detail: list[dict[Any, Any]] = field(default_factory=list)
    doi: str | None = None
    title: str | None = None
    year: int | None = None
    venue: str | None = None


def _detail(
    name: str, *, author_id: str | None = None, institution: str | None = None
) -> dict[Any, Any]:
    """Build one OpenAlex authorship entry."""
    author: dict[str, object] = {"display_name": name}
    if author_id is not None:
        author["id"] = author_id
    entry: dict[str, object] = {"author": author}
    if institution is not None:
        entry["institutions"] = [{"display_name": institution}]
    return entry


def test_empty_input() -> None:
    assert flatten_authors([]) == []


def test_flattens_one_row_per_author() -> None:
    papers: list[CitingPaper] = [
        _FakePaper(
            "p1",
            ["Alice", "Bob"],
            authors_detail=[
                _detail("Alice", author_id="A1", institution="MIT"),
                _detail("Bob", author_id="B1", institution="Stanford"),
            ],
        ),
        _FakePaper("p2", ["Carol"], authors_detail=[_detail("Carol", author_id="C1")]),
    ]
    rows = flatten_authors(papers)
    assert len(rows) == 3
    assert rows[0].author_name == "Alice"
    assert rows[0].appears_in == ["p1"]
    assert rows[0].affiliation == "MIT"
    assert rows[0].author_id == "A1"
    assert rows[1].author_name == "Bob"
    assert rows[1].appears_in == ["p1"]
    assert rows[1].affiliation == "Stanford"
    assert rows[2].author_name == "Carol"
    assert rows[2].appears_in == ["p2"]
    assert rows[2].affiliation is None


def test_same_author_aggregates_across_papers_by_id() -> None:
    papers: list[CitingPaper] = [
        _FakePaper(
            "p1", ["Alice"], authors_detail=[_detail("Alice", author_id="A1", institution="MIT")]
        ),
        _FakePaper(
            "p2", ["Alice"], authors_detail=[_detail("Alice", author_id="A1", institution="MIT")]
        ),
        _FakePaper("p3", ["Bob"], authors_detail=[_detail("Bob", author_id="B1")]),
    ]
    rows = flatten_authors(papers)
    assert len(rows) == 2
    alice = rows[0]
    assert alice.author_name == "Alice"
    assert alice.appears_in == ["p1", "p2"]
    assert alice.author_id == "A1"


def test_same_exact_name_aggregates_without_ids() -> None:
    papers: list[CitingPaper] = [
        _FakePaper("p1", ["Alice"]),
        _FakePaper("p2", ["Alice"]),
    ]
    rows = flatten_authors(papers)
    assert len(rows) == 1
    assert rows[0].author_name == "Alice"
    assert rows[0].appears_in == ["p1", "p2"]
    assert rows[0].author_id is None


def test_same_name_different_ids_not_merged() -> None:
    papers: list[CitingPaper] = [
        _FakePaper("p1", ["Alice"], authors_detail=[_detail("Alice", author_id="A1")]),
        _FakePaper("p2", ["Alice"], authors_detail=[_detail("Alice", author_id="A2")]),
    ]
    rows = flatten_authors(papers)
    assert len(rows) == 2
    assert [row.author_id for row in rows] == ["A1", "A2"]


def test_id_keyed_and_name_keyed_rows_not_merged() -> None:
    papers: list[CitingPaper] = [
        _FakePaper("p1", ["Alice"], authors_detail=[_detail("Alice", author_id="A1")]),
        _FakePaper("p2", ["Alice"]),
    ]
    rows = flatten_authors(papers)
    assert len(rows) == 2


def test_merged_row_takes_first_seen_name_and_affiliation() -> None:
    papers: list[CitingPaper] = [
        _FakePaper(
            "p1", ["Alice"], authors_detail=[_detail("Alice", author_id="A1", institution="MIT")]
        ),
        _FakePaper(
            "p2",
            ["A. Smith"],
            authors_detail=[_detail("A. Smith", author_id="A1", institution="Stanford")],
        ),
    ]
    rows = flatten_authors(papers)
    assert len(rows) == 1
    assert rows[0].author_name == "Alice"
    assert rows[0].affiliation == "MIT"
    assert rows[0].appears_in == ["p1", "p2"]


def test_affiliation_extracted_from_first_institution() -> None:
    paper: CitingPaper = _FakePaper(
        "p1",
        ["Alice"],
        authors_detail=[_detail("Alice", author_id="A1", institution="Tsinghua University")],
    )
    rows = flatten_authors([paper])
    assert rows[0].affiliation == "Tsinghua University"


def test_missing_institutions_yield_none_affiliation() -> None:
    paper: CitingPaper = _FakePaper(
        "p1",
        ["Alice"],
        authors_detail=[{"author": {"id": "A1", "display_name": "Alice"}, "institutions": []}],
    )
    rows = flatten_authors([paper])
    assert rows[0].affiliation is None
    assert rows[0].author_id == "A1"


def test_no_detail_falls_back_to_authors_raw() -> None:
    paper: CitingPaper = _FakePaper("p1", ["Alice", "Bob"])
    rows = flatten_authors([paper])
    assert [row.author_name for row in rows] == ["Alice", "Bob"]
    assert all(row.author_id is None for row in rows)
    assert all(row.affiliation is None for row in rows)
    assert rows[0].appears_in == ["p1"]


def test_partial_detail_falls_back_per_slot() -> None:
    paper: CitingPaper = _FakePaper(
        "p1", ["Alice", "Bob"], authors_detail=[_detail("Alice", author_id="A1")]
    )
    rows = flatten_authors([paper])
    assert len(rows) == 2
    assert rows[0].author_name == "Alice"
    assert rows[0].author_id == "A1"
    assert rows[1].author_name == "Bob"
    assert rows[1].author_id is None


def test_detail_entry_missing_display_name_falls_back_to_raw() -> None:
    paper: CitingPaper = _FakePaper("p1", ["Alice"], authors_detail=[{"author": {"id": "A1"}}])
    rows = flatten_authors([paper])
    assert rows[0].author_name == "Alice"
    assert rows[0].author_id == "A1"


def test_affiliation_filter_keeps_only_matching_occurrences() -> None:
    papers: list[CitingPaper] = [
        _FakePaper(
            "p1",
            ["Alice"],
            authors_detail=[_detail("Alice", author_id="A1", institution="Tsinghua University")],
        ),
        _FakePaper(
            "p2", ["Alice"], authors_detail=[_detail("Alice", author_id="A1", institution="MIT")]
        ),
        _FakePaper(
            "p3", ["Bob"], authors_detail=[_detail("Bob", author_id="B1", institution="MIT")]
        ),
    ]
    rows = flatten_authors(papers, affiliation_filter="MIT")
    assert len(rows) == 2
    by_name = {row.author_name: row for row in rows}
    assert by_name["Alice"].appears_in == ["p2"]
    assert by_name["Alice"].affiliation == "MIT"
    assert by_name["Bob"].appears_in == ["p3"]
    assert by_name["Bob"].affiliation == "MIT"


def test_affiliation_filter_drops_missing_affiliations() -> None:
    paper: CitingPaper = _FakePaper("p1", ["Alice"])
    assert flatten_authors([paper], affiliation_filter="MIT") == []


def test_affiliation_filter_is_case_sensitive_substring() -> None:
    paper: CitingPaper = _FakePaper(
        "p1",
        ["Alice"],
        authors_detail=[_detail("Alice", institution="Tsinghua University")],
    )
    assert flatten_authors([paper], affiliation_filter="Tsinghua") != []
    assert flatten_authors([paper], affiliation_filter="tsinghua") == []


def test_none_field_rows_kept_with_none_id_and_affiliation() -> None:
    paper: CitingPaper = _FakePaper(
        "p1", ["Alice"], authors_detail=[{"author": {"display_name": "Alice"}}]
    )
    rows = flatten_authors([paper])
    assert len(rows) == 1
    assert rows[0].author_name == "Alice"
    assert rows[0].author_id is None
    assert rows[0].affiliation is None


def test_duplicate_author_entries_in_one_paper_collapse_paper_id() -> None:
    paper: CitingPaper = _FakePaper(
        "p1",
        ["Alice", "Alice"],
        authors_detail=[
            _detail("Alice", author_id="A1"),
            _detail("Alice", author_id="A1"),
        ],
    )
    rows = flatten_authors([paper])
    assert len(rows) == 1
    assert rows[0].appears_in == ["p1"]


def test_blank_raw_name_slot_skipped() -> None:
    paper: CitingPaper = _FakePaper("p1", ["", "Bob"])
    rows = flatten_authors([paper])
    assert [row.author_name for row in rows] == ["Bob"]
