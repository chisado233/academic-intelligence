"""Budget & source-registry CLI surface (WP5).

``paper sources`` renders the source registry plus the capability matrix
(upgrade technical-design.md §7, IM-1) — it is no longer an alias of
``paper source``.  ``paper sources status`` adds the per-source quota
snapshot (BudgetManager ``status()`` over the shared SQLite store), and
``paper budget`` is the standalone all-source quota overview.
"""

from __future__ import annotations

from typing import Annotated, cast

import typer
from rich.console import Console
from rich.table import Table

from academic_intelligence import AcademicIntelligence
from academic_intelligence.budget.manager import BudgetManager
from academic_intelligence.budget.models import BudgetStatus
from academic_intelligence.budget.store import BudgetStore
from academic_intelligence.cli_source import (
    _DEFAULT_SOURCE_CLASSES,
    OPERATION_METHODS,
    _run_cli,
    _source_supports,
)
from academic_intelligence.core.types import Config

console = Console()

#: (source name, declared operations) rows of the capability matrix.
CapabilityRow = tuple[str, dict[str, bool]]


def _capability_rows() -> list[CapabilityRow]:
    """Declared operations of every registered source adapter (no network)."""
    rows: list[CapabilityRow] = []
    for source_cls in _DEFAULT_SOURCE_CLASSES:
        source = source_cls()
        rows.append(
            (
                source.name,
                {op: bool(_source_supports(source, op)) for op in OPERATION_METHODS},
            )
        )
    return rows


def _print_matrix(rows: list[CapabilityRow]) -> None:
    operations = list(OPERATION_METHODS)
    table = Table(title="Source registry & capability matrix")
    table.add_column("Source", style="cyan")
    for op in operations:
        table.add_column(op, justify="center")
    for name, ops in rows:
        table.add_row(
            name,
            *["✓" if ops[op] else "–" for op in operations],
        )
    console.print(table)


def _budget_store(ai: AcademicIntelligence) -> BudgetStore | None:
    """Return the connected storage as a BudgetStore when it implements it."""
    if hasattr(ai.storage, "save_budget_usage"):
        return cast(BudgetStore, ai.storage)
    return None


def _print_budget_table(statuses: list[BudgetStatus]) -> None:
    if not statuses:
        console.print("[yellow]No configured budgets[/yellow]")
        return
    table = Table(title="Budget quotas (current period)")
    table.add_column("Source", style="cyan")
    table.add_column("Semantics")
    table.add_column("Period")
    table.add_column("Used")
    table.add_column("Limit")
    table.add_column("Remaining")
    for status in statuses:
        exhausted = " (exhausted)" if status.quota_exhausted else ""
        table.add_row(
            status.source,
            status.semantics.value,
            status.period,
            f"{status.used:g}",
            f"{status.limit:g}",
            f"{status.remaining:g}{exhausted}",
        )
    console.print(table)


def build_sources_app() -> typer.Typer:
    """Build the ``sources`` command group (registry matrix + status)."""
    sources_app = typer.Typer(
        help="Source registry and health (capability matrix / quotas)",
    )

    @sources_app.callback(invoke_without_command=True)
    def _sources_callback(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        _print_matrix(_capability_rows())

    @sources_app.command("status")
    def sources_status_cmd(
        storage_type: Annotated[
            str, typer.Option("--storage-type", help="Storage backend")
        ] = "sqlite",
        storage_path: Annotated[
            str, typer.Option("--storage-path", help="Storage path")
        ] = "./academic_intelligence.db",
    ) -> None:
        """Show the capability matrix plus per-source quota status."""

        async def _run() -> None:
            cfg = Config(storage_type=storage_type, storage_path=storage_path)
            async with AcademicIntelligence(cfg) as ai:
                manager = BudgetManager(store=_budget_store(ai))
                statuses = await manager.status()
            _print_matrix(_capability_rows())
            _print_budget_table(statuses)

        _run_cli(_run())

    return sources_app


def budget_cmd(
    storage_type: Annotated[
        str, typer.Option("--storage-type", help="Storage backend")
    ] = "sqlite",
    storage_path: Annotated[
        str, typer.Option("--storage-path", help="Storage path")
    ] = "./academic_intelligence.db",
) -> None:
    """Show per-source budget quotas (all-source overview, §7)."""

    async def _run() -> None:
        cfg = Config(storage_type=storage_type, storage_path=storage_path)
        async with AcademicIntelligence(cfg) as ai:
            manager = BudgetManager(store=_budget_store(ai))
            statuses = await manager.status()
        _print_budget_table(statuses)

    _run_cli(_run())
