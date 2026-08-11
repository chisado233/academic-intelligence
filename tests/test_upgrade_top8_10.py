"""Upgrade round 4: structured failures, graph snapshots, and scalable exports."""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from academic_intelligence import AcademicIntelligence
from academic_intelligence.cli import app
from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.exceptions import (
    AllSourcesFailedError,
    RateLimitError,
    SourceFailure,
)
from academic_intelligence.core.models import Author, Citation, Paper
from academic_intelligence.exporters import ExportDependencyError, export_papers
from academic_intelligence.graph import KnowledgeGraph
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.ieee import IEEESource
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage

runner = CliRunner()


class _FailingSource(BaseSource):
    name = "failing"

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        raise RateLimitError(
            "quota exhausted",
            source_name=self.name,
            context={"retry_count": 3, "http_status": 429},
        )

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        return []

    async def get_author_profile(self, author_name: str) -> Author | None:
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return []


class _SuccessfulSource(_FailingSource):
    name = "successful"

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return [Paper(id="paper-1", title="Structured collection")]


def test_source_failure_exposes_structured_fields_and_string_compatibility() -> None:
    failure = SourceFailure.from_exception(
        source="semantic_scholar",
        operation="search_papers",
        exc=RateLimitError(
            "quota exhausted",
            source_name="semantic_scholar",
            context={"retry_count": 3, "http_status": 429},
        ),
    )

    assert failure.source == "semantic_scholar"
    assert failure.operation == "search_papers"
    assert failure.error_type == "RateLimitError"
    assert failure.message == "quota exhausted"
    assert failure.retry_count == 3
    assert failure.http_status == 429
    assert failure.transient is True
    assert failure.permanent is False
    assert str(failure) == "semantic_scholar: quota exhausted"
    assert "quota exhausted" in failure
    assert failure == "semantic_scholar: quota exhausted"


def test_all_sources_failed_normalizes_legacy_strings_to_records() -> None:
    exc = AllSourcesFailedError(
        "all failed",
        query="transformers",
        sources_attempted=["legacy"],
        failures={"legacy": "network down"},
    )

    assert isinstance(exc.failures["legacy"], SourceFailure)
    assert exc.failures["legacy"].source == "legacy"
    assert exc.failures["legacy"].operation == "unknown"
    assert exc.failures["legacy"].message == "network down"
    assert exc.failures == {"legacy": "legacy: network down"}
    assert str(exc).count("legacy:") == 1


@pytest.mark.asyncio
async def test_collector_emits_structured_source_failures() -> None:
    collector = MultiSourceCollector(sources=[_SuccessfulSource(), _FailingSource()])

    result = await collector.collect("structured")

    assert len(result.errors) == 1
    failure = result.errors[0]
    assert isinstance(failure, SourceFailure)
    assert failure.source == "failing"
    assert failure.operation == "search_papers"
    assert failure.retry_count == 3
    assert failure.http_status == 429
    assert result.stats["source_failures"][0]["source"] == "failing"


def test_source_capabilities_mark_citation_stubs_unsupported() -> None:
    assert ArxivSource().supports("get_citations") is False
    assert IEEESource().supports("get_citations") is False
    assert ArxivSource().capabilities["search_papers"] is True


def test_facade_exposes_source_capabilities_without_connecting(tmp_path: Path) -> None:
    ai = AcademicIntelligence(
        {
            "sources": ["arxiv", "ieee"],
            "storage_path": str(tmp_path / "db.sqlite"),
        }
    )

    capabilities = ai.source_capabilities()

    assert capabilities["arxiv"]["get_citations"] is False
    assert capabilities["ieee"]["get_citations"] is False
    assert capabilities["arxiv"]["get_paper_by_doi"] is True


def test_graph_snapshot_round_trip_preserves_nodes_edges_and_version(tmp_path: Path) -> None:
    graph = KnowledgeGraph()
    graph.add_node("p1", type="paper", title="Root", year=2024)
    graph.add_node("p2", type="paper", loaded=False)
    graph.add_edge("p1", "p2", relation="cites", confidence=0.8)
    path = tmp_path / "graph.json"

    graph.save_snapshot(path)
    restored = KnowledgeGraph.load_snapshot(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert restored.export_json() == graph.export_json()
    assert restored.get_node("p2") == {
        "id": "p2",
        "type": "paper",
        "loaded": False,
    }
    assert restored.to_subgraph("p1", radius=1).edges()[0]["confidence"] == 0.8
    assert not list(tmp_path.glob("graph.json.tmp-*"))


def test_graph_snapshot_rejects_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "future.json"
    path.write_text(
        json.dumps({"version": 999, "nodes": [], "edges": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="snapshot version"):
        KnowledgeGraph.load_snapshot(path)


def test_cli_expand_snapshot_can_be_exported_in_a_new_process(tmp_path: Path) -> None:
    storage_path = tmp_path / "storage"

    async def _seed() -> None:
        store = JSONStorage(str(storage_path))
        await store.connect()
        try:
            await store.save_batch(
                papers=[
                    Paper(
                        id="p1",
                        title="Root paper",
                        references=["p2"],
                    ),
                    Paper(id="p2", title="Referenced paper"),
                ]
            )
        finally:
            await store.close()

    asyncio.run(_seed())

    inline = runner.invoke(
        app,
        [
            "expand",
            "p1",
            "--relations",
            "references",
            "--no-fetch-missing",
            "--storage-type",
            "json",
            "--storage-path",
            str(storage_path),
        ],
    )
    assert inline.exit_code == 0, inline.stdout
    assert '"center_id": "p1"' in inline.stdout

    snapshot_path = tmp_path / "expanded.json"
    expand = runner.invoke(
        app,
        [
            "expand",
            "p1",
            "--relations",
            "references",
            "--no-fetch-missing",
            "--storage-type",
            "json",
            "--storage-path",
            str(storage_path),
            "--output",
            str(snapshot_path),
        ],
    )
    assert expand.exit_code == 0, expand.stdout
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["version"] == 1
    assert {node["id"] for node in snapshot["nodes"]} == {"p1", "p2"}

    export_path = tmp_path / "subgraph.json"
    exported = runner.invoke(
        app,
        [
            "export",
            "--center",
            "p1",
            "--snapshot",
            str(snapshot_path),
            "--output",
            str(export_path),
        ],
    )
    assert exported.exit_code == 0, exported.stdout
    subgraph = json.loads(export_path.read_text(encoding="utf-8"))
    assert {node["id"] for node in subgraph["nodes"]} == {"p1", "p2"}
    assert subgraph["center"] == "p1"


@pytest.mark.asyncio
async def test_facade_loaded_snapshot_survives_lazy_connect(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "facade-graph.json"
    graph = KnowledgeGraph()
    graph.add_node("p1", type="paper", title="Persisted graph")
    graph.save_snapshot(snapshot_path)
    ai = AcademicIntelligence(
        {
            "storage_type": "json",
            "storage_path": str(tmp_path / "facade-storage"),
        }
    )
    ai.load_graph_snapshot(snapshot_path)

    try:
        subgraph = await ai.subgraph("p1", radius=0)
        assert subgraph["node_count"] == 1
    finally:
        await ai.close()


def _storage_for(tmp_path: Path, backend: str) -> SQLiteStorage | JSONStorage:
    if backend == "sqlite":
        return SQLiteStorage(str(tmp_path / "cursor.sqlite"))
    return JSONStorage(str(tmp_path / "cursor-json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_query_papers_keyset_pages_are_stable_complete_and_disjoint(
    tmp_path: Path,
    backend: str,
) -> None:
    store = _storage_for(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                Paper(id="p3", title="Same"),
                Paper(id="p1", title="Same"),
                Paper(id="p4", title="Zulu"),
                Paper(id="p2", title="Same"),
            ]
        )

        first = await store.query_papers(order_by="title", limit=2)
        second = await store.query_papers(
            order_by="title",
            after=first[-1].id,
            limit=2,
        )

        ids = [paper.id for paper in first + second]
        assert ids == ["p1", "p2", "p3", "p4"]
        assert len(ids) == len(set(ids))
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_query_authors_keyset_uses_id_as_tie_breaker(
    tmp_path: Path,
    backend: str,
) -> None:
    store = _storage_for(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            authors=[
                Author(id="a2", name="Same"),
                Author(id="a3", name="Zulu"),
                Author(id="a1", name="Same"),
            ]
        )

        first = await store.query_authors(order_by="name", limit=2)
        second = await store.query_authors(
            order_by="name",
            after=first[-1].id,
            limit=2,
        )

        assert [author.id for author in first + second] == ["a1", "a2", "a3"]
        assert len(await store.query_authors(interest="", limit=1)) == 1
    finally:
        await store.close()


class _PagedPaperStorage:
    def __init__(self, papers: list[Paper]) -> None:
        self.papers = papers
        self.calls: list[tuple[int, str | None]] = []

    async def query_papers(
        self,
        *,
        limit: int,
        after: str | None,
        order_by: str,
    ) -> list[Paper]:
        assert order_by == "id"
        self.calls.append((limit, after))
        start = 0
        if after is not None:
            start = next(i + 1 for i, paper in enumerate(self.papers) if paper.id == after)
        return self.papers[start : start + limit]


@pytest.mark.asyncio
async def test_jsonl_export_queries_and_writes_bounded_batches(tmp_path: Path) -> None:
    storage = _PagedPaperStorage(
        [Paper(id=f"p{i}", title=f"Paper {i}") for i in range(5)]
    )
    output = tmp_path / "papers.jsonl"

    count = await export_papers(
        storage,
        output,
        format="jsonl",
        batch_size=2,
    )

    assert count == 5
    assert storage.calls == [(2, None), (2, "p1"), (2, "p3"), (2, "p4")]
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [record["id"] for record in records] == [f"p{i}" for i in range(5)]


@pytest.mark.parametrize("export_format", ["csv", "jsonl"])
def test_cli_export_papers_writes_storage_rows(
    tmp_path: Path,
    export_format: str,
) -> None:
    storage_path = tmp_path / "export-storage"

    async def _seed() -> None:
        store = JSONStorage(str(storage_path))
        await store.connect()
        try:
            await store.save_batch(
                papers=[
                    Paper(id="p2", title="Second", keywords=["graph"]),
                    Paper(id="p1", title="First", keywords=["AI", "ML"]),
                ]
            )
        finally:
            await store.close()

    asyncio.run(_seed())
    output = tmp_path / f"papers.{export_format}"
    result = runner.invoke(
        app,
        [
            "export-papers",
            "--format",
            export_format,
            "--output",
            str(output),
            "--batch-size",
            "1",
            "--storage-type",
            "json",
            "--storage-path",
            str(storage_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    if export_format == "jsonl":
        records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    else:
        with output.open(encoding="utf-8", newline="") as handle:
            records = list(csv.DictReader(handle))
        assert json.loads(records[0]["keywords"]) == ["AI", "ML"]
    assert [record["id"] for record in records] == ["p1", "p2"]


@pytest.mark.asyncio
async def test_parquet_export_reports_missing_optional_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(name: str) -> object:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("academic_intelligence.exporters.importlib.import_module", _missing)
    storage = _PagedPaperStorage([Paper(id="p1", title="Paper")])

    with pytest.raises(ExportDependencyError, match=r"academic-intelligence\[export\]"):
        await export_papers(
            storage,
            tmp_path / "papers.parquet",
            format="parquet",
        )
