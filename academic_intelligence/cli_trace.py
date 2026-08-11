"""Trace-chain CLI (Task 4): ``paper trace-citing|trace-authors|trace-profiles``.

Wraps the three frozen trace primitives
(:mod:`academic_intelligence.trace.citing` / ``.authors`` / ``.profiles``)
into chained ``paper`` commands that exchange CSV files (UTF-8 + BOM,
Excel-compatible — same conventions as ``paper export-papers --excel-safe``):

- ``paper trace-citing <paper-id> [--sources ..] [--limit N] [--resume-from C] [--output citing.csv]``
  — columns ``citing_paper_id,doi,title,year,venue,authors_raw,authors_detail``
  (``authors_raw`` semicolon-joined; ``authors_detail`` compact JSON of the
  OpenAlex ``authorships`` entries, so the chained workflow keeps
  affiliation/author-id detail across the CSV boundary);
- ``paper trace-authors <citing.csv|paper-id> [--affiliation-filter KW] [--output authors.csv]``
  — columns ``author_name,appears_in,affiliation,author_id`` (``appears_in``
  semicolon-joined); input is either a ``trace-citing`` CSV (read back into
  ``CitingPaper`` records) or a raw paper id (fetched internally first);
- ``paper trace-profiles <authors.csv> [--batch-size N] [--output profiles.csv]``
  — columns ``author_name,author_id,institution,h_index,fields,works_count,top_works``
  (``fields`` semicolon-joined, ``top_works`` compact JSON).

Exit-code contract (dispatch Task 4): a run that produced *no* data because
every source failed exits 2; a partial run (some sources/authors failed but
data remains) prints a ``PARTIAL`` notice and keeps exit 0; an empty-but-clean
result (a paper with no citations) keeps exit 0 with a hint, mirroring the
collect commands' F1/T2 convention.

All human-facing progress/warnings go to **stderr** so the CSV text on stdout
stays pipe-safe when ``--output`` is omitted.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from rich.console import Console

from academic_intelligence.cli_source import _run_cli
from academic_intelligence.core.exceptions import SourceFailure
from academic_intelligence.trace.authors import (
    CitingPaper as AuthorsCitingPaper,
)
from academic_intelligence.trace.authors import (
    flatten_authors,
)
from academic_intelligence.trace.citing import (
    _OPENALEX_PAGE_SIZE,
    CitingPaper,
    CitingResult,
    fetch_citing_papers,
)
from academic_intelligence.trace.profiles import (
    AuthorRow as ProfileRow,
)
from academic_intelligence.trace.profiles import (
    fetch_profiles,
)

#: Status/progress console — always stderr so CSV-to-stdout stays pipe-safe.
msg_console = Console(stderr=True, highlight=False)

#: Source names the trace primitives know how to drive (``fetch_citing_papers``
#: rejects anything else fail-soft; the CLI validates them upfront instead).
_TRACE_SOURCES: tuple[str, ...] = ("openalex", "opencitations")

_CITING_COLUMNS: tuple[str, ...] = (
    "citing_paper_id",
    "doi",
    "title",
    "year",
    "venue",
    "authors_raw",
    "authors_detail",
)
_AUTHOR_COLUMNS: tuple[str, ...] = (
    "author_name",
    "appears_in",
    "affiliation",
    "author_id",
)
_PROFILE_COLUMNS: tuple[str, ...] = (
    "author_name",
    "author_id",
    "institution",
    "h_index",
    "fields",
    "works_count",
    "top_works",
)

#: Cells starting with these characters are formula-injection hazards in Excel;
#: mirror of ``academic_intelligence.exporters._FORMULA_PREFIXES``.
_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


# ---------------------------------------------------------------------------
# CSV helpers (UTF-8 + BOM on files, Excel-safe cells, like export-papers
# --excel-safe)
# ---------------------------------------------------------------------------


def _csv_cell(value: Any) -> Any:
    """Coerce one CSV cell: None → "", list/dict → compact JSON, formula-safe."""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _render_csv(fieldnames: Sequence[str], rows: list[dict[str, Any]]) -> str:
    """Render rows as CSV text (no BOM — the caller adds it for files)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _csv_cell(row.get(field)) for field in fieldnames})
    return buffer.getvalue()


def _write_csv(
    output: str | None,
    fieldnames: Sequence[str],
    rows: list[dict[str, Any]],
) -> None:
    """Write *rows* to *output* (UTF-8 + BOM) or print the CSV text to stdout."""
    text = _render_csv(fieldnames, rows)
    if output:
        with open(output, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write(text)
        msg_console.print(f"[green]Wrote[/green] {output} ({len(rows)} row(s))")
    else:
        sys.stdout.write(text)


def _read_csv_rows(
    path: Path,
    columns: Sequence[str],
    required: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """Read a CSV into clean ``{column: stripped-value}`` records.

    ``utf-8-sig`` tolerates the BOM written by :func:`_write_csv`.  Rows where
    every expected column is blank are dropped; a missing column is a usage
    error (exit 2).  ``required`` (default: all of *columns*) names the
    columns whose absence is fatal — columns listed in *columns* but not in
    ``required`` are optional (missing cells read as ``""``), which lets the
    citing CSV keep ``authors_detail`` optional for backward compatibility.
    """
    need = list(required) if required is not None else list(columns)
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise typer.BadParameter(f"empty CSV file: {path}")
        missing = [c for c in need if c not in reader.fieldnames]
        if missing:
            raise typer.BadParameter(
                f"CSV {path} is missing column(s): {', '.join(missing)}; "
                f"expected columns: {', '.join(need)}"
            )
        records: list[dict[str, str]] = []
        for row in reader:
            record = {c: (row.get(c) or "").strip() for c in columns}
            if any(record.values()):
                records.append(record)
        return records


def _parse_optional_int(value: str) -> int | None:
    """Parse a CSV cell as int, tolerating blank/garbage cells (→ None)."""
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _split_semicolon(value: str) -> list[str]:
    """Split a semicolon-joined CSV cell into non-empty stripped parts."""
    return [part for part in (p.strip() for p in value.split(";")) if part]


def _parse_authors_detail(cell: str) -> list[dict[str, Any]]:
    """Parse the compact-JSON ``authors_detail`` cell back into entries.

    Blank or malformed cells yield ``[]`` so hand-edited or pre-``authors_detail``
    CSVs still flatten (falling back to ``authors_raw``), keeping the column
    backward-compatible.
    """
    if not cell.strip():
        return []
    try:
        parsed = json.loads(cell)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


def _read_citing_csv(path: Path) -> list[CitingPaper]:
    """Read a ``trace-citing`` CSV back into :class:`CitingPaper` records.

    The ``authors_detail`` column round-trips the OpenAlex ``authorships``
    entries (compact JSON, as written by ``trace-citing``), so the chained
    workflow keeps affiliation / author-id detail through the file boundary
    and ``trace-authors`` / ``trace-profiles`` are not all-placeholder (I-1).
    The column is optional on read: a CSV written before it existed (or with
    blank cells) reconstructs papers with empty detail, and flattening falls
    back to raw author names.
    """
    columns = _CITING_COLUMNS
    required = [c for c in _CITING_COLUMNS if c != "authors_detail"]
    papers: list[CitingPaper] = []
    for row in _read_csv_rows(path, columns, required=required):
        if not row["citing_paper_id"]:
            continue
        papers.append(
            CitingPaper(
                citing_paper_id=row["citing_paper_id"],
                doi=row["doi"] or None,
                title=row["title"] or None,
                year=_parse_optional_int(row["year"]),
                venue=row["venue"] or None,
                authors_raw=_split_semicolon(row["authors_raw"]),
                authors_detail=_parse_authors_detail(row.get("authors_detail") or ""),
            )
        )
    return papers


def _read_authors_csv(path: Path) -> list[ProfileRow]:
    """Read a ``trace-authors`` CSV back into :class:`ProfileRow` records."""
    rows: list[ProfileRow] = []
    for row in _read_csv_rows(path, _AUTHOR_COLUMNS):
        if not row["author_name"]:
            continue
        rows.append(
            ProfileRow(
                author_name=row["author_name"],
                appears_in=_split_semicolon(row["appears_in"]),
                affiliation=row["affiliation"] or None,
                author_id=row["author_id"] or None,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Shared fetching (per-page progress via the library's documented resume loop)
# ---------------------------------------------------------------------------


async def _fetch_citing_all(
    paper_id: str,
    *,
    sources: list[str] | None,
    limit: int | None,
    resume_from: str | None,
) -> CitingResult:
    """Fetch citing papers in page-sized chunks, printing per-page progress.

    The frozen primitive exposes no pagination callback, so the CLI drives it
    through the documented resume contract: each call asks for at most one
    OpenAlex page (``_OPENALEX_PAGE_SIZE``) and continues with the returned
    ``resume_cursor``, deduplicating across calls (dedup key: ``doi`` when
    present, else ``citing_paper_id``).  The caller-supplied ``--limit`` still
    caps the accumulated total.  Fail-soft errors are deduplicated across
    pages (M-2).

    OpenCitations is a single unpaginated response: it is pulled by the first
    chunk and then dropped from later chunks (M-4), so resume pages and large
    pulls do not re-download every COCI edge; when the run itself is a resume
    (``--resume-from``), OpenCitations was already pulled by the truncated
    run and is skipped entirely.
    """
    collected: dict[str, CitingPaper] = {}
    errors: list[SourceFailure] = []
    seen_errors: set[tuple[str, str]] = set()
    source_stats: dict[str, int] = {}
    written_stats: dict[str, int] = {}
    # Which sources the caller asked for, expanded to the explicit list so a
    # later chunk can drop OpenCitations without re-checking ``None``.
    requested = list(sources) if sources is not None else list(_TRACE_SOURCES)
    skip_opencitations = "opencitations" in requested and resume_from is not None
    cursor = resume_from
    resume_cursor: str | None = None
    page = 0
    while True:
        remaining = None if limit is None else max(0, limit - len(collected))
        if remaining == 0:
            break
        chunk = _OPENALEX_PAGE_SIZE if remaining is None else min(remaining, _OPENALEX_PAGE_SIZE)
        chunk_sources = requested
        if page > 0 or skip_opencitations:
            chunk_sources = [s for s in requested if s != "opencitations"]
        result = await fetch_citing_papers(
            paper_id, sources=chunk_sources, limit=chunk, resume_from=cursor
        )
        page += 1
        for paper in result.papers:
            key = paper.doi if paper.doi else paper.citing_paper_id
            existing = collected.get(key)
            # Prefer the richer record on a cross-chunk collision (an
            # OpenAlex row with title over a bare COCI DOI row).
            if existing is None or (paper.title and not existing.title):
                collected[key] = paper
        for error in result.errors:
            error_key = (error.source, error.message)
            if error_key not in seen_errors:
                seen_errors.add(error_key)
                errors.append(error)
        for name, count in result.source_stats.items():
            source_stats[name] = source_stats.get(name, 0) + count
        for name, count in result.written_stats.items():
            written_stats[name] = written_stats.get(name, 0) + count
        msg_console.print(
            f"[dim]page {page}[/dim]: {len(result.papers)} new, {len(collected)} total"
        )
        resume_cursor = result.resume_cursor
        if result.resume_cursor is None:
            break
        cursor = result.resume_cursor
    if skip_opencitations and "opencitations" in requested:
        msg_console.print(
            "[dim]opencitations: already pulled by the previous run; "
            "not re-fetching on --resume-from[/dim]"
        )
    return CitingResult(
        papers=list(collected.values()),
        resume_cursor=resume_cursor,
        source_stats=source_stats,
        written_stats=written_stats,
        errors=errors,
    )


def _format_source_stats(result: CitingResult) -> str:
    """Render per-source pull/write counts (M-3: "N available, M written").

    ``source_stats`` counts what each source pulled (pre-truncation);
    ``written_stats`` counts how many of the output rows each source
    contributed.  The two differ only when ``--limit`` truncated the merged
    result; the "available, written" wording makes that explicit instead of
    implying every pulled paper was output.
    """
    order = list(result.source_stats.keys())
    for name in result.written_stats:
        if name not in order:
            order.append(name)
    parts: list[str] = []
    for name in order:
        available = result.source_stats.get(name, 0)
        written = result.written_stats.get(name, 0)
        if written == available:
            parts.append(f"{name} {available}")
        else:
            parts.append(f"{name} {available} available, {written} written")
    return ", ".join(parts)


def _print_failures(errors: list[SourceFailure]) -> None:
    """Render fail-soft source errors as one-line warnings (stderr)."""
    for error in errors:
        status = "transient" if error.transient else "permanent"
        msg_console.print(f"[yellow]warn[/yellow] [{error.source}] {error.message} ({status})")


def _parse_trace_sources(sources: str | None) -> list[str] | None:
    """Parse ``--sources``; ``None``/``all`` → library default, unknown → exit 2."""
    if sources is None or sources.lower() in {"all", "*"}:
        return None
    parsed = [s.strip() for s in sources.split(",") if s.strip()]
    unknown = [s for s in parsed if s not in _TRACE_SOURCES]
    if unknown:
        raise typer.BadParameter(
            f"unknown trace source(s): {', '.join(unknown)}; "
            f"valid sources: {', '.join(_TRACE_SOURCES)}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def trace_citing_cmd(
    paper_id: Annotated[
        str,
        typer.Argument(help="OpenAlex W-id (W…), DOI (10.…), or arXiv id of the cited paper"),
    ],
    sources: Annotated[
        str | None,
        typer.Option(
            "--sources",
            help="Comma-separated trace sources: openalex,opencitations (default: both)",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Stop after N deduplicated citing papers (default: fetch all)",
        ),
    ] = None,
    resume_from: Annotated[
        str | None,
        typer.Option(
            "--resume-from",
            help="OpenAlex cursor printed by a previous truncated run",
        ),
    ] = None,
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help="Write UTF-8-BOM CSV to a file (default: print CSV to stdout)",
        ),
    ] = None,
) -> None:
    """Fetch papers that cite <paper-id> and write them as CSV (trace step 1)."""

    async def _run() -> None:
        parsed_sources = _parse_trace_sources(sources)
        result = await _fetch_citing_all(
            paper_id,
            sources=parsed_sources,
            limit=limit,
            resume_from=resume_from,
        )
        _print_failures(result.errors)
        if not result.papers:
            if result.errors:
                msg_console.print(
                    "[bold red]Error[/bold red]: every source failed; no citing papers fetched"
                )
                raise typer.Exit(code=2)
            msg_console.print(
                f"[yellow]No results[/yellow]: no citing papers found for "
                f"{paper_id!r} (check the id, or widen --sources)"
            )
            _write_csv(output, _CITING_COLUMNS, [])
            return
        if result.errors:
            msg_console.print(
                f"[yellow]PARTIAL[/yellow]: {len(result.errors)} source(s) "
                "failed; the CSV below is from the remaining source(s)"
            )
        if result.resume_cursor is not None:
            msg_console.print(
                "[yellow]truncated[/yellow]: run again with "
                f"--resume-from {result.resume_cursor} to continue"
            )
        stats_text = _format_source_stats(result)
        msg_console.print(
            f"[bold]Found[/bold] {len(result.papers)} citing paper(s)"
            + (f" ({stats_text})" if stats_text else "")
        )
        rows = [
            {
                "citing_paper_id": paper.citing_paper_id,
                "doi": paper.doi,
                "title": paper.title,
                "year": paper.year,
                "venue": paper.venue,
                "authors_raw": ";".join(paper.authors_raw),
                "authors_detail": paper.authors_detail,
            }
            for paper in result.papers
        ]
        _write_csv(output, _CITING_COLUMNS, rows)

    _run_cli(_run())


def trace_authors_cmd(
    input_arg: Annotated[
        str,
        typer.Argument(
            help="Citing-papers CSV from `paper trace-citing`, or a paper id "
            "(W-id / DOI / arXiv id to fetch first)"
        ),
    ],
    affiliation_filter: Annotated[
        str | None,
        typer.Option(
            "--affiliation-filter",
            help="Keep only author occurrences whose affiliation contains KW (exact substring)",
        ),
    ] = None,
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help="Write UTF-8-BOM CSV to a file (default: print CSV to stdout)",
        ),
    ] = None,
) -> None:
    """Flatten citing-paper authors into one row per author (trace step 2)."""

    async def _run() -> None:
        input_path = Path(input_arg)
        if input_path.is_file():
            papers = _read_citing_csv(input_path)
        else:
            result = await _fetch_citing_all(input_arg, sources=None, limit=None, resume_from=None)
            _print_failures(result.errors)
            papers = result.papers
            if not papers and result.errors:
                msg_console.print(
                    "[bold red]Error[/bold red]: every source failed; no citing papers to flatten"
                )
                raise typer.Exit(code=2)
            if papers and result.errors:
                msg_console.print(
                    "[yellow]PARTIAL[/yellow]: some source(s) failed; "
                    "flattening the partial citing set"
                )
        if not papers:
            msg_console.print(
                f"[yellow]No input[/yellow]: no citing papers to flatten from {input_arg!r}"
            )
            raise typer.Exit(code=2)
        # authors.CitingPaper is a Protocol that citing.CitingPaper satisfies
        # structurally, but ``list`` is invariant — cast across the frozen
        # library boundary.
        rows = flatten_authors(
            cast(list[AuthorsCitingPaper], papers),
            affiliation_filter=affiliation_filter,
        )
        if not rows:
            msg_console.print(
                f"[yellow]No authors[/yellow]: no author rows produced from "
                f"{input_arg!r}"
                + (f" with affiliation filter {affiliation_filter!r}" if affiliation_filter else "")
            )
            raise typer.Exit(code=2)
        csv_rows = [
            {
                "author_name": row.author_name,
                "appears_in": ";".join(row.appears_in),
                "affiliation": row.affiliation,
                "author_id": row.author_id,
            }
            for row in rows
        ]
        _write_csv(output, _AUTHOR_COLUMNS, csv_rows)

    _run_cli(_run())


def trace_profiles_cmd(
    authors_csv: Annotated[
        str,
        typer.Argument(help="Authors CSV from `paper trace-authors`"),
    ],
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            min=1,
            help="Number of OpenAlex profile fetches in flight per batch",
        ),
    ] = 20,
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help="Write UTF-8-BOM CSV to a file (default: print CSV to stdout)",
        ),
    ] = None,
) -> None:
    """Enrich author rows with OpenAlex profiles (trace step 3)."""

    async def _run() -> None:
        path = Path(authors_csv)
        if not path.is_file():
            raise typer.BadParameter(f"not a file: {authors_csv}")
        rows = _read_authors_csv(path)
        if not rows:
            msg_console.print(f"[yellow]No input[/yellow]: no author rows in {path}")
            raise typer.Exit(code=2)
        profiles = await fetch_profiles(rows, batch_size=batch_size)
        failed = [profile for profile in profiles if profile.errors]
        for profile in failed:
            for error in profile.errors:
                msg_console.print(f"[yellow]warn[/yellow] [{profile.author_name}] {error}")
        if failed and len(failed) == len(profiles):
            msg_console.print("[bold red]Error[/bold red]: every author profile fetch failed")
            raise typer.Exit(code=2)
        if failed:
            msg_console.print(
                f"[yellow]PARTIAL[/yellow]: {len(failed)}/{len(profiles)} "
                "author profile(s) failed; the CSV below is the partial result"
            )
        csv_rows = [
            {
                "author_name": profile.author_name,
                "author_id": profile.author_id,
                "institution": profile.institution,
                "h_index": profile.h_index,
                "fields": ";".join(profile.fields),
                "works_count": profile.works_count,
                "top_works": profile.top_works,
            }
            for profile in profiles
        ]
        _write_csv(output, _PROFILE_COLUMNS, csv_rows)

    _run_cli(_run())


def register_trace(app: typer.Typer) -> None:
    """Mount the three trace-chain commands onto the ``paper`` app."""
    app.command(
        "trace-citing",
        help="Fetch papers that cite a paper, as CSV (trace chain step 1)",
    )(trace_citing_cmd)
    app.command(
        "trace-authors",
        help="Flatten citing-paper authors into one row per author (trace chain step 2)",
    )(trace_authors_cmd)
    app.command(
        "trace-profiles",
        help="Enrich author rows with OpenAlex profiles (trace chain step 3)",
    )(trace_profiles_cmd)
