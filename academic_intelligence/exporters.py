"""Streaming paper exports backed by keyset-paginated storage queries."""

from __future__ import annotations

import contextlib
import csv
import importlib
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

from academic_intelligence.core.exceptions import AcademicIntelligenceError
from academic_intelligence.core.models import Paper

ExportFormat = Literal["csv", "jsonl", "parquet"]


class ExportDependencyError(AcademicIntelligenceError):
    """Raised when an optional export backend is unavailable."""


class PaperQueryStorage(Protocol):
    """Minimal storage surface required by :func:`export_papers`."""

    async def query_papers(
        self,
        *,
        limit: int,
        after: str | None,
        order_by: str,
    ) -> list[Paper]: ...


_PARQUET_INTEGER_FIELDS = frozenset({"year", "citations", "reference_count"})
_PARQUET_FLOAT_FIELDS = frozenset({"synthetic_confidence"})
_PARQUET_JSON_FIELDS = frozenset(
    {
        "authors",
        "keywords",
        "fields_of_study",
        "references",
        "citations_list",
        "evidence_list",
        "evidence",
    }
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _csv_value(value: Any, *, excel_safe: bool = False) -> Any:
    if value is None:
        return ""
    if isinstance(value, list | dict):
        value = _json_text(value)
    if excel_safe and isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _parquet_schema(pa: Any, fields: list[str]) -> Any:
    """Return a schema independent of the values observed in any batch."""
    arrow_fields = []
    for field in fields:
        if field in _PARQUET_INTEGER_FIELDS:
            kind = pa.int64()
        elif field in _PARQUET_FLOAT_FIELDS:
            kind = pa.float64()
        else:
            kind = pa.string()
        arrow_fields.append(pa.field(field, kind))
    return pa.schema(arrow_fields)


def _parquet_record(paper: Paper, fields: list[str]) -> dict[str, Any]:
    record = paper.model_dump(mode="json")
    return {
        field: (
            None
            if record.get(field) is None
            else _json_text(record[field])
            if field in _PARQUET_JSON_FIELDS
            else record[field]
        )
        for field in fields
    }


async def _iter_batches(
    storage: PaperQueryStorage,
    *,
    after: str | None,
    batch_size: int,
) -> Any:
    cursor = after
    while True:
        batch = await storage.query_papers(
            order_by="id",
            after=cursor,
            limit=batch_size,
        )
        if not batch:
            return
        yield batch
        cursor = batch[-1].id
        if cursor is None:
            raise ValueError("stored paper is missing an id and cannot be paginated")


async def export_papers(
    storage: PaperQueryStorage,
    output: str | Path,
    *,
    format: ExportFormat,
    after: str | None = None,
    batch_size: int = 500,
    excel_safe: bool = False,
) -> int:
    """Stream stored papers to CSV, JSONL, or optional Parquet output."""
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")
    if format not in {"csv", "jsonl", "parquet"}:
        raise ValueError("format must be one of: csv, jsonl, parquet")
    if excel_safe and format != "csv":
        raise ValueError("excel_safe is only valid for CSV exports")

    pa: Any = None
    pq: Any = None
    if format == "parquet":
        try:
            # Some ABI-broken NumPy/pyarrow combinations print a full native
            # traceback before raising ImportError.  Keep suppression scoped
            # strictly to optional dependency loading; exporter runtime
            # failures after a successful import remain fully visible.
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(
                io.StringIO()
            ):
                pa = importlib.import_module("pyarrow")
                pq = importlib.import_module("pyarrow.parquet")
        except ImportError as exc:
            raise ExportDependencyError(
                "Parquet export requires pyarrow; install "
                "`academic-intelligence[export]`"
            ) from exc

    target = Path(output)
    temporary = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex}")
    fields = list(Paper.model_fields)
    parquet_schema = _parquet_schema(pa, fields) if format == "parquet" else None
    count = 0
    writer: Any = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if format == "jsonl":
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                async for batch in _iter_batches(
                    storage,
                    after=after,
                    batch_size=batch_size,
                ):
                    for paper in batch:
                        handle.write(
                            json.dumps(
                                paper.model_dump(mode="json"),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        )
                        handle.write("\n")
                        count += 1
                handle.flush()
                os.fsync(handle.fileno())
        elif format == "csv":
            encoding = "utf-8-sig" if excel_safe else "utf-8"
            with temporary.open("w", encoding=encoding, newline="") as handle:
                csv_writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                csv_writer.writeheader()
                async for batch in _iter_batches(
                    storage,
                    after=after,
                    batch_size=batch_size,
                ):
                    for paper in batch:
                        record = paper.model_dump(mode="json")
                        csv_writer.writerow(
                            {
                                field: _csv_value(
                                    record.get(field), excel_safe=excel_safe
                                )
                                for field in fields
                            }
                        )
                        count += 1
                handle.flush()
                os.fsync(handle.fileno())
        else:
            async for batch in _iter_batches(
                storage,
                after=after,
                batch_size=batch_size,
            ):
                records: list[dict[str, Any]] = [
                    _parquet_record(paper, fields) for paper in batch
                ]
                table = pa.Table.from_pylist(records, schema=parquet_schema)
                if writer is None:
                    writer = pq.ParquetWriter(str(temporary), parquet_schema)
                writer.write_table(table)
                count += len(batch)
            if writer is None:
                empty = pa.Table.from_pylist([], schema=parquet_schema)
                writer = pq.ParquetWriter(str(temporary), parquet_schema)
                writer.write_table(empty)
            writer.close()
            writer = None
        os.replace(temporary, target)
        return count
    finally:
        if writer is not None:
            writer.close()
        temporary.unlink(missing_ok=True)


__all__ = ["ExportDependencyError", "ExportFormat", "export_papers"]
