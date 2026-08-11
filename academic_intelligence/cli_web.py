"""Web crawl CLI (WP3): ``paper web crawl <url> [--extract schema.json]``.

Wraps :class:`~academic_intelligence.webcrawler.crawler.WebCrawler` for the
``paper web crawl`` command (upgrade technical-design.md §4 / §7).  The
crawler runs the full §1.2 pipeline — robots pre-check, anti-crawl
interception (blocked, never escalated), content extraction and optional
schema extraction — and the CLI reports the outcome.  ``blocked`` / ``failed``
crawls exit 2 (a crawl that was denied is not a success, D6).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from academic_intelligence.cli_source import _run_cli
from academic_intelligence.webcrawler.crawler import WebCrawler
from academic_intelligence.webcrawler.models import CrawlStatus, SchemaField, SchemaSpec

console = Console()

web_app = typer.Typer(help="Web crawling tools")


def _load_schema(path: str) -> SchemaSpec:
    """Load a rule-mode extraction schema from a JSON file.

    Accepted shapes:
    - a ``SchemaSpec`` object (``{"fields": [...], "llm": false}``);
    - a bare list of field objects;
    - a convenience mapping ``{"field": "selector"}``.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"cannot load schema {path!r}: {exc}") from exc
    if isinstance(data, dict) and "fields" in data:
        return SchemaSpec.model_validate(data)
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        return SchemaSpec(fields=[SchemaField.model_validate(item) for item in data])
    if isinstance(data, dict) and all(isinstance(value, str) for value in data.values()):
        # {"title": "h1.title"} convenience form -> one CSS field per key.
        return SchemaSpec(
            fields=[
                SchemaField(field=key, selector=value) for key, value in data.items()
            ]
        )
    raise typer.BadParameter(
        f"schema {path!r} must be a SchemaSpec object, a list of fields, or a "
        "{field: selector} mapping"
    )


@web_app.command("crawl")
def web_crawl_cmd(
    url: Annotated[str, typer.Argument(help="Absolute HTTP(S) URL to crawl")],
    extract: Annotated[
        str | None,
        typer.Option(
            "--extract",
            "-e",
            help="JSON schema file for structured extraction (rules mode)",
        ),
    ] = None,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Write the result JSON to a file"),
    ] = None,
) -> None:
    """Crawl a public page (robots pre-check; blocked/failed exit 2)."""
    schema = _load_schema(extract) if extract else None

    async def _run() -> None:
        async with WebCrawler() as crawler:
            document = await crawler.crawl(url, schema=schema)
        if document.status == CrawlStatus.BLOCKED:
            console.print(f"[bold red]Blocked[/bold red] {url}")
            console.print(
                f"[red]reason:[/red] {document.diagnostic or 'unknown'}"
            )
            raise typer.Exit(code=2)
        if document.status == CrawlStatus.FAILED:
            console.print(f"[bold red]Failed[/bold red] {url}")
            console.print(
                f"[red]reason:[/red] {document.diagnostic or 'unknown'}"
            )
            raise typer.Exit(code=2)
        console.print(f"[bold]Crawled[/bold] {url}")
        console.print(f"  title: {document.title or '(none)'}")
        console.print(f"  content chars: {len(document.content)}")
        console.print(f"  links: {len(document.links)}")
        if document.extracted:
            console.print("  extracted:")
            console.print_json(
                json.dumps(document.extracted, ensure_ascii=False, indent=2)
            )
        if output:
            Path(output).write_text(
                json.dumps(
                    document.to_dict(), ensure_ascii=False, indent=2, default=str
                ),
                encoding="utf-8",
            )
            console.print(f"[green]Wrote[/green] {output}")

    _run_cli(_run())
