"""Author identity CLI (WP6): ``paper author resolve|profile|search|confirm``.

Wraps :class:`~academic_intelligence.identity.resolver.Resolver`
(upgrade technical-design.md §4.1 / §9 WP6):

- ``paper author resolve <paper-id> "<作者名>"`` — 论文内作者身份解析
  (confirmed reuse → authority-id profile → disambiguated candidate table);
- ``paper author profile <author-id>`` — 按 ID 拉完整档案（含代表作，按引用
  数排序）;
- ``paper author search "<名字>" --disambiguate`` — 候选列表 + 消歧排序
  （候选对比表：机构/方向/合著者/年份/venue + 综合分）;
- ``paper author confirm <candidate-id> --for <paper-id> --name "<作者名>"``
  — 确认写回 ``author_identity_global`` + 论文级证据链接，二次 resolve
  直接命中（跨论文复用）。

Exit-code contract (design §5): a resolution that produced *no* identity
conclusion (paper/author not found, zero candidates) exits 2; any
conclusion at all (including ``ambiguous`` / ``different``) keeps exit 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from academic_intelligence import AcademicIntelligence
from academic_intelligence.cli_source import _config_secret, _run_cli
from academic_intelligence.core.types import Config
from academic_intelligence.identity import Resolver
from academic_intelligence.identity.models import (
    AuthorCandidate,
    AuthorProfile,
)

console = Console()

author_app = typer.Typer(help="Author identity resolution (WP6)")

_MATCH_LABELS = {
    "confirmed": "已确认身份（跨论文复用）",
    "id_linked": "ID 直连源档案",
    "auto": "自动判同",
    "ambiguous": "待确认（ambiguous）",
    "different": "判为不同人",
    "not_found": "未找到候选",
}


def _build_resolver(ai: AcademicIntelligence) -> Resolver:
    """Build a Resolver over the connected engine's storage.

    The default fetcher reuses the engine's shared HTTP client (the engine
    owns its lifecycle; :meth:`Resolver.close` only releases a client the
    fetcher itself created, so nothing is double-closed).
    """
    return Resolver(
        ai.storage,
        http_client=ai._http,  # noqa: SLF001 — shared session client
        openalex_email=_config_secret(ai.config, "openalex_email"),
        s2_api_key=_config_secret(ai.config, "semantic_scholar_api_key"),
    )


def _write_json_output(path: str | None, payload: object) -> None:
    """Honour ``--output/-o``: write the JSON payload to *path*."""
    if not path:
        return
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    console.print(f"[green]Wrote[/green] {path}")


def _print_profile(profile: AuthorProfile) -> None:
    """Render one :class:`AuthorProfile` (resolve / profile commands)."""
    console.print(f"[bold]姓名[/bold] {escape(profile.name)}")
    console.print(
        f"[bold]来源[/bold] {profile.source}  [bold]ID[/bold] {profile.author_id}"
    )
    if profile.affiliation:
        console.print(f"[bold]机构[/bold] {escape(profile.affiliation)}")
    if profile.h_index is not None:
        console.print(f"[bold]h-index[/bold] {profile.h_index}")
    if profile.citations is not None:
        console.print(f"[bold]引用[/bold] {profile.citations}")
    if profile.paper_count is not None:
        console.print(f"[bold]论文数[/bold] {profile.paper_count}")
    if profile.homepage:
        console.print(f"[bold]主页[/bold] {escape(profile.homepage)}")
    if profile.interests:
        console.print(f"[bold]研究方向[/bold] {escape(', '.join(profile.interests))}")
    if profile.profile_url:
        console.print(f"[bold]档案[/bold] {escape(profile.profile_url)}")
    if profile.representative_papers:
        table = Table(title="代表论文（按引用数排序）")
        table.add_column("引用", justify="right", style="cyan")
        table.add_column("年份", justify="right")
        table.add_column("标题")
        table.add_column("venue")
        for paper in profile.representative_papers[:10]:
            table.add_row(
                str(paper.cited_by_count),
                str(paper.year or ""),
                paper.title[:90],
                escape(paper.venue or ""),
            )
        console.print(table)
    if profile.entity_flags:
        console.print(
            f"[yellow]⚠ 疑似归属错误/漏检作品[/yellow]"
            f"（OpenAlex 同名实体下发现署名为 {escape(profile.affiliation or profile.name)} "
            f"的作品，可能属于该作者，需人工确认）"
        )
        for flag in profile.entity_flags:
            console.print(
                f"[yellow]实体 {flag.entity_id}[/yellow]"
                f"（{escape(flag.entity_affiliation or '机构未知')}，"
                f"reason={flag.reason}）"
            )
            if flag.flagged_papers:
                flag_table = Table(show_header=True)
                flag_table.add_column("引用", justify="right", style="cyan")
                flag_table.add_column("年份", justify="right")
                flag_table.add_column("标题")
                flag_table.add_column("work_id")
                for paper in flag.flagged_papers:
                    flag_table.add_row(
                        str(paper.cited_by_count),
                        str(paper.year or ""),
                        paper.title[:80],
                        escape(paper.work_id or ""),
                    )
                console.print(flag_table)


def _print_candidates(candidates: list[AuthorCandidate]) -> None:
    """Render the candidate comparison table (resolve / search output)."""
    if not candidates:
        return
    table = Table(title=f"候选对比表（{len(candidates)}）")
    table.add_column("#", justify="right", style="dim")
    table.add_column("候选 ID")
    table.add_column("机构")
    table.add_column("方向")
    table.add_column("合著者", max_width=24)
    table.add_column("年份", max_width=12)
    table.add_column("venue", max_width=16)
    table.add_column("h-index", justify="right")
    table.add_column("引用", justify="right")
    table.add_column("综合分", justify="right")
    table.add_column("判定")
    for idx, candidate in enumerate(candidates, start=1):
        score = (
            f"{candidate.score:.2f}" if candidate.score is not None else "-"
        )
        verdict = candidate.verdict or ""
        if candidate.paper_match:
            verdict = "same(著作命中)"
        years = ""
        if len(candidate.active_years) > 1:
            years = f"{min(candidate.active_years)}-{max(candidate.active_years)}"
        elif candidate.active_years:
            years = str(candidate.active_years[0])
        table.add_row(
            str(idx),
            escape(candidate.candidate_id),
            escape(candidate.affiliation or ""),
            escape(", ".join(candidate.interests[:3])),
            escape(", ".join(candidate.coauthors[:4])),
            years,
            escape(", ".join(candidate.venues[:3])),
            str(candidate.h_index or ""),
            str(candidate.citations or ""),
            score,
            escape(verdict),
        )
    console.print(table)


def _print_evidence(chain: list[dict[str, Any]]) -> None:
    """Render the evidence chain (证据链)."""
    if not chain:
        return
    console.print("[bold]证据链[/bold]")
    for entry in chain:
        source = entry.get("source") or "?"
        source_id = entry.get("source_id") or ""
        url = entry.get("source_url") or ""
        confidence = entry.get("confidence")
        detail = entry.get("detail")
        bits = [f"[{source}]{source_id}"]
        if url:
            bits.append(url)
        if confidence is not None:
            bits.append(f"置信 {float(confidence):.2f}")
        if detail:
            bits.append(escape(str(detail)))
        console.print(f"  - {' | '.join(bits)}")


@author_app.command("resolve")
def author_resolve_cmd(
    paper_id: Annotated[
        str, typer.Argument(help="Stored paper id (e.g. arXiv ID or internal id)")
    ],
    name: Annotated[str, typer.Argument(help="Byline author name in the paper")],
    storage_type: Annotated[
        str, typer.Option("--storage-type", help="Storage backend (sqlite/json)")
    ] = "sqlite",
    storage_path: Annotated[
        str, typer.Option("--storage-path", help="Storage path")
    ] = "./academic_intelligence.db",
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Write the result JSON to a file"),
    ] = None,
    show_all: Annotated[
        bool,
        typer.Option(
            "--show-all",
            help="Show every candidate in the comparison table "
            "(default: top 10 + total count)",
        ),
    ] = False,
) -> None:
    """Resolve an author's identity inside a stored paper (Q2 / Q6)."""

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        async with AcademicIntelligence(cfg) as ai:
            resolver = _build_resolver(ai)
            async with resolver:
                result = await resolver.resolve(paper_id, name)
        label = _MATCH_LABELS.get(result.match, result.match)
        console.print(f"[bold]resolve {paper_id} / {name!r}[/bold] → [bold]{label}[/bold]")
        if result.profile is not None:
            _print_profile(result.profile)
        if result.candidates:
            shown = result.candidates if show_all else result.candidates[:10]
            _print_candidates(shown)
            if len(result.candidates) > len(shown):
                console.print(
                    f"[dim]…共 {len(result.candidates)} 个候选，"
                    f"显示前 {len(shown)} 个（--show-all 查看全部）[/dim]"
                )
        if result.evidence_chain:
            chain = result.evidence_chain if show_all else result.evidence_chain[:10]
            _print_evidence(chain)
            if len(result.evidence_chain) > len(chain):
                console.print(
                    f"[dim]…证据链共 {len(result.evidence_chain)} 条，"
                    f"显示前 {len(chain)} 条[/dim]"
                )
        if result.message:
            console.print(f"[yellow]{escape(result.message)}[/yellow]")
        _write_json_output(output, result.to_dict())
        if result.match == "not_found":
            raise typer.Exit(code=2)

    _run_cli(_run())


@author_app.command("profile")
def author_profile_cmd(
    author_id: Annotated[
        str, typer.Argument(help="Authority id (OpenAlex A... / S2 id / ORCID)")
    ],
    source: Annotated[
        str,
        typer.Option(
            "--source", help="Authority system: openalex | s2 | orcid"
        ),
    ] = "openalex",
    storage_type: Annotated[
        str, typer.Option("--storage-type", help="Storage backend (sqlite/json)")
    ] = "sqlite",
    storage_path: Annotated[
        str, typer.Option("--storage-path", help="Storage path")
    ] = "./academic_intelligence.db",
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Write the result JSON to a file"),
    ] = None,
) -> None:
    """Fetch the complete profile for one author id (Q3, 代表作按引用排序)."""

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        async with AcademicIntelligence(cfg) as ai:
            resolver = _build_resolver(ai)
            async with resolver:
                profile = await resolver.profile(author_id, source)
        console.print(f"[bold]profile {source}:{author_id}[/bold]")
        _print_profile(profile)
        _write_json_output(output, profile.to_dict())

    _run_cli(_run())


@author_app.command("search")
def author_search_cmd(
    name: Annotated[str, typer.Argument(help="Author name to search")],
    disambiguate: Annotated[
        bool,
        typer.Option(
            "--disambiguate/--no-disambiguate",
            help="Score + sort candidates by disambiguation (Q7/D3)",
        ),
    ] = True,
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="Max candidates to show")
    ] = 10,
    storage_type: Annotated[
        str, typer.Option("--storage-type", help="Storage backend (sqlite/json)")
    ] = "sqlite",
    storage_path: Annotated[
        str, typer.Option("--storage-path", help="Storage path")
    ] = "./academic_intelligence.db",
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Write the result JSON to a file"),
    ] = None,
) -> None:
    """Search same-name candidates, optionally disambiguated (Q7)."""

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        async with AcademicIntelligence(cfg) as ai:
            resolver = _build_resolver(ai)
            async with resolver:
                candidates = await resolver.search(
                    name, disambiguate=disambiguate, limit=limit
                )
        mode = "消歧排序" if disambiguate else "原始顺序"
        console.print(
            f"[bold]search {name!r}[/bold] ({mode}) — "
            f"{len(candidates)} 个候选"
        )
        if candidates:
            _print_candidates(candidates)
        else:
            console.print(
                f"[yellow]未找到[/yellow]: no candidates for {name!r}"
            )
        _write_json_output(
            output, [candidate.to_dict() for candidate in candidates]
        )
        if not candidates:
            raise typer.Exit(code=2)

    _run_cli(_run())


@author_app.command("confirm")
def author_confirm_cmd(
    candidate_id: Annotated[
        str,
        typer.Argument(
            help="Candidate id: openalex:<id> / s2:<id> / orcid:<id> / OpenAlex URL"
        ),
    ],
    paper_id: Annotated[
        str,
        typer.Option("--for", help="Paper id the byline name belongs to"),
    ],
    name: Annotated[
        str, typer.Option("--name", help="Byline author name to confirm")
    ],
    storage_type: Annotated[
        str, typer.Option("--storage-type", help="Storage backend (sqlite/json)")
    ] = "sqlite",
    storage_path: Annotated[
        str, typer.Option("--storage-path", help="Storage path")
    ] = "./academic_intelligence.db",
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Write the result JSON to a file"),
    ] = None,
) -> None:
    """Confirm a candidate as the identity of a paper's byline name (I8).

    Writes ``author_identity_global`` (status=confirmed) + the paper-level
    evidence link; the next ``paper author resolve`` of the same name hits
    it directly (cross-paper reuse).
    """

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        async with AcademicIntelligence(cfg) as ai:
            resolver = _build_resolver(ai)
            async with resolver:
                result = await resolver.confirm(
                    candidate_id, paper_id, name, confirmed_by="cli"
                )
        console.print(
            f"[green]已确认[/green] {result.author_name!r} = "
            f"{result.source}:{result.author_id}"
        )
        console.print(f"  论文级证据链接: {result.paper_id}")
        console.print(
            f"  author_identity_global: status={result.status}, "
            f"confirmed_by={result.confirmed_by}"
        )
        if result.message:
            console.print(f"[yellow]{escape(result.message)}[/yellow]")
        _write_json_output(output, result.to_dict())

    _run_cli(_run())
