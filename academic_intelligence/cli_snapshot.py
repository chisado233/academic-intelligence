"""Snapshot CLI: ``paper snapshot status|download|build|enable|disable``.

Mounts the OpenAlex free-snapshot workflow onto the ``paper`` command tree:

- ``paper snapshot status`` — local snapshot state (routing switch, download
  partition count, index date / works / citation edges);
- ``paper snapshot download [--date YYYY-MM-DD] [--dir PATH]`` — download the
  works partitions (progress, resume, gz verify); prints an explicit **size
  notice** before downloading (a user-initiated, deliberately large operation);
- ``paper snapshot build [--dir PATH]`` — decompress JSONL.gz → SQLite index
  (progress, interruptible/resumable);
- ``paper snapshot enable|disable [--dir PATH]`` — query-routing switch:
  when enabled, ``paper trace-citing`` without an explicit flag prefers the
  local snapshot.

Default store: ``<project>/snapshot_data/`` (override with ``--dir``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from academic_intelligence.cli_source import _run_cli
from academic_intelligence.snapshot import (
    default_snapshot_dir,
    downloads_dir,
    snapshot_index_path,
)
from academic_intelligence.snapshot.build import build_snapshot
from academic_intelligence.snapshot.download import download_all
from academic_intelligence.snapshot.manifest import fetch_manifest
from academic_intelligence.snapshot.store import (
    SnapshotStore,
    read_routing_config,
    write_routing_config,
)

snapshot_app = typer.Typer(
    help=(
        "OpenAlex 免费快照：下载季度全量 works 分区并建本地引用索引"
        "（paper trace-citing --use-snapshot 零 API 额度查询）"
    ),
    no_args_is_help=True,
)
console = Console(highlight=False)

#: Estimated gzip→JSONL expansion factor for the size notice (docs: ~330 GB
#: compressed → ~1.6 TB decompressed ≈ 4.8×).
_GZ_EXPANSION = 5


def _human_size(byte_count: int) -> str:
    """Render a byte count in a compact human form (B/KB/MB/GB/TB)."""
    size = float(byte_count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{byte_count} B"


def _resolve_dir(dir_value: str | None) -> Path:
    """Resolve ``--dir`` to a snapshot store path (default: project snapshot_data)."""
    if dir_value:
        return Path(dir_value).expanduser()
    return default_snapshot_dir()


def _print_size_notice(file_count: int, total_bytes: int, filtered: bool) -> None:
    """Print the mandatory size notice *before* any download starts."""
    console.print()
    console.print("[bold yellow]磁盘大小提示（下载前必读）[/bold yellow]")
    console.print(
        "  OpenAlex works 快照为季度全量数据，本清单共 "
        f"[bold]{file_count}[/bold] 个分区文件，压缩后约 "
        f"[bold]{_human_size(total_bytes)}[/bold]，"
        f"解压后约 {_GZ_EXPANSION} 倍（约 {_human_size(total_bytes * _GZ_EXPANSION)}）。"
    )
    console.print("  个人使用请先评估磁盘空间；下载是用户主动选择的操作。")
    if not filtered:
        console.print("  如需只取某个 updated_date 增量分区，用 --date YYYY-MM-DD。")
    console.print()


def _print_index_status(snapshot_dir: Path) -> None:
    """Render the local index state (date / status / works / citation edges)."""
    db_path = snapshot_index_path(snapshot_dir)
    if not db_path.is_file():
        console.print("  索引：[yellow]未构建[/yellow]（先 paper snapshot download / build）")
        return
    store = SnapshotStore(db_path)
    store.connect()
    try:
        meta = store.get_meta()
        stats = store.stats()
    finally:
        store.close()
    if meta is None:
        console.print("  索引：[yellow]未构建[/yellow]（index.db 存在但无元数据）")
        return
    console.print(
        f"  索引：[green]{meta.get('snapshot_date') or '?'}[/green] "
        f"状态 {meta.get('status') or '?'}，"
        f"[bold]{stats['works_count']}[/bold] works，"
        f"[bold]{stats['citation_count']}[/bold] 引用边"
        f"（{db_path.name}）"
    )


@snapshot_app.command("status")
def status_cmd(
    dir: Annotated[
        str | None,
        typer.Option("--dir", help="快照数据目录（默认 <项目>/snapshot_data）"),
    ] = None,
) -> None:
    """显示本地快照状态（路由开关/分区数/索引日期/works 数/引用边数）。"""
    snapshot_dir = _resolve_dir(dir)
    downloads = downloads_dir(snapshot_dir)
    routing = read_routing_config(snapshot_dir)

    console.print(f"[bold]快照数据目录[/bold] {snapshot_dir}")
    if routing is None:
        console.print(
            "  路由开关：[yellow]未设置[/yellow]（默认走 API；"
            "paper snapshot enable 后 trace-citing 默认先查本地）"
        )
    else:
        state = "[green]已启用[/green]" if routing else "[yellow]已禁用[/yellow]"
        console.print(f"  路由开关：{state}")

    if downloads.is_dir():
        parts = sorted(downloads.glob("*.gz"))
        total = sum(p.stat().st_size for p in parts)
        console.print(
            f"  已下载：{len(parts)} 个分区文件，{_human_size(total)}"
            + (f"（{downloads}）" if parts else "")
        )
    else:
        console.print("  已下载：[yellow]无[/yellow]（paper snapshot download）")
    _print_index_status(snapshot_dir)
    if routing:
        console.print(
            "  [green]提示：[/green]paper trace-citing 将默认先查本地快照，"
            "未命中时回退 API（--no-use-snapshot 可跳过本地）"
        )


@snapshot_app.command("download")
def download_cmd(
    date: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="只下载该 updated_date 分区（YYYY-MM-DD）；默认下载全部 works 分区",
        ),
    ] = None,
    dir: Annotated[
        str | None,
        typer.Option("--dir", help="快照数据目录（默认 <项目>/snapshot_data）"),
    ] = None,
) -> None:
    """下载 works 快照分区（进度/断点续传/校验 gz；先打印大小提示）。"""

    async def _run() -> None:
        snapshot_dir = _resolve_dir(dir)
        dest = downloads_dir(snapshot_dir)
        dest.mkdir(parents=True, exist_ok=True)
        manifest = await fetch_manifest()
        files = list(manifest.files)
        filtered = False
        if date:
            matched = [f for f in files if f.date == date]
            if not matched:
                available = sorted({f.date for f in files if f.date is not None})
                sample = ", ".join(available[-5:]) if available else "无"
                raise typer.BadParameter(
                    f"清单中没有 {date!r} 分区（最近可用的 updated_date：{sample}）"
                )
            files = matched
            filtered = True
        total_bytes = sum(f.size or 0 for f in files)
        console.print(f"[bold]快照清单[/bold] {manifest.snapshot_date or '?'}（{manifest.url}）")
        if manifest.record_count is not None:
            console.print(
                f"  works 总数：{manifest.record_count:,}"
                f"（清单压缩总大小 {_human_size(manifest.content_length or 0)}）"
            )
        _print_size_notice(len(files), total_bytes, filtered)

        async def _progress(url: str, done: int, total: int | None) -> None:
            name = url.rsplit("/", 1)[-1]
            if total:
                pct = done * 100 // total
                console.print(
                    f"\r  [dim]{name}[/dim] {_human_size(done)}/{_human_size(total)} ({pct}%)",
                    end="",
                )
            else:
                console.print(f"\r  [dim]{name}[/dim] {_human_size(done)}", end="")

        summary = await download_all(files, dest, on_progress=_progress)
        console.print()
        if summary.downloaded:
            console.print(
                f"[green]下载完成[/green] {len(summary.downloaded)} 个分区"
                f"（{_human_size(summary.bytes_written)}）→ {dest}"
            )
        if summary.skipped:
            console.print(f"[dim]跳过[/dim] {len(summary.skipped)} 个已存在且校验通过的分区")
        console.print("[green]下一步[/green] paper snapshot build 建本地索引")

    _run_cli(_run())


@snapshot_app.command("build")
def build_cmd(
    dir: Annotated[
        str | None,
        typer.Option("--dir", help="快照数据目录（默认 <项目>/snapshot_data）"),
    ] = None,
) -> None:
    """解压 JSONL.gz 建 SQLite 索引（进度；可中断续建）。"""

    async def _run() -> None:
        snapshot_dir = _resolve_dir(dir)
        manifest = await fetch_manifest()
        console.print(f"[bold]快照清单[/bold] {manifest.snapshot_date or '?'}（{manifest.url}）")

        def _progress(file_key: str, done: int, expected: int | None) -> None:
            suffix = f"/{expected}" if expected else ""
            console.print(f"\r  [dim]{file_key}[/dim] {done}{suffix} works", end="")

        result = await asyncio.to_thread(
            build_snapshot, manifest, snapshot_dir, on_progress=_progress
        )
        console.print()
        if result.rebuilt:
            console.print(
                "[yellow]检测到快照日期变化[/yellow]：已重建索引（旧的 "
                f"{manifest.snapshot_date} 索引被替换）"
            )
        if result.already_built:
            console.print(
                f"[green]已构建[/green] 日期 {result.snapshot_date}："
                f"{result.works_count} works，{result.citation_count} 引用边"
                "（无需重建）"
            )
            return
        if result.parts_processed:
            console.print(
                f"[green]构建完成[/green] 日期 {result.snapshot_date}："
                f"处理 {len(result.parts_processed)} 个分区，"
                f"[bold]{result.works_count}[/bold] works，"
                f"[bold]{result.citation_count}[/bold] 引用边"
                f"（{snapshot_index_path(snapshot_dir).name}）"
            )
        if result.parts_resumed:
            console.print(f"[dim]续建跳过[/dim] {len(result.parts_resumed)} 个已构建分区")
        if result.missing_parts:
            console.print(
                f"[yellow]提示[/yellow] {len(result.missing_parts)} 个清单分区未下载"
                "（paper snapshot download --date 补下后可继续 build 续建）"
            )
        console.print(
            "[green]下一步[/green] paper trace-citing <id> --use-snapshot "
            "（或 paper snapshot enable 后默认先查本地）"
        )

    _run_cli(_run())


def _set_routing(snapshot_dir: Path, enabled: bool, label: str) -> None:
    """Write the routing switch and confirm."""
    write_routing_config(snapshot_dir, enabled)
    console.print(
        f"[green]快照路由已{label}[/green]：paper trace-citing 将"
        + ("默认先查本地快照" if enabled else "默认走 API")
        + "（--use-snapshot/--no-use-snapshot 可临时覆盖）"
    )


@snapshot_app.command("enable")
def enable_cmd(
    dir: Annotated[
        str | None,
        typer.Option("--dir", help="快照数据目录（默认 <项目>/snapshot_data）"),
    ] = None,
) -> None:
    """启用查询路由：trace-citing 默认先查本地快照（未命中回退 API）。"""
    _set_routing(_resolve_dir(dir), True, "启用")


@snapshot_app.command("disable")
def disable_cmd(
    dir: Annotated[
        str | None,
        typer.Option("--dir", help="快照数据目录（默认 <项目>/snapshot_data）"),
    ] = None,
) -> None:
    """禁用查询路由：trace-citing 恢复默认走 API。"""
    _set_routing(_resolve_dir(dir), False, "禁用")


def register_snapshot(app: typer.Typer) -> None:
    """Mount the snapshot subcommand group onto the ``paper`` app."""
    app.add_typer(snapshot_app, name="snapshot")
