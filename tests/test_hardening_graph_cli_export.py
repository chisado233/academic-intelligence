"""CLI automation and export-contract regressions."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from academic_intelligence import AcademicIntelligence
from academic_intelligence.cli import app
from academic_intelligence.core.models import (
    Author,
    CollectionResult,
    ExpandResult,
    ExpandStats,
    IncrementalUpdateResult,
    Paper,
)
from academic_intelligence.exporters import ExportDependencyError, export_papers

runner = CliRunner()


def test_root_help_lists_promised_convenience_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # WP6 CLI restructure (functional-design §4.1): the top-level `author`
    # command is now the identity group (resolve/profile/search/confirm);
    # direct author collection lives under `collect author`.  The former
    # `author-papers` alias was removed with the restructure.
    for command in ("paper", "author", "collect", "update"):
        assert command in result.stdout


def test_pyproject_declares_click_as_a_direct_runtime_dependency() -> None:
    """The installed CLI imports click directly, so packaging must provide it."""
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert any(dependency.lower().startswith("click") for dependency in dependencies)


def test_direct_paper_command_forwards_to_existing_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, list[str] | None, bool, int]] = []

    async def fake_collect(
        self: AcademicIntelligence,
        query: str,
        sources: list[str] | None = None,
        *,
        persist: bool = False,
        limit: int = 10,
    ) -> CollectionResult:
        calls.append((query, sources, persist, limit))
        return CollectionResult(papers=[Paper(id="p1", title="A paper")])

    monkeypatch.setattr(AcademicIntelligence, "collect_paper", fake_collect)
    result = runner.invoke(
        app,
        [
            "paper",
            "10.1000/test",
            "--sources",
            "arxiv",
            "--persist",
            "--limit",
            "3",
            "--storage-type",
            "json",
            "--storage-path",
            str(tmp_path / "paper-store"),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert calls == [("10.1000/test", ["arxiv"], True, 3)]
    assert "A paper" in result.stdout


@pytest.mark.parametrize("command", ["collect author"])
def test_direct_author_commands_share_existing_author_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
) -> None:
    """`paper collect author <name>` forwards to the collect-author API.

    WP6 restructure (functional-design §4.1): the top-level `author`
    command hosts the identity group (resolve/profile/search/confirm); the
    direct author-collection path is `collect author` (the former
    `author` / `author-papers` convenience aliases were removed with it).
    """
    calls: list[str] = []

    async def fake_collect(
        self: AcademicIntelligence,
        name: str,
        sources: list[str] | None = None,
        *,
        persist: bool = False,
    ) -> CollectionResult:
        calls.append(name)
        return CollectionResult(
            authors=[Author(id="a1", name=name)],
            papers=[Paper(id="p1", title="Authored paper")],
        )

    monkeypatch.setattr(AcademicIntelligence, "collect_author_papers", fake_collect)
    argv = command.split() + [
        "Ada Lovelace",
        "--storage-type",
        "json",
        "--storage-path",
        str(tmp_path / command.replace(" ", "_")),
    ]
    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.stdout
    assert calls == ["Ada Lovelace"]
    assert "Ada Lovelace" in result.stdout


def test_update_author_reports_structured_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_update(
        self: AcademicIntelligence,
        name: str,
        sources: list[str] | None = None,
    ) -> IncrementalUpdateResult:
        return IncrementalUpdateResult(
            new=[Paper(id="new", title="New")],
            unchanged=["old-1", "old-2"],
            total_checked=3,
            sources_used=sources or [],
        )

    monkeypatch.setattr(AcademicIntelligence, "update_author_papers", fake_update)
    output = tmp_path / "update.json"
    result = runner.invoke(
        app,
        [
            "update",
            "--author",
            "Ada",
            "--sources",
            "arxiv",
            "--output",
            str(output),
            "--storage-type",
            "json",
            "--storage-path",
            str(tmp_path / "update-store"),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "3" in result.stdout and "1 new" in result.stdout
    assert json.loads(output.read_text(encoding="utf-8"))["total_checked"] == 3


@pytest.mark.parametrize(("partial", "expected_code"), [(False, 2), (True, 0)])
def test_expand_total_failure_is_nonzero_but_partial_success_is_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    partial: bool,
    expected_code: int,
) -> None:
    async def fake_expand(self: AcademicIntelligence, *args: Any, **kwargs: Any) -> ExpandResult:
        nodes = [{"id": "neighbor", "type": "paper"}] if partial else []
        return ExpandResult(
            center_id="missing",
            nodes=nodes,
            stats=ExpandStats(
                nodes_found=len(nodes),
                failed=1,
                failures=["paper not found"],
            ),
        )

    monkeypatch.setattr(AcademicIntelligence, "expand", fake_expand)
    result = runner.invoke(
        app,
        [
            "expand",
            "missing",
            "--no-fetch-missing",
            "--storage-type",
            "json",
            "--storage-path",
            str(tmp_path / f"expand-{partial}"),
        ],
    )

    assert result.exit_code == expected_code
    assert "paper not found" in result.stdout


def test_cli_preserves_bracketed_parquet_install_guidance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def unavailable(*args: Any, **kwargs: Any) -> int:
        raise ExportDependencyError(
            "Parquet export requires pyarrow; install "
            "`academic-intelligence[export]`"
        )

    monkeypatch.setattr("academic_intelligence.cli.export_papers", unavailable)
    result = runner.invoke(
        app,
        [
            "export-papers",
            "--format",
            "parquet",
            "--output",
            str(tmp_path / "papers.parquet"),
            "--storage-type",
            "json",
            "--storage-path",
            str(tmp_path / "store"),
        ],
    )

    assert result.exit_code == 2
    assert "academic-intelligence[export]" in result.stdout


@pytest.mark.asyncio
async def test_parquet_importerror_is_mapped_to_export_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def broken_import(name: str) -> Any:
        raise ImportError("binary ABI mismatch")

    monkeypatch.setattr("academic_intelligence.exporters.importlib.import_module", broken_import)
    storage = SimpleNamespace(query_papers=lambda **kwargs: None)
    with pytest.raises(ExportDependencyError, match=r"academic-intelligence\[export\]"):
        await export_papers(storage, tmp_path / "broken.parquet", format="parquet")


@pytest.mark.asyncio
async def test_parquet_broken_import_does_not_leak_third_party_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def noisy_broken_import(name: str) -> Any:
        print("RAW NUMPY ABI TRACEBACK", file=sys.stderr)
        raise ImportError("binary ABI mismatch")

    monkeypatch.setattr(
        "academic_intelligence.exporters.importlib.import_module",
        noisy_broken_import,
    )
    storage = SimpleNamespace(query_papers=lambda **kwargs: None)
    with pytest.raises(ExportDependencyError):
        await export_papers(storage, tmp_path / "broken.parquet", format="parquet")

    assert "RAW NUMPY ABI TRACEBACK" not in capsys.readouterr().err


class _BatchStorage:
    def __init__(self, papers: list[Paper]) -> None:
        self.papers = papers

    async def query_papers(
        self,
        *,
        limit: int,
        after: str | None,
        order_by: str,
    ) -> list[Paper]:
        start = 0
        if after is not None:
            start = next(i + 1 for i, paper in enumerate(self.papers) if paper.id == after)
        return self.papers[start : start + limit]


@pytest.mark.asyncio
async def test_parquet_uses_one_declared_schema_for_all_batches_and_empty_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    table_calls: list[tuple[list[dict[str, Any]], object]] = []
    writer_schemas: list[object] = []

    class FakeTable:
        @staticmethod
        def from_pylist(records: list[dict[str, Any]], schema: object) -> object:
            table_calls.append((records, schema))
            return SimpleNamespace(schema=schema)

    fake_pa = SimpleNamespace(
        Table=FakeTable,
        field=lambda name, kind: (name, kind),
        schema=lambda fields: tuple(fields),
        string=lambda: "string",
        int64=lambda: "int64",
        float64=lambda: "float64",
    )

    class FakeWriter:
        def __init__(self, path: str, schema: object) -> None:
            writer_schemas.append(schema)
            Path(path).write_bytes(b"PAR1")

        def write_table(self, table: object) -> None:
            assert table.schema == writer_schemas[-1]

        def close(self) -> None:
            return None

    fake_pq = SimpleNamespace(ParquetWriter=FakeWriter)

    def fake_import(name: str) -> object:
        return fake_pa if name == "pyarrow" else fake_pq

    monkeypatch.setattr("academic_intelligence.exporters.importlib.import_module", fake_import)
    papers = [
        Paper(id="p1", title="Null first", year=None),
        Paper(id="p2", title="Typed second", year=2024, keywords=["AI"]),
    ]
    await export_papers(
        _BatchStorage(papers),
        tmp_path / "two.parquet",
        format="parquet",
        batch_size=1,
    )
    await export_papers(
        _BatchStorage([]),
        tmp_path / "empty.parquet",
        format="parquet",
    )

    assert writer_schemas and all(schema == writer_schemas[0] for schema in writer_schemas)
    assert table_calls and all(schema == writer_schemas[0] for _, schema in table_calls)
    assert isinstance(table_calls[1][0][0]["keywords"], str)


@pytest.mark.asyncio
async def test_excel_safe_csv_is_explicit_and_jsonl_remains_raw(tmp_path: Path) -> None:
    paper = Paper(id="p1", title='=HYPERLINK("https://x") 中文')
    storage = _BatchStorage([paper])
    raw = tmp_path / "raw.csv"
    safe = tmp_path / "safe.csv"
    jsonl = tmp_path / "papers.jsonl"

    await export_papers(storage, raw, format="csv")
    await export_papers(storage, safe, format="csv", excel_safe=True)
    await export_papers(storage, jsonl, format="jsonl", excel_safe=False)

    assert not raw.read_bytes().startswith(b"\xef\xbb\xbf")
    assert '=HYPERLINK(""https://x"") 中文' in raw.read_text(encoding="utf-8")
    assert safe.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "'=HYPERLINK" in safe.read_text(encoding="utf-8-sig")
    assert json.loads(jsonl.read_text(encoding="utf-8"))["title"].startswith("=")
