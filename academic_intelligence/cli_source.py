"""Source subcommand tree: ``paper source <source> <operation>``.

Each source adapter declares the operations it supports (``search`` /
``get`` / ``citations`` / ``fulltext``) through its ``capabilities``
ClassVar.  :func:`register_source` mounts one command per adapter onto a
``source`` group; the generated command validates the requested operation
against the adapter's declaration at runtime and rejects undeclared
operations with an explicit ``"<source> 不支持 <operation>"`` error
(fail-closed, aligned with the existing arXiv ``get_citations`` behaviour).

Operation -> adapter method mapping (technical-design.md §1.1.1 C1)::

    CLI operation   adapter method            capability key
    --------------  ------------------------  ---------------
    search          search_papers(query)      search
    get             get_paper_by_doi(doi)     get
                    get_paper_by_arxiv_id(id) (arXiv IDs)
                    get_paper_by_id(id)       (source-specific ids)
    citations       get_citations(paper_id)   citations
    fulltext        get_fulltext(paper)       fulltext

``get`` dispatches on the input's identifier shape (CR-1): a DOI
(``10.`` / doi.org) goes to ``get_paper_by_doi``, an arXiv ID goes to
``get_paper_by_arxiv_id`` when the adapter exposes it, and any other input
falls back to a source-specific ``get_paper_by_id`` when present.  An input
matching no supported form is a real failure (exit 2), never a fake
"not found" success.  ``fulltext`` first resolves its argument to a
:class:`Paper` through the same routing, because the adapters'
``get_fulltext`` signatures take a ``Paper`` (CR-2).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import click
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.exceptions import (
    AcademicIntelligenceError,
    CollectorError,
    SourceError,
    StorageError,
)
from academic_intelligence.core.models import Citation, Paper, normalize_doi
from academic_intelligence.core.types import Config
from academic_intelligence.fulltext.locator import DEFAULT_SOURCES
from academic_intelligence.fulltext.models import FullText
from academic_intelligence.fulltext.pipeline import FulltextPipeline
from academic_intelligence.sources.arxiv import ArxivSource, _parse_arxiv_id
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.core_ import CoreSource
from academic_intelligence.sources.crossref import CrossrefSource
from academic_intelligence.sources.europe_pmc import EuropePmcSource
from academic_intelligence.sources.ieee import IEEESource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.sources.opencitations import OpenCitationsSource
from academic_intelligence.sources.pubmed import PubMedSource
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource
from academic_intelligence.sources.unpaywall import UnpaywallSource

if TYPE_CHECKING:
    # TYPE_CHECKING only (same pattern as fulltext/pipeline.py): the runtime
    # annotation is a string thanks to ``from __future__ import annotations``,
    # so importing SQLiteStorage here would be a pure startup-cost cycle risk.
    from academic_intelligence.storage.sqlite_store import SQLiteStorage

console = Console()

#: CLI operation -> adapter method mapping (technical-design.md §1.1.1 C1).
OPERATION_METHODS: dict[str, str] = {
    "search": "search_papers",
    "get": "get_paper_by_doi",
    "citations": "get_citations",
    "fulltext": "get_fulltext",
}

#: Adapters registered by default.  Google Scholar stays out of the source
#: tree (design M14: ``enable_google_scholar=False`` default — the adapter is
#: preserved for historical evidence but not exposed as a ``paper source``
#: command).  New adapters mount themselves by calling :func:`register_source`
#: with an instance of the ``source`` group.
_DEFAULT_SOURCE_CLASSES: tuple[type[BaseSource], ...] = (
    SemanticScholarSource,
    OpenAlexSource,
    ArxivSource,
    PubMedSource,
    IEEESource,
    # --- crawler upgrade 2026-08: new free sources ---
    CrossrefSource,
    UnpaywallSource,
    EuropePmcSource,
    OpenCitationsSource,
    CoreSource,
)

#: Friendly CLI command aliases per canonical source name (E1).  The
#: underscore name stays the registry key; hyphenated / short forms are
#: mounted as additional ``paper source <alias>`` commands so the documented
#: spellings (``europe-pmc`` / ``epmc``) work next to ``europe_pmc``.
_SOURCE_COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "europe_pmc": ("europe-pmc", "epmc"),
    "opencitations": ("coci",),
}


# ---------------------------------------------------------------------------
# CLI error plumbing (shared with cli.py)
# ---------------------------------------------------------------------------


def _map_cli_error(exc: Exception) -> str:
    """Map a known exception to a one-line user-facing message (I-11)."""
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(part) for part in first.get("loc", ()))
            msg = str(first.get("msg", "")).strip()
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            where = f" for {loc}" if loc else ""
            return f"invalid value{where}: {msg}"
        return str(exc)
    if isinstance(exc, CollectorError):
        message = str(exc)
        if "No data sources registered" in message or not message.strip():
            return "unknown or invalid data source: no data sources registered"
        return message
    if isinstance(exc, ValueError):
        # e.g. unknown relation raised by the traversal layer
        return str(exc)
    if isinstance(exc, StorageError):
        return f"storage error: {exc}"
    if isinstance(exc, SourceError):
        return f"source error ({exc.source_name}): {exc.message}"
    if isinstance(exc, AcademicIntelligenceError):
        return exc.message
    return str(exc) or exc.__class__.__name__


def _run_cli(coro: Coroutine[Any, Any, None]) -> None:
    """Run an async command, mapping known errors to friendly exit-2 errors."""
    try:
        asyncio.run(coro)
    except typer.Exit:
        raise
    except click.ClickException:
        raise  # click renders its own "Error: ..." with exit code 2
    except Exception as exc:  # CLI boundary maps every unexpected failure
        console.print(f"[bold red]Error[/bold red]: {escape(_map_cli_error(exc))}")
        raise typer.Exit(code=2) from exc


# ---------------------------------------------------------------------------
# Capability-driven registration
# ---------------------------------------------------------------------------


def _source_supports(source: BaseSource, operation: str) -> bool:
    """Return whether *source* declares support for the CLI *operation*.

    Capabilities are checked under both key conventions so legacy adapters
    (method-name keys such as ``search_papers``) and new adapters
    (operation-name keys such as ``search``) resolve identically.
    Undeclared operations are fail-closed (False).
    """
    method = OPERATION_METHODS[operation]
    capabilities = dict(getattr(source, "capabilities", {}) or {})
    if operation in capabilities:
        return bool(capabilities[operation])
    return bool(capabilities.get(method, False))


def _require_arg(operation: str, value: str | None, name: str) -> str:
    """Return the operation's single argument, or a clear usage error."""
    if not value:
        raise typer.BadParameter(
            f"operation {operation!r} requires an argument: <{name}>"
        )
    return value


def _emit(payload: object) -> None:
    """Pretty-print a JSON payload (mirrors ``cli._write_output``)."""
    console.print_json(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _print_papers(source_name: str, papers: list[Paper]) -> None:
    console.print(f"[bold]Found[/bold] {len(papers)} paper(s) from {source_name}")
    if papers:
        _emit([paper.to_dict() for paper in papers])


def _print_paper(source_name: str, identifier: str, paper: Paper | None) -> None:
    if paper is None:
        console.print(
            f"[yellow]未找到[/yellow]: no paper found for {identifier!r} "
            f"in {source_name}"
        )
        return
    console.print(f"[bold]Found[/bold] paper from {source_name}")
    _emit(paper.to_dict())


def _print_citations(source_name: str, citations: list[Citation]) -> None:
    console.print(f"[bold]Found[/bold] {len(citations)} citation(s) from {source_name}")
    if citations:
        _emit([citation.to_dict() for citation in citations])


def _normalize_fulltext_payload(payload: object) -> object:
    """Coerce an adapter's fulltext payload into a JSON-serializable shape.

    Unpaywall returns ``list[OALocation]``, CORE returns a URL string and
    Europe PMC returns full-text XML — the CLI renders all three uniformly
    (CR-2) instead of dumping raw ``default=str`` output.
    """
    if isinstance(payload, list):
        return [
            location.to_dict() if callable(getattr(location, "to_dict", None)) else str(location)
            for location in payload
        ]
    return payload


def _print_fulltext(source_name: str, payload: object) -> None:
    if payload is None:
        console.print(f"[yellow]No fulltext[/yellow] available from {source_name}")
        return
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.lower().startswith(("http://", "https://")):
            _emit({"source": source_name, "fulltext_url": stripped})
        else:
            _emit(
                {
                    "source": source_name,
                    "fulltext_chars": len(stripped),
                    "preview": stripped[:500],
                }
            )
        return
    _emit(_normalize_fulltext_payload(payload))


def _print_fulltext_result(source_name: str, fulltext: FullText) -> None:
    """Render a :class:`FullText` produced by the full-text pipeline."""
    console.print(
        f"[bold]Full text[/bold] fetched from {source_name}: "
        f"{fulltext.paragraph_count} paragraphs via {fulltext.source}"
    )
    if fulltext.file_path:
        console.print(f"  file: {fulltext.file_path}")
    if fulltext.oa_license:
        console.print(f"  license: {fulltext.oa_license}")


def _write_json_output(path: str | None, payload: object) -> None:
    """Honour ``--output/-o``: write the JSON payload to *path*."""
    if not path:
        return
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    console.print(f"[green]Wrote[/green] {path}")


def _route_get(source: BaseSource, identifier: str) -> tuple[str, str]:
    """Route a ``get`` identifier to the adapter method that can resolve it.

    Input forms (upgrade technical-design.md §1.1.1): DOI → ``get_paper_by_doi``;
    arXiv ID → ``get_paper_by_arxiv_id`` (when the adapter exposes it);
    source-specific id (OpenAlex W-id / CORE work id) → ``get_paper_by_id``.

    Raises:
        ValueError: When *identifier* matches no supported form for *source*
            — the CLI reports this as a real failure (exit 2) instead of a
            fake "not found" success (CR-1).
    """
    doi = normalize_doi(identifier)
    if doi is not None:
        return "get_paper_by_doi", doi
    if _parse_arxiv_id(identifier) is not None:
        if hasattr(source, "get_paper_by_arxiv_id"):
            return "get_paper_by_arxiv_id", identifier
        raise ValueError(
            f"{source.name} 不支持按 arXiv ID 获取论文；请提供 DOI"
        )
    if hasattr(source, "get_paper_by_id"):
        return "get_paper_by_id", identifier
    supported = "DOI"
    if hasattr(source, "get_paper_by_arxiv_id"):
        supported += "、arXiv ID"
    if hasattr(source, "get_paper_by_id"):
        supported += "、源专用 ID"
    raise ValueError(
        f"无法识别的论文标识 {identifier!r}：{source.name} 仅支持 {supported}"
    )


async def _resolve_paper_for_operation(source: BaseSource, reference: str) -> Paper:
    """Resolve a ``fulltext``-operation argument to a :class:`Paper`.

    The adapters' ``get_fulltext`` signatures take a ``Paper`` (CR-2), so the
    CLI resolves the input (DOI / arXiv ID / source-specific id) through the
    adapter's own get methods first, then hands the paper over.

    Raises:
        ValueError: When the reference cannot be resolved to a paper record.
    """
    method_name, arg = _route_get(source, reference)
    method = getattr(source, method_name, None)
    if method is None:
        raise ValueError(f"{source.name} 无法解析 {reference!r} 对应的论文")
    paper: Paper | None = await method(arg)
    if paper is None:
        raise ValueError(
            f"未找到 {reference!r} 对应的论文记录（{source.name}），无法获取全文"
        )
    return paper


def _config_secret(config: Config, name: str) -> str | None:
    """Unwrap a SecretStr config field, tolerating plain values."""
    value = getattr(config, name, None)
    if value is None:
        return None
    get_secret_value = getattr(value, "get_secret_value", None)
    return get_secret_value() if callable(get_secret_value) else str(value)


def _fulltext_storage(storage: Any) -> SQLiteStorage | None:
    """Return *storage* for full-text persistence when the backend supports it.

    Only the SQLite backend implements ``save_full_text``; the JSON backend
    cannot persist full-text rows, so the pipeline runs without persistence.
    """
    if hasattr(storage, "save_full_text"):
        return cast("SQLiteStorage", storage)
    return None


async def _run_fulltext_pipeline(
    ai: AcademicIntelligence,
    source: BaseSource,
    paper: Paper,
    *,
    persist: bool,
) -> FullText:
    """Run the legal-OA full-text pipeline after a ``get`` (CR-3 ``--fulltext``).

    The pipeline's locator understands ``unpaywall`` / ``core`` / ``arxiv`` /
    ``europe_pmc``; when the adapter is one of those the pipeline is
    restricted to it (Europe PMC's own OA evidence is reused directly),
    otherwise the default priority order applies.  Config/environment
    credentials are forwarded so Unpaywall/CORE lookups work as configured.
    """
    if source.name in {"unpaywall", "core", "arxiv", "europe_pmc"}:
        sources: tuple[str, ...] = (source.name,)
    else:
        sources = DEFAULT_SOURCES
    unpaywall_email = getattr(source, "email", None) or _config_secret(
        ai.config, "unpaywall_email"
    )
    core_api_key = getattr(source, "api_key", None) or _config_secret(
        ai.config, "core_api_key"
    )
    async with FulltextPipeline(
        storage=_fulltext_storage(ai.storage),
        unpaywall_email=unpaywall_email,
        core_api_key=core_api_key,
    ) as pipeline:
        return await pipeline.fetch(paper, sources=sources, persist=persist)


def _run_operation(
    source_name: str,
    operation: str,
    value: str | None,
    *,
    limit: int,
    storage_type: str,
    storage_path: str,
    persist: bool = False,
    fulltext: bool = False,
    output: str | None = None,
) -> None:
    """Run *operation* against the named adapter, mapping errors to exit 2.

    ``--persist`` upserts the results through the shared storage batch path
    (CR-3); ``--fulltext`` runs the legal-OA full-text pipeline after a
    ``get``; ``--output/-o`` writes the JSON payload to a file.

    Exit-code contract (C4, upgrade technical-design §5): an operation that
    resolves to *no* result (``get`` miss, empty ``search`` / ``citations``
    list, no full text) is a real failure and exits 2 — never a fake success;
    any result at all keeps exit 0 (partial semantics live at the multi-source
    collector layer, not here).
    """

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        async with AcademicIntelligence(cfg) as ai:
            resolved = ai._resolve_sources([source_name])
            if not resolved:
                raise ValueError(f"unknown or unavailable data source: {source_name}")
            source = resolved[0]
            if not _source_supports(source, operation):
                raise ValueError(f"{source_name} 不支持 {operation}")
            if operation == "search":
                query = _require_arg(operation, value, "query")
                papers = await source.search_papers(query, limit=limit)
                if persist:
                    await ai.storage.save_batch(papers=papers)
                _print_papers(source_name, papers)
                if not papers:
                    raise typer.Exit(code=2)
                _write_json_output(output, [paper.to_dict() for paper in papers])
            elif operation == "get":
                identifier = _require_arg(operation, value, "id")
                method_name, arg = _route_get(source, identifier)
                method = getattr(source, method_name, None)
                if method is None:
                    raise ValueError(f"{source_name} 不支持按 {method_name} 获取论文")
                paper = await method(arg)
                if paper is None:
                    _print_paper(source_name, identifier, None)
                    raise typer.Exit(code=2)
                if persist:
                    await ai.storage.save_batch(papers=[paper])
                if fulltext:
                    fulltext_result = await _run_fulltext_pipeline(
                        ai, source, paper, persist=persist
                    )
                    _print_paper(source_name, identifier, paper)
                    _print_fulltext_result(source_name, fulltext_result)
                else:
                    _print_paper(source_name, identifier, paper)
                _write_json_output(output, paper.to_dict())
            elif operation == "citations":
                paper_id = _require_arg(operation, value, "paper_id")
                citations = await source.get_citations(paper_id)
                if persist:
                    await ai.storage.save_batch(citations=citations)
                _print_citations(source_name, citations)
                if not citations:
                    raise typer.Exit(code=2)
                _write_json_output(
                    output, [citation.to_dict() for citation in citations]
                )
            elif operation == "fulltext":
                reference = _require_arg(operation, value, "paper")
                paper = await _resolve_paper_for_operation(source, reference)
                method = getattr(source, "get_fulltext", None)
                if method is None:
                    raise ValueError(f"{source_name} 不支持 fulltext")
                payload = await method(paper)
                if persist:
                    await ai.storage.save_batch(papers=[paper])
                _print_fulltext(source_name, payload)
                if payload is None:
                    raise typer.Exit(code=2)
                _write_json_output(
                    output, _normalize_fulltext_payload(payload)
                )
            else:  # unreachable: validated in the generated command
                raise ValueError(f"unknown operation {operation!r}")

    _run_cli(_run())


def register_source(app: typer.Typer, source: BaseSource) -> None:
    """Mount one source adapter onto a ``source`` command group.

    The generated command accepts any operation in :data:`OPERATION_METHODS`
    and dispatches to the adapter's mapped method only when the adapter
    declares the operation in ``capabilities``; undeclared operations are
    rejected with an explicit ``"<source> 不支持 <operation>"`` error
    (fail-closed).

    Friendly aliases from :data:`_SOURCE_COMMAND_ALIASES` (e.g. ``europe-pmc``
    / ``epmc``) are mounted as additional commands that share the same
    handler; the canonical ``source.name`` stays the registry key and the
    resolver input (E1).
    """
    name = source.name
    declared = [op for op in OPERATION_METHODS if _source_supports(source, op)]
    declared_text = ", ".join(declared) if declared else "(none)"
    operation_help = f"Operation: {declared_text}"
    command_names = (name, *_SOURCE_COMMAND_ALIASES.get(name, ()))

    def source_cmd(
        operation: Annotated[str, typer.Argument(help="")],
        value: Annotated[
            str | None, typer.Argument(help="Operation argument")
        ] = None,
        limit: Annotated[
            int, typer.Option("--limit", "-n", help="Max results (search only)")
        ] = 10,
        persist: Annotated[
            bool,
            typer.Option("--persist", help="Save results to storage (upsert)"),
        ] = False,
        fulltext: Annotated[
            bool,
            typer.Option(
                "--fulltext",
                help="Run the legal-OA full-text pipeline after get",
            ),
        ] = False,
        output: Annotated[
            str | None,
            typer.Option("--output", "-o", help="Write JSON output to a file"),
        ] = None,
        storage_type: Annotated[
            str, typer.Option("--storage-type", help="Storage backend (sqlite/json)")
        ] = "sqlite",
        storage_path: Annotated[
            str, typer.Option("--storage-path", help="Storage path")
        ] = "./academic_intelligence.db",
    ) -> None:
        """Run an operation against a source adapter."""
        if operation not in OPERATION_METHODS:
            raise typer.BadParameter(
                f"unknown operation {operation!r}; valid operations: "
                f"{', '.join(OPERATION_METHODS)}"
            )
        if fulltext and operation != "get":
            console.print(
                "[yellow]--fulltext only applies to the get operation; "
                "ignored for this operation[/yellow]"
            )
        _run_operation(
            name,
            operation,
            value,
            limit=limit,
            storage_type=storage_type,
            storage_path=storage_path,
            persist=persist,
            fulltext=fulltext and operation == "get",
            output=output,
        )

    # E1 help hygiene: the operation argument's help must list only the
    # adapter's declared operations.  ``from __future__ import annotations``
    # turns the annotation into a string, so the closure-scoped
    # ``operation_help`` cannot be referenced there (typer re-evaluates
    # string annotations with ``eval_str=True`` and closures are invisible
    # to ``eval``).  Inject the already-evaluated ``Annotated`` object
    # directly; non-string annotation values are not re-evaluated.
    source_cmd.__annotations__["operation"] = Annotated[
        str, typer.Argument(help=operation_help)
    ]

    for command_name in command_names:
        app.command(
            command_name,
            help=(
                f"Run an operation on the {name} adapter. "
                f"Declared operations: {declared_text}."
            ),
        )(source_cmd)


def register_sources(app: typer.Typer) -> None:
    """Register every default source adapter onto *app*."""
    for source_cls in _DEFAULT_SOURCE_CLASSES:
        register_source(app, source_cls())
