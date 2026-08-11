"""Command-line interface for Academic Intelligence."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import NoReturn, cast

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from academic_intelligence import AcademicIntelligence, __version__
from academic_intelligence.cli_author import author_app
from academic_intelligence.cli_budget import budget_cmd, build_sources_app
from academic_intelligence.cli_source import (
    _fulltext_storage,
    _run_cli,
    register_sources,
)
from academic_intelligence.cli_trace import register_trace
from academic_intelligence.cli_web import web_app
from academic_intelligence.core.models import Paper, normalize_doi
from academic_intelligence.core.types import Config
from academic_intelligence.exporters import ExportFormat, export_papers
from academic_intelligence.fulltext.locator import DEFAULT_SOURCES
from academic_intelligence.fulltext.parser import PDFParser
from academic_intelligence.fulltext.pipeline import FulltextPipeline
from academic_intelligence.fulltext.segmenter import Segmenter
from academic_intelligence.graph import KnowledgeGraph
from academic_intelligence.graph.traversal import ALL_RELATIONS
from academic_intelligence.sources.arxiv import _parse_arxiv_id

app = typer.Typer(
    name="paper",
    help="Academic Intelligence — multi-source academic data collection CLI",
    add_completion=False,
    no_args_is_help=True,
)
collect_app = typer.Typer(help="Collect academic data from sources")
app.add_typer(collect_app, name="collect")
source_app = typer.Typer(
    help="Run a source adapter operation directly (e.g. `source arxiv search <query>`)"
    " for arxiv, semantic_scholar, openalex, pubmed, ieee",
    no_args_is_help=True,
)
sources_app = build_sources_app()
register_sources(source_app)
app.add_typer(source_app, name="source")
app.add_typer(sources_app, name="sources")
app.add_typer(web_app, name="web")
app.add_typer(author_app, name="author")
register_trace(app)

console = Console()


def _version_callback(value: bool) -> None:
    """Print the package version and exit (FIX-T T7: ``paper --version``)."""
    if value:
        console.print(f"paper, version {__version__}")
        raise typer.Exit()


@app.callback()
def _root_callback(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Academic Intelligence — multi-source academic data collection CLI."""
    return None


def _parse_sources(sources: str | None) -> list[str] | None:
    if sources is None or sources.lower() in {"all", "*"}:
        return None
    return [s.strip() for s in sources.split(",") if s.strip()]


def _parse_year_range(year: str | None) -> tuple[int | None, int | None, int | None]:
    """Return (exact, year_from, year_to)."""
    if not year:
        return None, None, None
    year = year.strip()
    if re.fullmatch(r"\d{4}", year):
        y = int(year)
        return y, None, None
    m = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", year)
    if m:
        return None, int(m.group(1)), int(m.group(2))
    raise typer.BadParameter(f"Invalid year format: {year!r} (use YYYY or YYYY-YYYY)")


def _force_utf8_stdout() -> None:
    """Reconfigure stdout/stderr to UTF-8 (I-11 GBK fix).

    On Windows the console codepage (cp936) is inherited by rich; Chinese
    output then gets emitted as GBK bytes and breaks UTF-8 pipes.  ``main()``
    calls this before the app runs; environments without ``reconfigure``
    (e.g. some test runners) are skipped silently.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(AttributeError, OSError, ValueError):
            reconfigure(encoding="utf-8")


def _validate_sources_option(sources: str | None) -> None:
    """Reject unknown ``--sources`` names at the CLI layer (exit 2, no traceback)."""
    parsed = _parse_sources(sources)
    if not parsed:
        return
    from academic_intelligence import _SOURCE_ALIASES

    unknown = [s for s in parsed if s.lower() not in _SOURCE_ALIASES]
    if unknown:
        raise typer.BadParameter(
            f"unknown data source(s): {', '.join(unknown)}; "
            f"valid sources: {', '.join(sorted(_SOURCE_ALIASES))}"
        )


def _validate_relations_option(relations: str | None) -> None:
    """Reject unknown ``--relations`` names at the CLI layer (exit 2)."""
    if not relations:
        return
    parsed = [r.strip() for r in relations.split(",") if r.strip()]
    unknown = [r for r in parsed if r not in ALL_RELATIONS]
    if unknown:
        raise typer.BadParameter(
            f"unknown relation(s): {', '.join(unknown)}; "
            f"valid relations: references,citations,authors,papers,coauthors"
        )


def _build_config(
    storage_type: str,
    storage_path: str,
    sources: str | None,
) -> Config:
    _validate_sources_option(sources)
    cfg = Config(
        storage_type=storage_type,
        storage_path=storage_path,
    )
    parsed = _parse_sources(sources)
    if parsed is not None:
        cfg.sources = parsed
    return cfg


def _write_output(path: str | None, payload: object) -> None:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    if path:
        Path(path).write_text(text, encoding="utf-8")
        console.print(f"[green]Wrote[/green] {path}")
    else:
        console.print_json(text)


@collect_app.command("author")
def collect_author_cmd(
    name: str = typer.Argument(..., help="Author name"),
    sources: str | None = typer.Option(
        None,
        "--sources",
        "-s",
        help="Comma-separated sources (gs,ss,openalex) or 'all'",
    ),
    output: str | None = typer.Option(None, "--output", "-o", help="Output JSON path"),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
    persist: bool = typer.Option(False, "--persist", help="Save results to storage"),
) -> None:
    """Collect papers by author name."""

    async def _run() -> None:
        cfg = _build_config(storage_type, storage_path, sources)
        async with AcademicIntelligence(cfg) as ai:
            result = await ai.collect_author_papers(
                name,
                sources=_parse_sources(sources),
                persist=persist,
            )
            console.print(
                f"[bold]Found[/bold] {len(result.papers)} papers, "
                f"{len(result.authors)} authors"
            )
            if result.errors:
                for err in result.errors:
                    console.print(f"[yellow]warn[/yellow] {err}")
            if result.warnings:
                for warning in result.warnings:
                    console.print(f"[yellow]warning[/yellow] {warning}")
            if not result.papers:
                # (FIX-T F1 / T2) An empty result is the most common failure
                # mode for a newcomer; print an actionable hint instead of a
                # bare "Found 0 papers" + empty JSON dump.  Exit code stays 0
                # so scripts that treat non-zero as a hard failure are not
                # broken (see result.md for the trade-off).
                console.print(
                    "[yellow]No results[/yellow]: no papers found for "
                    f"author {name!r}. Check the spelling, or try a different "
                    "source (e.g. --sources openalex / arxiv / pubmed)."
                )
                return
            _write_output(output, result.to_dict())

    _run_cli(_run())


@collect_app.command("paper")
def collect_paper_cmd(
    query: str = typer.Argument(..., help="DOI or paper title"),
    sources: str | None = typer.Option(None, "--sources", "-s"),
    output: str | None = typer.Option(None, "--output", "-o"),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
    persist: bool = typer.Option(False, "--persist"),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Collect paper by DOI or title."""

    async def _run() -> None:
        cfg = _build_config(storage_type, storage_path, sources)
        async with AcademicIntelligence(cfg) as ai:
            result = await ai.collect_paper(
                query,
                sources=_parse_sources(sources),
                persist=persist,
                limit=limit,
            )
            console.print(f"[bold]Found[/bold] {len(result.papers)} papers")
            if result.errors:
                for err in result.errors:
                    console.print(f"[yellow]warn[/yellow] {err}")
            if result.warnings:
                for warning in result.warnings:
                    console.print(f"[yellow]warning[/yellow] {warning}")
            if not result.papers:
                # (FIX-T F1 / T2) Actionable empty-result hint; see the author
                # command for the exit-code trade-off (kept 0).
                console.print(
                    "[yellow]No results[/yellow]: no papers found for "
                    f"{query!r}. Check the spelling, try a different source "
                    "(--sources arxiv for arXiv IDs, --sources openalex for "
                    "DOIs), or verify the DOI format (e.g. 10.1038/nature14539)."
                )
                return
            _write_output(output, result.to_dict())

    _run_cli(_run())


@collect_app.command("citations")
def collect_citations_cmd(
    paper_id: str = typer.Argument(
        ...,
        help="Paper id to collect citations for (e.g. an OpenAlex W-id)",
    ),
    sources: str | None = typer.Option(
        None,
        "--sources",
        "-s",
        help="Comma-separated sources (gs,ss,openalex) or 'all'",
    ),
    output: str | None = typer.Option(None, "--output", "-o", help="Output JSON path"),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
    persist: bool = typer.Option(False, "--persist", help="Save results to storage"),
) -> None:
    """Collect citation relationships for a paper id (FIX-T F2 / T3).

    Mirrors the Python API ``collect_citations``: sources that expose citing
    works also return the full citing-paper records so they can be persisted.
    """

    async def _run() -> None:
        cfg = _build_config(storage_type, storage_path, sources)
        async with AcademicIntelligence(cfg) as ai:
            result = await ai.collect_citations(
                paper_id,
                sources=_parse_sources(sources),
                persist=persist,
            )
            console.print(
                f"[bold]Found[/bold] {len(result.citations)} citations, "
                f"{len(result.papers)} citing papers"
            )
            if result.errors:
                for err in result.errors:
                    console.print(f"[yellow]warn[/yellow] {err}")
            if result.warnings:
                for warning in result.warnings:
                    console.print(f"[yellow]warning[/yellow] {warning}")
            if not result.citations and not result.papers:
                console.print(
                    "[yellow]No results[/yellow]: no citations found for "
                    f"paper {paper_id!r}. Verify the paper id, or try a "
                    "different source."
                )
                return
            _write_output(output, result.to_dict())

    _run_cli(_run())


@app.command("paper")
def paper_cmd(
    query: str = typer.Argument(..., help="DOI, source identifier, or paper title"),
    sources: str | None = typer.Option(None, "--sources", "-s"),
    output: str | None = typer.Option(None, "--output", "-o"),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
    persist: bool = typer.Option(False, "--persist"),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Convenience alias for ``paper collect paper``."""
    collect_paper_cmd(
        query=query,
        sources=sources,
        output=output,
        storage_type=storage_type,
        storage_path=storage_path,
        persist=persist,
        limit=limit,
    )


@app.command("update")
def update_cmd(
    author: str = typer.Option(..., "--author", help="Author whose papers to refresh"),
    sources: str | None = typer.Option(None, "--sources", "-s"),
    output: str | None = typer.Option(None, "--output", "-o"),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
) -> None:
    """Incrementally refresh papers for an author."""

    async def _run() -> None:
        cfg = _build_config(storage_type, storage_path, sources)
        async with AcademicIntelligence(cfg) as ai:
            result = await ai.update_author_papers(
                author,
                sources=_parse_sources(sources),
            )
        console.print(
            f"[bold]Checked[/bold] {result.total_checked}: "
            f"{len(result.new)} new, {len(result.updated)} updated, "
            f"{len(result.unchanged)} unchanged"
        )
        for warning in result.warnings:
            console.print(f"[yellow]warning[/yellow] {warning}")
        if output:
            _write_output(output, result.to_dict())

    _run_cli(_run())


@app.command("query")
def query_cmd(
    entity: str = typer.Argument("papers", help="Entity type: papers"),
    author: str | None = typer.Option(None, "--author"),
    year: str | None = typer.Option(None, "--year", help="YYYY or YYYY-YYYY"),
    venue: str | None = typer.Option(None, "--venue"),
    keyword: str | None = typer.Option(None, "--keyword"),
    limit: int = typer.Option(10, "--limit", "-n"),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
    output: str | None = typer.Option(None, "--output", "-o"),
) -> None:
    """Query stored academic data."""
    if entity not in {"papers", "paper"}:
        raise typer.BadParameter("Only 'papers' entity is currently supported")

    exact, year_from, year_to = _parse_year_range(year)

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        async with AcademicIntelligence(cfg) as ai:
            papers = await ai.query_papers(
                author=author,
                year=exact,
                year_from=year_from,
                year_to=year_to,
                venue=venue,
                keyword=keyword,
                limit=limit,
            )
            if output:
                _write_output(output, [p.to_dict() for p in papers])
            else:
                table = Table(title=f"Papers ({len(papers)})")
                table.add_column("Year", style="cyan")
                table.add_column("Title")
                table.add_column("Authors")
                table.add_column("DOI")
                for p in papers:
                    table.add_row(
                        str(p.year or ""),
                        p.title[:80],
                        ", ".join(a.name for a in p.authors[:3]),
                        p.doi or "",
                    )
                console.print(table)

    _run_cli(_run())


@app.command("stats")
def stats_cmd(
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
) -> None:
    """Show storage statistics."""

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        async with AcademicIntelligence(cfg) as ai:
            stats = await ai.get_stats()
            table = Table(title="Storage Stats")
            table.add_column("Key")
            table.add_column("Value")
            for k, v in stats.items():
                table.add_row(str(k), str(v))
            console.print(table)

    _run_cli(_run())


@app.command("expand")
def expand_cmd(
    entity_id: str = typer.Argument(..., help="Paper or author id to expand"),
    relations: str | None = typer.Option(
        None,
        "--relations",
        "-r",
        help="Comma-separated relations: references,citations,authors,papers,coauthors",
    ),
    depth: int = typer.Option(1, "--depth", "-d", help="Expansion depth (default 1, max 3)"),
    sources: str | None = typer.Option(
        None,
        "--sources",
        "-s",
        help="Comma-separated sources (gs,ss,openalex) or 'all'",
    ),
    fetch_missing: bool = typer.Option(
        True,
        "--fetch-missing/--no-fetch-missing",
        help="Fetch storage misses from the data sources",
    ),
    output: str | None = typer.Option(None, "--output", "-o", help="Output JSON path"),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
) -> None:
    """Expand an entity's relationships in the session knowledge graph."""
    _validate_relations_option(relations)

    async def _run() -> None:
        cfg = _build_config(storage_type, storage_path, sources)
        parsed_relations = (
            [r.strip() for r in relations.split(",") if r.strip()]
            if relations
            else None
        )
        async with AcademicIntelligence(cfg) as ai:
            result = await ai.expand(
                entity_id,
                relations=parsed_relations,
                depth=depth,
                fetch_missing=fetch_missing,
                sources=_parse_sources(sources),
            )
            console.print(
                f"[bold]Expanded[/bold] {entity_id}: "
                f"{result.stats.nodes_found} nodes, {result.stats.edges_found} edges "
                f"(depth {result.stats.depth_reached}, cache hits {result.stats.cache_hits})"
            )
            if result.stats.truncated:
                console.print("[yellow]truncated[/yellow] depth / node limit reached")
            if result.stats.failed:
                console.print(f"[yellow]failed[/yellow] {result.stats.failed} relation(s)")
                for failure in result.stats.failures:
                    console.print(f"  [yellow]-[/yellow] {failure}")
            useful_result = bool(
                result.nodes
                or result.edges
                or result.stats.nodes_found
                or result.stats.edges_found
                or result.stats.cache_hits
                or result.stats.fetched_new
            )
            if result.stats.failed > 0 and not useful_result:
                raise typer.Exit(code=2)
            if output:
                ai.save_graph_snapshot(output)
                console.print(f"[green]Wrote graph snapshot[/green] {output}")
            else:
                _write_output(None, result.to_dict())

    _run_cli(_run())


@app.command("export")
def export_cmd(
    center: str = typer.Option(..., "--center", "-c", help="Center entity id"),
    radius: int = typer.Option(2, "--radius", "-r", help="Subgraph radius"),
    output: str | None = typer.Option(None, "--output", "-o", help="Output JSON path"),
    snapshot: str | None = typer.Option(
        None,
        "--snapshot",
        help="Versioned graph snapshot written by `paper expand --output`",
    ),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
) -> None:
    """Export a subgraph from a snapshot, falling back to the session graph."""

    async def _run() -> None:
        if snapshot:
            graph = KnowledgeGraph.load_snapshot(snapshot)
            subgraph = graph.to_subgraph(center, radius)
            sub = subgraph.export_json()
            sub["center"] = center
            sub["radius"] = radius
        else:
            cfg = Config(storage_type=storage_type, storage_path=storage_path)
            async with AcademicIntelligence(cfg) as ai:
                sub = await ai.subgraph(center, radius=radius)
        if sub["node_count"] == 0:
            raise ValueError(
                "graph is empty; run `paper expand <id> --output graph.json` and "
                "retry with `paper export --snapshot graph.json --center <id>`"
            )
        console.print(
            f"[bold]Subgraph[/bold] around {center}: "
            f"{sub['node_count']} nodes, {sub['edge_count']} edges"
        )
        _write_output(output, sub)

    _run_cli(_run())


@app.command("export-papers")
def export_papers_cmd(
    format: str = typer.Option(..., "--format", help="Export format: csv, jsonl, parquet"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    after: str | None = typer.Option(None, "--after", help="Resume after this paper id"),
    batch_size: int = typer.Option(500, "--batch-size", min=1),
    excel_safe: bool = typer.Option(
        False,
        "--excel-safe/--raw-csv",
        help="CSV only: add a UTF-8 BOM and neutralize formula-leading cells",
    ),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
) -> None:
    """Stream stored papers to CSV, JSONL, or optional Parquet."""
    raw_format = format.lower()
    if raw_format not in {"csv", "jsonl", "parquet"}:
        raise typer.BadParameter("format must be one of: csv, jsonl, parquet")
    if excel_safe and raw_format != "csv":
        raise typer.BadParameter("--excel-safe is only valid with --format csv")
    normalized_format = cast(ExportFormat, raw_format)

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        async with AcademicIntelligence(cfg) as ai:
            count = await export_papers(
                ai.storage,
                output,
                format=normalized_format,
                after=after,
                batch_size=batch_size,
                excel_safe=excel_safe,
            )
        console.print(f"[green]Exported[/green] {count} paper(s) to {output}")

    _run_cli(_run())


# ---------------------------------------------------------------------------
# Full-text pipeline CLI (upgrade technical-design.md §1.3 / §4.1)
# ---------------------------------------------------------------------------

#: DOI lookup order for the ``paper fulltext <id>`` M15 normalization step —
#: metadata-first, then the OA locator, then the aggregators.
_DOI_LOOKUP_ORDER: tuple[str, ...] = ("crossref", "arxiv", "unpaywall", "europe_pmc", "core")

#: Full-text sources the pipeline's locator understands (E4: Europe PMC's
#: own OA evidence is a legal locator source, ``paper fulltext --sources``
#: must accept it).
_FULLTEXT_SOURCES: tuple[str, ...] = ("unpaywall", "core", "arxiv", "europe_pmc")

logger = logging.getLogger(__name__)


async def _resolve_paper_for_fulltext(
    ai: AcademicIntelligence,
    identifier: str,
) -> Paper:
    """Resolve an input id to a Paper carrying doi/arxiv_id (M15).

    Order: storage by internal id → arXiv ID via the arxiv adapter → DOI via
    the metadata sources (first hit wins).  A paper without a DOI can still
    take the arXiv-only path; an input matching no identifier form is a real
    failure (exit 2), never a fake success.

    Raises:
        ValueError: When the input matches no identifier form or resolves to
            nothing.
    """
    paper = await ai.storage.get_paper(identifier)
    if paper is not None and (paper.doi or paper.arxiv_id):
        return paper
    doi = normalize_doi(identifier)
    arxiv_id = _parse_arxiv_id(identifier)
    if arxiv_id is None and doi is None:
        if paper is not None:
            return paper  # stored internal id without identifiers; pipeline reports the gap
        raise ValueError(
            f"无法识别的论文标识 {identifier!r}：请提供内部 id、arXiv ID 或 DOI"
        )
    if arxiv_id is not None:
        arxiv_sources = ai._resolve_sources(["arxiv"])
        if arxiv_sources:
            get_by_arxiv_id = getattr(arxiv_sources[0], "get_paper_by_arxiv_id", None)
            if get_by_arxiv_id is not None:
                try:
                    resolved: Paper | None = await get_by_arxiv_id(arxiv_id)
                except Exception as exc:
                    logger.warning("arXiv lookup failed for %s: %s", arxiv_id, exc)
                    resolved = None
                if resolved is not None:
                    return resolved
    if doi is not None:
        for source_name in _DOI_LOOKUP_ORDER:
            sources = ai._resolve_sources([source_name])
            if not sources:
                continue
            get_by_doi = getattr(sources[0], "get_paper_by_doi", None)
            if get_by_doi is None:
                continue
            try:
                resolved = await get_by_doi(doi)
            except Exception as exc:
                logger.warning("%s lookup failed for %s: %s", source_name, doi, exc)
                continue
            if resolved is not None:
                return resolved
    raise ValueError(f"未找到 {identifier!r} 对应的论文记录（无法解析全文所需标识）")


@app.command("fulltext")
def fulltext_cmd(
    identifier: str = typer.Argument(
        ..., help="Internal id, arXiv ID, or DOI of the paper"
    ),
    sources: str | None = typer.Option(
        None,
        "--sources",
        "-s",
        help="Comma-separated full-text sources (unpaywall,core,arxiv,europe_pmc); default all",
    ),
    persist: bool = typer.Option(False, "--persist", help="Save full text to storage"),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Write the FullText JSON to a file"
    ),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
) -> None:
    """Fetch legal OA full text: locate → download → parse → segment (M15)."""

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        parsed_sources: tuple[str, ...] = (
            tuple(s.strip() for s in sources.split(",") if s.strip())
            if sources
            else DEFAULT_SOURCES
        )
        unknown = [s for s in parsed_sources if s not in _FULLTEXT_SOURCES]
        if unknown:
            raise typer.BadParameter(
                f"unknown fulltext source(s): {', '.join(unknown)}; "
                f"valid sources: {', '.join(_FULLTEXT_SOURCES)}"
            )
        async with AcademicIntelligence(cfg) as ai:
            paper = await _resolve_paper_for_fulltext(ai, identifier)
            unpaywall_email = (
                cfg.unpaywall_email.get_secret_value()
                if cfg.unpaywall_email is not None
                else None
            )
            core_api_key = (
                cfg.core_api_key.get_secret_value()
                if cfg.core_api_key is not None
                else None
            )
            async with FulltextPipeline(
                storage=_fulltext_storage(ai.storage),
                unpaywall_email=unpaywall_email,
                core_api_key=core_api_key,
            ) as pipeline:
                fulltext = await pipeline.fetch(
                    paper, sources=parsed_sources, persist=persist
                )
        console.print(
            f"[bold]Full text[/bold] fetched for {identifier}: "
            f"{fulltext.paragraph_count} paragraphs, "
            f"{sum(len(segment.text) for segment in fulltext.segments)} chars "
            f"from {fulltext.source}"
        )
        if fulltext.file_path:
            console.print(f"  file: {fulltext.file_path}")
        if fulltext.oa_license:
            console.print(f"  license: {fulltext.oa_license}")
        if output:
            _write_output(output, fulltext.to_dict())

    _run_cli(_run())


pdf_app = typer.Typer(help="Local PDF tools")
app.add_typer(pdf_app, name="pdf")


@pdf_app.command("parse")
def pdf_parse_cmd(
    file: str = typer.Argument(..., help="PDF file to parse"),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write segments as JSONL (one paragraph per line)",
    ),
) -> None:
    """Parse a local PDF into text segments (pages → paragraphs)."""
    file_path = Path(file)
    if not file_path.is_file():
        raise typer.BadParameter(f"not a file: {file}")
    parser = PDFParser()
    try:
        pages = parser.parse(file_path)
    except Exception as exc:
        console.print(f"[bold red]Error[/bold red]: {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    segments = Segmenter().segment(pages)
    chars = sum(len(segment.text) for segment in segments)
    console.print(
        f"[bold]Parsed[/bold] {file_path.name}: {len(pages)} pages, "
        f"{len(segments)} paragraphs, {chars} chars"
    )
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            for segment in segments:
                fh.write(json.dumps(segment.to_dict(), ensure_ascii=False) + "\n")
        console.print(f"[green]Wrote[/green] {output}")


app.command("budget", help="Show per-source budget quotas (all-source overview)")(
    budget_cmd
)


def ai_legacy_shim() -> NoReturn:
    """Legacy ``ai`` console-script entry (renamed to ``paper``; design §8 migration).

    Any ``ai`` invocation prints the rename notice and exits 2 so existing
    scripts fail loudly instead of silently running the old command name.
    """
    _force_utf8_stdout()
    sys.stderr.write("Command 'ai' was renamed to 'paper'")
    raise SystemExit(2)


def main() -> None:
    """CLI entry point for console_scripts."""
    _force_utf8_stdout()
    app()


if __name__ == "__main__":
    main()
