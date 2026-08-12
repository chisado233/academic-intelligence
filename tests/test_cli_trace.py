"""Integration tests for the trace-chain CLI (Task 4).

Exercises the real ``paper`` command tree with the CLI's network entry points
(``cli_trace.fetch_citing_papers`` / ``cli_trace.fetch_profiles``) replaced by
in-memory fakes, so the suite runs fully offline.  Coverage:

- ``--help`` for all three commands + presence in the root help;
- the chained flow citing.csv → authors.csv → profiles.csv, asserting column
  names, cell content and the UTF-8 BOM on every file;
- CSV-to-stdout mode stays clean (progress/warnings go to stderr);
- error mapping: all-sources-failed exits 2; partial failures print PARTIAL
  and keep exit 0; unknown ``--sources`` / malformed CSV exit 2;
- ``--limit``/``--resume-from`` chunked fetching with per-page progress and
  the ``--resume-from`` continuation hint;
- ``trace-authors`` with a paper-id input (authors_detail flows through);
- ``--affiliation-filter``;
- ``trace-profiles`` all-failed / partial semantics.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import academic_intelligence.cli_trace as cli_trace
from academic_intelligence.cli import app
from academic_intelligence.core.exceptions import SourceFailure
from academic_intelligence.trace.citing import CitingPaper, CitingResult
from academic_intelligence.trace.profiles import AuthorProfile

runner = CliRunner()

CITING_W_ID = "W2257979135"

_CITING_HEADER = "citing_paper_id,doi,title,year,venue,authors_raw,authors_detail"
_AUTHOR_HEADER = "author_name,appears_in,affiliation,author_id"
_PROFILE_HEADER = "author_name,author_id,institution,h_index,fields,works_count,top_works"


def _paper(
    paper_id: str,
    *,
    authors: tuple[str, ...] = ("Alice", "Bob"),
    institutions: tuple[str | None, ...] | None = None,
    author_ids: tuple[str, ...] | None = None,
    title: str = "A citing paper",
    year: int | None = 2024,
    doi: str | None = None,
    venue: str | None = "Nature",
) -> CitingPaper:
    """Build a realistic citing-paper record (OpenAlex style detail)."""
    if institutions is None:
        institutions = (None,) * len(authors)
    if author_ids is None:
        author_ids = tuple(f"https://openalex.org/A{i + 1}" for i in range(len(authors)))
    detail: list[dict[str, Any]] = []
    for name, inst, aid in zip(authors, institutions, author_ids, strict=True):
        entry: dict[str, Any] = {
            "author": {
                "id": aid,
                "display_name": name,
            }
        }
        if inst is not None:
            entry["institutions"] = [{"display_name": inst}]
        detail.append(entry)
    return CitingPaper(
        citing_paper_id=paper_id,
        doi=doi,
        title=title,
        year=year,
        venue=venue,
        authors_raw=list(authors),
        authors_detail=detail,
    )


def _result(
    *papers: CitingPaper,
    errors: tuple[SourceFailure, ...] = (),
    resume_cursor: str | None = None,
) -> CitingResult:
    return CitingResult(
        papers=list(papers),
        resume_cursor=resume_cursor,
        source_stats={"openalex": len(papers), "opencitations": 0},
        written_stats={"openalex": len(papers), "opencitations": 0},
        errors=list(errors),
    )


def _failure(source: str = "openalex", message: str = "boom") -> SourceFailure:
    return SourceFailure.from_message(
        source=source, operation="fetch_citing_papers", message=message
    )


def _install_citing(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Any,
) -> None:
    """Route ``cli_trace.fetch_citing_papers`` to a fake async callable."""
    monkeypatch.setattr(cli_trace, "fetch_citing_papers", fetch)


def _install_profiles(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Any,
) -> None:
    """Route ``cli_trace.fetch_profiles`` to a fake async callable."""
    monkeypatch.setattr(cli_trace, "fetch_profiles", fetch)


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read a trace CSV written by the CLI (UTF-8 + BOM tolerant)."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# Help surface
# ---------------------------------------------------------------------------


def test_root_help_lists_trace_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "trace-citing" in result.stdout
    assert "trace-authors" in result.stdout
    assert "trace-profiles" in result.stdout


def test_trace_commands_help() -> None:
    for command in ("trace-citing", "trace-authors", "trace-profiles"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert command in result.stdout


# ---------------------------------------------------------------------------
# Chained flow: trace-citing CSV → trace-authors CSV → trace-profiles CSV
# ---------------------------------------------------------------------------


def test_full_chain_csv_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        return _result(
            _paper(
                "W1",
                authors=("Alice", "Bob"),
                institutions=("MIT", "Stanford"),
                author_ids=("A1", "A2"),
                doi="10.1000/1",
                title="Alpha",
                year=2023,
                venue="Venue A",
            ),
            _paper(
                "W2",
                authors=("Bob", "Carol"),
                institutions=("Stanford", "Oxford"),
                author_ids=("A2", "A3"),
                doi="10.1000/2",
                title="Beta",
                year=2024,
                venue="Venue B",
            ),
        )

    _install_citing(monkeypatch, fake_fetch)

    # --- step 1: trace-citing ---
    citing_csv = tmp_path / "citing.csv"
    result = runner.invoke(app, ["trace-citing", CITING_W_ID, "--output", str(citing_csv)])
    assert result.exit_code == 0, result.output
    assert citing_csv.read_bytes()[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM
    rows = _read_csv(citing_csv)
    assert list(rows[0]) == [
        "citing_paper_id",
        "doi",
        "title",
        "year",
        "venue",
        "authors_raw",
        "authors_detail",
    ]
    assert [r["citing_paper_id"] for r in rows] == ["W1", "W2"]
    assert rows[0]["doi"] == "10.1000/1"
    assert rows[0]["title"] == "Alpha"
    assert rows[0]["year"] == "2023"
    assert rows[0]["venue"] == "Venue A"
    assert rows[0]["authors_raw"] == "Alice;Bob"
    # The detail column round-trips the OpenAlex authorships verbatim.
    detail = json.loads(rows[0]["authors_detail"])
    assert [d["author"]["display_name"] for d in detail] == ["Alice", "Bob"]
    assert [d["author"]["id"] for d in detail] == ["A1", "A2"]
    assert detail[0]["institutions"][0]["display_name"] == "MIT"

    # --- step 2: trace-authors reads the CSV (detail preserved through the
    # file, so affiliation/author_id survive the chain — no more all-placeholder
    # authors) ---
    authors_csv = tmp_path / "authors.csv"
    result = runner.invoke(app, ["trace-authors", str(citing_csv), "--output", str(authors_csv)])
    assert result.exit_code == 0, result.output
    assert authors_csv.read_bytes()[:3] == b"\xef\xbb\xbf"
    rows = _read_csv(authors_csv)
    assert list(rows[0]) == [
        "author_name",
        "appears_in",
        "affiliation",
        "author_id",
    ]
    assert [r["author_name"] for r in rows] == ["Alice", "Bob", "Carol"]
    assert rows[0]["affiliation"] == "MIT"
    assert rows[0]["author_id"] == "A1"
    assert rows[1]["appears_in"] == "W1;W2"  # Bob cites both papers (id A2)
    assert rows[1]["affiliation"] == "Stanford"
    assert rows[1]["author_id"] == "A2"
    assert rows[2]["affiliation"] == "Oxford"
    assert rows[2]["author_id"] == "A3"

    # --- step 3: trace-profiles reads the authors CSV.  The fake mirrors the
    # real contract (rows with an author_id get enriched; rows without are
    # placeholders), so the assertions below fail if the CSV chain dropped
    # the detail — a real chain assertion. ---
    async def fake_profiles(
        author_rows: Any,
        *,
        batch_size: int = 20,
        http: Any = None,
    ) -> list[AuthorProfile]:
        out: list[AuthorProfile] = []
        for row in author_rows:
            if row.author_id is None:
                out.append(AuthorProfile(author_name=row.author_name, author_id=None))
            else:
                out.append(
                    AuthorProfile(
                        author_name=row.author_name,
                        author_id=row.author_id,
                        institution=row.affiliation,
                        h_index=42,
                        fields=["Computer Vision", "Object Detection"],
                        works_count=120,
                        top_works=[
                            {
                                "title": "Top Work",
                                "venue": "CVPR",
                                "year": 2020,
                                "cited_by_count": 10,
                            }
                        ],
                    )
                )
        return out

    _install_profiles(monkeypatch, fake_profiles)
    profiles_csv = tmp_path / "profiles.csv"
    result = runner.invoke(
        app,
        ["trace-profiles", str(authors_csv), "--output", str(profiles_csv)],
    )
    assert result.exit_code == 0, result.output
    assert profiles_csv.read_bytes()[:3] == b"\xef\xbb\xbf"
    rows = _read_csv(profiles_csv)
    assert list(rows[0]) == [
        "author_name",
        "author_id",
        "institution",
        "h_index",
        "fields",
        "works_count",
        "top_works",
    ]
    assert [r["author_name"] for r in rows] == ["Alice", "Bob", "Carol"]
    # Chain result: every row has an ID and an institution — no placeholders.
    assert [r["author_id"] for r in rows] == ["A1", "A2", "A3"]
    assert [r["institution"] for r in rows] == ["MIT", "Stanford", "Oxford"]
    assert rows[0]["h_index"] == "42"
    assert rows[0]["fields"] == "Computer Vision;Object Detection"
    assert rows[0]["works_count"] == "120"
    top_works = json.loads(rows[0]["top_works"])
    assert top_works[0]["title"] == "Top Work"
    assert top_works[0]["cited_by_count"] == 10


# ---------------------------------------------------------------------------
# stdout / stderr separation + progress
# ---------------------------------------------------------------------------


def test_trace_citing_stdout_is_clean_csv_with_progress_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        return _result(_paper("W1"), _paper("W2"))

    _install_citing(monkeypatch, fake_fetch)

    result = runner.invoke(app, ["trace-citing", CITING_W_ID])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == _CITING_HEADER
    assert "Alice;Bob" in result.stdout
    # Human-facing output never pollutes the CSV stream.
    assert "page" in result.stderr
    assert "Found" in result.stderr
    assert "page" not in result.stdout


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_trace_citing_all_sources_fail_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        return _result(errors=(_failure(), _failure(source="opencitations", message="nope")))

    _install_citing(monkeypatch, fake_fetch)

    result = runner.invoke(app, ["trace-citing", CITING_W_ID])
    assert result.exit_code == 2
    assert "every source failed" in result.stderr


def test_trace_citing_partial_failure_prints_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        return _result(_paper("W1"), errors=(_failure(),))

    _install_citing(monkeypatch, fake_fetch)

    citing_csv = tmp_path / "citing.csv"
    result = runner.invoke(app, ["trace-citing", CITING_W_ID, "--output", str(citing_csv)])
    assert result.exit_code == 0
    assert "PARTIAL" in result.stderr
    assert citing_csv.exists()
    assert citing_csv.read_bytes()[:3] == b"\xef\xbb\xbf"


def test_trace_citing_unknown_source_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        return _result(_paper("W1"))

    _install_citing(monkeypatch, fake_fetch)

    result = runner.invoke(app, ["trace-citing", CITING_W_ID, "--sources", "bogus"])
    assert result.exit_code == 2
    assert "unknown trace source" in result.output


def test_trace_authors_empty_csv_exits_2(tmp_path: Path) -> None:
    citing_csv = tmp_path / "citing.csv"
    citing_csv.write_text(f"\ufeff{_CITING_HEADER}\n", encoding="utf-8")
    result = runner.invoke(app, ["trace-authors", str(citing_csv)])
    assert result.exit_code == 2
    assert "no citing papers" in result.stderr


def test_trace_authors_missing_column_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("\ufeffciting_paper_id,title\nW1,Alpha\n", encoding="utf-8")
    result = runner.invoke(app, ["trace-authors", str(bad)])
    assert result.exit_code == 2
    assert "missing column" in result.output


def test_trace_profiles_not_a_file_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["trace-profiles", str(tmp_path / "missing.csv")])
    assert result.exit_code == 2
    assert "not a file" in result.output


def test_trace_profiles_all_failed_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authors_csv = tmp_path / "authors.csv"
    authors_csv.write_text(f"\ufeff{_AUTHOR_HEADER}\nAlice,W1,,A1\n", encoding="utf-8")

    async def fake_profiles(
        author_rows: Any,
        *,
        batch_size: int = 20,
        http: Any = None,
    ) -> list[AuthorProfile]:
        return [
            AuthorProfile(
                author_name=row.author_name,
                author_id=row.author_id,
                source="",  # 双源失败无来源（新契约）
                errors=["fetch failed: boom"],
            )
            for row in author_rows
        ]

    _install_profiles(monkeypatch, fake_profiles)

    result = runner.invoke(app, ["trace-profiles", str(authors_csv)])
    assert result.exit_code == 2
    assert "every author profile" in result.stderr


def test_trace_profiles_partial_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authors_csv = tmp_path / "authors.csv"
    authors_csv.write_text(
        f"\ufeff{_AUTHOR_HEADER}\nAlice,W1,,A1\nBob,W1,,A2\n",
        encoding="utf-8",
    )

    async def fake_profiles(
        author_rows: Any,
        *,
        batch_size: int = 20,
        http: Any = None,
    ) -> list[AuthorProfile]:
        out: list[AuthorProfile] = []
        for row in author_rows:
            if row.author_id == "A2":
                out.append(
                    AuthorProfile(
                        author_name=row.author_name,
                        author_id=row.author_id,
                        source="",  # 双源失败无来源（新契约）
                        errors=["fetch failed: boom"],
                    )
                )
            else:
                out.append(
                    AuthorProfile(
                        author_name=row.author_name,
                        author_id=row.author_id,
                        institution="MIT",
                    )
                )
        return out

    _install_profiles(monkeypatch, fake_profiles)

    result = runner.invoke(
        app, ["trace-profiles", str(authors_csv), "--output", str(tmp_path / "p.csv")]
    )
    assert result.exit_code == 0
    assert "PARTIAL" in result.stderr
    assert "1/2" in result.stderr


# ---------------------------------------------------------------------------
# trace-authors inputs + filters
# ---------------------------------------------------------------------------


def test_trace_authors_paper_id_input_keeps_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        return _result(
            _paper(
                "W1",
                authors=("Alice", "Bob"),
                institutions=("MIT", "Stanford"),
            )
        )

    _install_citing(monkeypatch, fake_fetch)

    authors_csv = tmp_path / "authors.csv"
    result = runner.invoke(app, ["trace-authors", CITING_W_ID, "--output", str(authors_csv)])
    assert result.exit_code == 0, result.output
    rows = _read_csv(authors_csv)
    assert [r["author_name"] for r in rows] == ["Alice", "Bob"]
    assert rows[0]["affiliation"] == "MIT"
    assert rows[0]["author_id"] == "https://openalex.org/A1"
    assert rows[0]["appears_in"] == "W1"


def test_trace_authors_affiliation_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        return _result(
            _paper(
                "W1",
                authors=("Alice", "Bob"),
                institutions=("MIT", "Stanford"),
            )
        )

    _install_citing(monkeypatch, fake_fetch)

    result = runner.invoke(app, ["trace-authors", CITING_W_ID, "--affiliation-filter", "MIT"])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(result.stdout.splitlines()))
    assert [r["author_name"] for r in rows] == ["Alice"]


# ---------------------------------------------------------------------------
# --limit / --resume-from chunked fetching
# ---------------------------------------------------------------------------


def test_trace_citing_chunked_fetch_with_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        calls.append(resume_from)
        if resume_from is None:
            return _result(_paper("W1"), _paper("W2"), resume_cursor="cursor-2")
        return _result(_paper("W3"))

    _install_citing(monkeypatch, fake_fetch)

    citing_csv = tmp_path / "citing.csv"
    result = runner.invoke(
        app,
        ["trace-citing", CITING_W_ID, "--limit", "3", "--output", str(citing_csv)],
    )
    assert result.exit_code == 0, result.output
    assert calls == [None, "cursor-2"]  # resumed exactly once
    assert [r["citing_paper_id"] for r in _read_csv(citing_csv)] == [
        "W1",
        "W2",
        "W3",
    ]
    assert "page 1" in result.stderr
    assert "page 2" in result.stderr


def test_trace_citing_truncated_prints_resume_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        return _result(_paper("W1"), _paper("W2"), resume_cursor="cursor-2")

    _install_citing(monkeypatch, fake_fetch)

    result = runner.invoke(app, ["trace-citing", CITING_W_ID, "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "--resume-from cursor-2" in result.stderr
    assert "truncated" in result.stderr


def test_trace_citing_opencitations_fetched_only_on_first_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-4: OpenCitations is unpaginated — later resume chunks skip it."""
    calls: list[list[str] | None] = []

    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        calls.append(sources)
        if resume_from is None:
            return _result(_paper("W1"), resume_cursor="cursor-2")
        return _result(_paper("W2"))

    _install_citing(monkeypatch, fake_fetch)

    result = runner.invoke(app, ["trace-citing", CITING_W_ID, "--limit", "10"])
    assert result.exit_code == 0, result.output
    # First chunk pulls both sources; the continuation chunk is openalex-only.
    assert calls == [["openalex", "opencitations"], ["openalex"]]
    rows = list(csv.DictReader(result.stdout.splitlines()))
    assert [r["citing_paper_id"] for r in rows] == ["W1", "W2"]


def test_trace_citing_resume_skips_opencitations_with_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-4: a --resume-from run does not re-pull COCI (pulled by the earlier run)."""
    calls: list[list[str] | None] = []

    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        calls.append(sources)
        return _result(_paper("W1"), resume_cursor=None)

    _install_citing(monkeypatch, fake_fetch)

    result = runner.invoke(
        app,
        ["trace-citing", CITING_W_ID, "--limit", "10", "--resume-from", "cursor-9"],
    )
    assert result.exit_code == 0, result.output
    assert calls == [["openalex"]]
    assert "opencitations" in result.stderr
    assert "previous run" in result.stderr


def test_trace_citing_deduplicates_repeated_page_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-2: the same (source, message) fail-soft error prints once per run."""

    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        if resume_from is None:
            return _result(
                _paper("W1"),
                errors=(_failure(source="opencitations", message="no DOI skipping"),),
                resume_cursor="cursor-2",
            )
        return _result(
            _paper("W2"),
            errors=(_failure(source="opencitations", message="no DOI skipping"),),
        )

    _install_citing(monkeypatch, fake_fetch)

    result = runner.invoke(app, ["trace-citing", CITING_W_ID, "--limit", "10"])
    assert result.exit_code == 0, result.output
    assert result.stderr.count("no DOI skipping") == 1
