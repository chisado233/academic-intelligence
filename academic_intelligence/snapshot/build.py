"""Decompress JSONL.gz partitions into the SQLite snapshot index.

``build_snapshot`` streams each downloaded ``part_*.gz`` (one JSON object per
line, the OpenAlex works snapshot format) and writes two things per work:

- the work row itself (``snapshot_works``), and
- the **inverted** citation edges — every ``referenced_works`` entry becomes
  ``(cited_id=ref, citing_id=this work)`` in ``snapshot_citations``, so a
  reverse-citation lookup is a single indexed ``cited_id`` query.

Build is **interruptible and resumable**: each partition file is processed
inside one transaction and recorded in ``snapshot_parts`` afterwards; a
re-run skips finished partitions and only continues with the missing ones.
All inserts are ``INSERT OR IGNORE`` (idempotent — resuming never duplicates
rows).  When the local index belongs to a *different* snapshot date than the
manifest, the index is rebuilt from scratch (with a notice), so a re-build
after a new quarterly release is a clean swap.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from academic_intelligence.snapshot import SnapshotError, downloads_dir, snapshot_index_path
from academic_intelligence.snapshot.manifest import SnapshotManifest
from academic_intelligence.snapshot.store import (
    _STATUS_BUILDING,
    _STATUS_BUILT,
    SnapshotStore,
    normalize_doi,
    normalize_work_id,
)

#: Works per flush inside a partition's transaction.
_WORK_FLUSH = 10_000
#: Citation edges per flush inside a partition's transaction.
_EDGE_FLUSH = 50_000


@dataclass
class BuildResult:
    """Result of a build run.

    Attributes:
        snapshot_date: Snapshot date that was indexed.
        parts_processed: Partition files indexed this run.
        parts_resumed: Partition files already indexed (skipped this run).
        missing_parts: Manifest files with no local download (warned, not fatal).
        works_count: Works in the index after the run.
        citation_count: Inverted citation edges after the run.
        already_built: True when the run was a no-op (index already complete).
        rebuilt: True when an existing index of a different date was dropped
            and rebuilt.
    """

    snapshot_date: str | None = None
    parts_processed: list[str] = field(default_factory=list)
    parts_resumed: list[str] = field(default_factory=list)
    missing_parts: list[str] = field(default_factory=list)
    works_count: int = 0
    citation_count: int = 0
    already_built: bool = False
    rebuilt: bool = False


ProgressCallback = Callable[[str, int, int | None], None]


def _parse_work_line(
    raw: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Extract (work row, referenced bare W-ids) from one snapshot JSONL object.

    Returns ``(None, [])`` when the line carries no usable work id (merged /
    deleted records and non-work entities are skipped).
    """
    work_id = normalize_work_id(raw.get("id"))
    if work_id is None:
        return None, []
    title_raw = raw.get("title") or raw.get("display_name")
    title = str(title_raw) if title_raw is not None else None
    year_raw = raw.get("publication_year")
    year = year_raw if isinstance(year_raw, int) and not isinstance(year_raw, bool) else None
    cited_raw = raw.get("cited_by_count")
    cited = cited_raw if isinstance(cited_raw, int) and not isinstance(cited_raw, bool) else 0

    references: list[str] = []
    refs_raw = raw.get("referenced_works")
    if isinstance(refs_raw, list):
        for ref in refs_raw:
            ref_id = normalize_work_id(ref)
            if ref_id is not None:
                references.append(ref_id)
    return (
        {
            "id": work_id,
            "title": title,
            "year": year,
            "doi": normalize_doi(raw.get("doi")),
            "cited_by_count": cited,
        },
        references,
    )


def _index_partition(
    store: SnapshotStore,
    file_key: str,
    path: Path,
    expected_records: int | None,
    on_progress: ProgressCallback | None,
) -> tuple[int, int]:
    """Index one partition file inside a single transaction.

    Returns ``(works, citation_edges)`` added.  Any exception rolls the
    transaction back, so the file is *not* marked built and a re-run
    re-processes it from scratch (resume is per-file, not mid-file).
    """
    works = 0
    edges = 0
    work_rows: list[dict[str, Any]] = []
    edge_pairs: list[tuple[str, str]] = []
    with store.transaction():
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise SnapshotError(
                            f"JSONL 解析失败: {path.name} 第 {works + 1} 条（{exc}）"
                        ) from exc
                    if not isinstance(raw, dict):
                        continue
                    work, references = _parse_work_line(raw)
                    if work is None:
                        continue
                    work_rows.append(work)
                    edge_pairs.extend((ref, work["id"]) for ref in references)
                    works += 1
                    edges += len(references)
                    if len(work_rows) >= _WORK_FLUSH:
                        store.insert_works(work_rows)
                        work_rows = []
                    if len(edge_pairs) >= _EDGE_FLUSH:
                        store.insert_citation_pairs(edge_pairs)
                        edge_pairs = []
                    if on_progress is not None and works % 1000 == 0:
                        on_progress(file_key, works, expected_records)
        except OSError as exc:
            raise SnapshotError(f"gz 解压失败: {path.name}（{exc}）") from exc
        store.insert_works(work_rows)
        store.insert_citation_pairs(edge_pairs)
    if on_progress is not None:
        on_progress(file_key, works, expected_records)
    return works, edges


def build_snapshot(
    manifest: SnapshotManifest,
    snapshot_dir: Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> BuildResult:
    """Build (or resume) the SQLite index from downloaded partitions.

    Args:
        manifest: The parsed works snapshot manifest describing the files.
        snapshot_dir: Snapshot store root (index at ``<root>/index.db``,
            partitions under ``<root>/downloads/``).
        on_progress: Per-partition progress callback
            ``(file_key, processed_records, expected_records)``.

    Returns:
        A :class:`BuildResult` with per-partition processing details.

    Raises:
        SnapshotError: When no partition is downloaded yet, or a partition
            file is corrupt/unparseable.
    """
    downloads = downloads_dir(snapshot_dir)
    db_path = snapshot_index_path(snapshot_dir)
    store = SnapshotStore(db_path)
    store.connect()
    result = BuildResult(snapshot_date=manifest.snapshot_date)
    try:
        meta = store.get_meta()
        existing_date = meta.get("snapshot_date") if meta is not None else None
        if manifest.snapshot_date and existing_date and existing_date != manifest.snapshot_date:
            result.rebuilt = True
            store.drop_all()
            store.init_schema()

        # Partition files that exist locally; the rest are warned, not fatal.
        pending: list[tuple[str, Path, int | None]] = []
        for file in manifest.files:
            local = downloads / file.key
            if local.is_file():
                pending.append((file.key, local, file.record_count))
            else:
                result.missing_parts.append(file.key)
        if not pending:
            raise SnapshotError(
                "没有可构建的分区文件：downloads 目录为空或清单文件未下载。"
                "请先运行 paper snapshot download（或 --date YYYY-MM-DD 只下增量分区）"
            )

        built_keys = store.built_part_keys()
        for key, _path, _count in pending:
            if key in built_keys:
                result.parts_resumed.append(key)
        unfinished = [(k, p, c) for k, p, c in pending if k not in built_keys]

        if not unfinished and not result.missing_parts:
            result.already_built = True
            stats = store.stats()
            result.works_count = int(stats["works_count"])
            result.citation_count = int(stats["citation_count"])
            return result

        store.set_meta(manifest.snapshot_date or "unknown", _STATUS_BUILDING)
        for key, path, expected in unfinished:
            _index_partition(store, key, path, expected, on_progress=on_progress)
            store.mark_part_built(key)
            result.parts_processed.append(key)
        store.set_meta(manifest.snapshot_date or "unknown", _STATUS_BUILT)
        stats = store.stats()
        result.works_count = int(stats["works_count"])
        result.citation_count = int(stats["citation_count"])
        return result
    finally:
        store.close()
