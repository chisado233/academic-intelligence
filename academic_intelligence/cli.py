"""Command-line interface for Academic Intelligence."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.types import Config

app = typer.Typer(
    name="ai",
    help="Academic Intelligence — multi-source academic data collection CLI",
    add_completion=False,
    no_args_is_help=True,
)
collect_app = typer.Typer(help="Collect academic data from sources")
app.add_typer(collect_app, name="collect")

console = Console()


def _parse_sources(sources: Optional[str]) -> Optional[List[str]]:
    if sources is None or sources.lower() in {"all", "*"}:
        return None
    return [s.strip() for s in sources.split(",") if s.strip()]


def _parse_year_range(year: Optional[str]) -> tuple[Optional[int], Optional[int], Optional[int]]:
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


def _build_config(
    storage_type: str,
    storage_path: str,
    sources: Optional[str],
) -> Config:
    cfg = Config(
        storage_type=storage_type,
        storage_path=storage_path,
    )
    parsed = _parse_sources(sources)
    if parsed is not None:
        cfg.sources = parsed
    return cfg


def _write_output(path: Optional[str], payload: object) -> None:
    text = json.dumps(
        payload if isinstance(payload, dict) else payload,  # type: ignore[arg-type]
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
    sources: Optional[str] = typer.Option(
        None,
        "--sources",
        "-s",
        help="Comma-separated sources (gs,ss,openalex) or 'all'",
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output JSON path"),
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
            _write_output(output, result.to_dict())

    asyncio.run(_run())


@collect_app.command("paper")
def collect_paper_cmd(
    query: str = typer.Argument(..., help="DOI or paper title"),
    sources: Optional[str] = typer.Option(None, "--sources", "-s"),
    output: Optional[str] = typer.Option(None, "--output", "-o"),
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
            _write_output(output, result.to_dict())

    asyncio.run(_run())


@app.command("query")
def query_cmd(
    entity: str = typer.Argument("papers", help="Entity type: papers"),
    author: Optional[str] = typer.Option(None, "--author"),
    year: Optional[str] = typer.Option(None, "--year", help="YYYY or YYYY-YYYY"),
    venue: Optional[str] = typer.Option(None, "--venue"),
    keyword: Optional[str] = typer.Option(None, "--keyword"),
    limit: int = typer.Option(10, "--limit", "-n"),
    storage_type: str = typer.Option("sqlite", "--storage-type"),
    storage_path: str = typer.Option("./academic_intelligence.db", "--storage-path"),
    output: Optional[str] = typer.Option(None, "--output", "-o"),
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
                        ", ".join(p.authors[:3]),
                        p.doi or "",
                    )
                console.print(table)

    asyncio.run(_run())


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

    asyncio.run(_run())


def main() -> None:
    """CLI entry point for console_scripts."""
    app()


if __name__ == "__main__":
    main()
