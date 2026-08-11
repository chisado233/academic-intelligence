"""CLI tests for the WP1 source-tree refactor.

Covers:
- ``paper --help`` exposes the ``source`` / ``sources`` command groups.
- ``paper source <source> <operation>`` dispatches to the adapter method
  mapped by :data:`academic_intelligence.cli_source.OPERATION_METHODS`.
- ``get`` routes by identifier shape — DOI vs arXiv ID vs source-specific id
  (CR-1); unroutable ids exit 2 instead of a fake "not found" success.
- ``fulltext`` resolves its argument to a Paper before calling the adapter
  (CR-2).
- ``--persist`` / ``--fulltext`` / ``--output`` source options (CR-3).
- Undeclared operations fail closed with an explicit
  ``"<source> 不支持 <operation>"`` error (exit 2).
- The ``ai`` legacy console-script shim prints the rename notice and exits 2.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from academic_intelligence import AcademicIntelligence
from academic_intelligence.cli import ai_legacy_shim, app
from academic_intelligence.cli_source import (
    _source_supports,
    register_source,
)
from academic_intelligence.core.models import AuthorRef, Citation, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.fulltext.models import FullText, Segment
from academic_intelligence.fulltext.pipeline import FulltextPipeline
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.unpaywall import OALocation as UnpaywallOALocation

runner = CliRunner()


class _FakeSource(BaseSource):
    """Minimal concrete adapter for registration / capability tests."""

    name = "fake"
    source_type = SourceType.ARXIV

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return []

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        return []

    async def get_author_profile(self, author_name: str) -> None:
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return []


class _OpStyleSource(_FakeSource):
    capabilities: ClassVar[dict[str, bool]] = {
        "search": True,
        "get": True,
        "citations": False,
        "fulltext": False,
    }


class _MethodStyleSource(_FakeSource):
    capabilities: ClassVar[dict[str, bool]] = {
        "search_papers": True,
        "get_paper_by_doi": True,
        "get_citations": False,
    }


def _paper(title: str) -> Paper:
    return Paper(
        id="fake-id",
        title=title,
        authors=[AuthorRef(name="A")],
        year=2024,
    )


# ---------------------------------------------------------------------------
# Command tree surface
# ---------------------------------------------------------------------------


def test_root_help_exposes_source_and_sources() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "source" in result.stdout
    assert "sources" in result.stdout
    assert "collect" in result.stdout
    assert "query" in result.stdout


def test_source_group_help_lists_registered_sources() -> None:
    result = runner.invoke(app, ["source", "--help"])
    assert result.exit_code == 0
    for name in ("arxiv", "semantic_scholar", "openalex", "pubmed", "ieee"):
        assert name in result.stdout


def test_sources_registry_prints_capability_matrix() -> None:
    """``paper sources`` renders the registry + capability matrix (IM-1)."""
    result = runner.invoke(app, ["sources"])
    assert result.exit_code == 0
    for name in ("arxiv", "crossref", "unpaywall", "europe_pmc", "opencitations", "core"):
        assert name in result.stdout
    assert "fulltext" in result.stdout  # capability column


def test_sources_has_status_subcommand() -> None:
    result = runner.invoke(app, ["sources", "--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout


def test_source_arxiv_help_lists_declared_operations() -> None:
    result = runner.invoke(app, ["source", "arxiv", "--help"])
    assert result.exit_code == 0
    # arXiv declares search + get; citations/fulltext are False -> fail closed.
    assert "search, get" in result.stdout


# ---------------------------------------------------------------------------
# Operation dispatch
# ---------------------------------------------------------------------------


def test_source_arxiv_search_dispatches_to_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``paper source arxiv search <q>`` calls ``search_papers``."""

    async def fake_search(self: ArxivSource, query: str, limit: int = 10) -> list[Paper]:
        return [_paper(f"Result for {query} (limit {limit})")]

    monkeypatch.setattr(ArxivSource, "search_papers", fake_search)
    result = runner.invoke(
        app,
        [
            "source",
            "arxiv",
            "search",
            "deepseek",
            "--limit",
            "3",
            "--storage-path",
            str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 paper(s) from arxiv" in result.output
    assert "deepseek" in result.output


def test_source_arxiv_citations_unsupported(tmp_path: Path) -> None:
    """Undeclared operation -> explicit ``<source> 不支持 <operation>``."""
    result = runner.invoke(
        app,
        ["source", "arxiv", "citations", "abc123", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 2
    assert "arxiv 不支持 citations" in result.output


def test_source_arxiv_fulltext_unsupported(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["source", "arxiv", "fulltext", "paper", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 2
    assert "arxiv 不支持 fulltext" in result.output


def test_source_unknown_operation(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["source", "arxiv", "bogus", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 2
    assert "unknown operation 'bogus'" in result.output


def test_source_search_requires_query(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["source", "arxiv", "search", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 2
    assert "requires an argument" in result.output


def test_source_unknown_source_rejected(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["source", "nonexistent", "search", "x", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 2
    assert "No such command" in result.output


# ---------------------------------------------------------------------------
# CR-1: get routes by identifier shape
# ---------------------------------------------------------------------------


def test_source_arxiv_get_routes_arxiv_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``paper source arxiv get 2501.12948`` dispatches to get_paper_by_arxiv_id."""

    async def fake_get_by_id(self: ArxivSource, arxiv_id: str) -> Paper:
        return _paper(f"ArXiv paper {arxiv_id}")

    async def fail_get_by_doi(self: ArxivSource, doi: str) -> Paper | None:
        raise AssertionError("DOI route must not be used for an arXiv ID")

    monkeypatch.setattr(ArxivSource, "get_paper_by_arxiv_id", fake_get_by_id)
    monkeypatch.setattr(ArxivSource, "get_paper_by_doi", fail_get_by_doi)
    result = runner.invoke(
        app,
        [
            "source", "arxiv", "get", "2501.12948",
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ArXiv paper 2501.12948" in result.output


def test_source_arxiv_get_routes_doi(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A DOI (with doi.org prefix) still routes to get_paper_by_doi."""

    async def fake_get_by_doi(self: ArxivSource, doi: str) -> Paper:
        assert doi == "10.48550/arxiv.2501.12948"
        return _paper(f"By DOI {doi}")

    monkeypatch.setattr(ArxivSource, "get_paper_by_doi", fake_get_by_doi)
    result = runner.invoke(
        app,
        [
            "source", "arxiv", "get", "https://doi.org/10.48550/arxiv.2501.12948",
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "By DOI 10.48550/arxiv.2501.12948" in result.output


def test_source_get_unroutable_identifier_exits_2(tmp_path: Path) -> None:
    """An id matching no supported form is a real failure (exit 2, CR-1)."""
    result = runner.invoke(
        app,
        ["source", "arxiv", "get", "not-an-id", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 2
    assert "无法识别" in result.output


def test_source_get_not_found_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A well-formed id the adapter cannot resolve is exit 2, not a fake 0 (C4)."""
    async def fake_get_by_doi(self: ArxivSource, doi: str) -> Paper | None:
        return None

    monkeypatch.setattr(ArxivSource, "get_paper_by_doi", fake_get_by_doi)
    result = runner.invoke(
        app,
        [
            "source", "arxiv", "get", "10.9999/nonexistent",
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "未找到" in result.output


def test_source_search_no_results_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An empty search result is 'not found' and exits 2 (C4)."""
    async def fake_search(self: ArxivSource, query: str, limit: int = 10) -> list[Paper]:
        return []

    monkeypatch.setattr(ArxivSource, "search_papers", fake_search)
    result = runner.invoke(
        app,
        [
            "source", "arxiv", "search", "no such paper ever",
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "0 paper(s)" in result.output


# ---------------------------------------------------------------------------
# E1: friendly command aliases + capability-driven help text
# ---------------------------------------------------------------------------


def test_source_europe_pmc_aliases_registered(tmp_path: Path) -> None:
    """europe-pmc / epmc work as commands next to the canonical europe_pmc."""
    for alias in ("europe_pmc", "europe-pmc", "epmc"):
        result = runner.invoke(
            app, ["source", alias, "--help", "--storage-path", str(tmp_path / "t.db")]
        )
        assert result.exit_code == 0, f"{alias}: {result.output}"
        assert "europe_pmc" in result.output or alias in result.output


def test_source_aliases_dispatch_same_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Aliased commands resolve the same adapter and run the same handler."""
    seen: list[str] = []

    async def fake_get_by_doi(self: object, doi: str) -> Paper:
        seen.append(doi)
        return _paper(f"By DOI {doi}")

    from academic_intelligence.sources.europe_pmc import EuropePmcSource

    monkeypatch.setattr(EuropePmcSource, "get_paper_by_doi", fake_get_by_doi)
    for alias in ("europe-pmc", "epmc"):
        result = runner.invoke(
            app,
            [
                "source", alias, "get", "10.1000/xyz",
                "--storage-path", str(tmp_path / "t.db"),
            ],
        )
        assert result.exit_code == 0, f"{alias}: {result.output}"
    assert seen == ["10.1000/xyz", "10.1000/xyz"]


def test_source_opencitations_alias_registered(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["source", "coci", "--help", "--storage-path", str(tmp_path / "t.db")]
    )
    assert result.exit_code == 0, result.output


def test_source_help_operation_text_matches_declared(tmp_path: Path) -> None:
    """The OPERATION help text lists only the adapter's declared operations."""
    result = runner.invoke(
        app, ["source", "crossref", "--help", "--storage-path", str(tmp_path / "t.db")]
    )
    assert result.exit_code == 0
    assert "Operation: search, get" in result.stdout
    # Undeclared operations must not leak into the argument help.
    assert "Operation: search, get, citations, fulltext" not in result.stdout
    assert "Declared operations: search, get" in result.stdout


def test_source_europe_pmc_help_operation_text_matches_declared(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["source", "europe_pmc", "--help", "--storage-path", str(tmp_path / "t.db")]
    )
    assert result.exit_code == 0
    assert "Operation: search, get, fulltext" in result.stdout
    assert "citations" not in result.stdout.split("Operation:")[1].split("\n")[0]


def test_source_get_arxiv_id_on_doi_only_source_exits_2(tmp_path: Path) -> None:
    """PubMed only resolves DOIs — an arXiv ID input is rejected (exit 2)."""
    result = runner.invoke(
        app,
        ["source", "pubmed", "get", "2501.12948", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 2
    assert "不支持按 arXiv ID 获取论文" in result.output


def test_source_openalex_get_routes_work_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OpenAlex W-ids fall back to the source-specific get_paper_by_id."""
    from academic_intelligence.sources.openalex import OpenAlexSource

    async def fake_get_by_id(self: OpenAlexSource, work_id: str) -> Paper:
        return _paper(f"OpenAlex {work_id}")

    monkeypatch.setattr(OpenAlexSource, "get_paper_by_id", fake_get_by_id)
    result = runner.invoke(
        app,
        ["source", "openalex", "get", "W123456789", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 0, result.output
    assert "OpenAlex W123456789" in result.output


# ---------------------------------------------------------------------------
# CR-2: fulltext operation resolves a Paper before calling the adapter
# ---------------------------------------------------------------------------


def test_source_unpaywall_fulltext_resolves_doi_to_paper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``paper source unpaywall fulltext <doi>`` passes a Paper to get_fulltext."""
    from academic_intelligence.sources.unpaywall import UnpaywallSource

    seen: list[object] = []

    async def fake_get_by_doi(self: UnpaywallSource, doi: str) -> Paper:
        return Paper(id="u-1", title="OA paper", doi=doi)

    async def fake_get_fulltext(self: UnpaywallSource, paper: Paper) -> list[object]:
        seen.append(paper)
        return [
            UnpaywallOALocation(
                url="https://example.com/oa.pdf",
                host_type="repository",
                license="cc-by",
            )
        ]

    monkeypatch.setattr(UnpaywallSource, "get_paper_by_doi", fake_get_by_doi)
    monkeypatch.setattr(UnpaywallSource, "get_fulltext", fake_get_fulltext)
    result = runner.invoke(
        app,
        [
            "source", "unpaywall", "fulltext", "10.1038/s41586-025-09422-z",
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "https://example.com/oa.pdf" in result.output
    assert len(seen) == 1
    assert isinstance(seen[0], Paper)
    assert seen[0].doi == "10.1038/s41586-025-09422-z"


def test_source_fulltext_unresolvable_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fulltext reference that resolves to nothing is a real failure."""
    from academic_intelligence.sources.unpaywall import UnpaywallSource

    async def fake_get_by_doi(self: UnpaywallSource, doi: str) -> Paper | None:
        return None

    monkeypatch.setattr(UnpaywallSource, "get_paper_by_doi", fake_get_by_doi)
    result = runner.invoke(
        app,
        [
            "source", "unpaywall", "fulltext", "10.1038/s41586-025-09422-z",
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 2
    assert "未找到" in result.output


# ---------------------------------------------------------------------------
# CR-3: --persist / --fulltext / --output source options
# ---------------------------------------------------------------------------


def _query_papers_sync(db_path: Path) -> list[Paper]:
    async def _run() -> list[Paper]:
        cfg = Config(storage_type="sqlite", storage_path=str(db_path))
        async with AcademicIntelligence(cfg) as ai:
            return await ai.storage.query_papers(limit=100)

    return asyncio.run(_run())


def test_source_get_persist_saves_paper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--persist`` upserts the fetched paper (CR-3, paper query 可查)."""
    db = tmp_path / "persist.db"

    async def fake_get_by_id(self: ArxivSource, arxiv_id: str) -> Paper:
        return _paper("Persisted DeepSeek-R1")

    monkeypatch.setattr(ArxivSource, "get_paper_by_arxiv_id", fake_get_by_id)
    result = runner.invoke(
        app,
        [
            "source", "arxiv", "get", "2501.12948", "--persist",
            "--storage-path", str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    papers = _query_papers_sync(db)
    assert len(papers) == 1
    assert papers[0].title == "Persisted DeepSeek-R1"


def test_source_search_persist_saves_papers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "search.db"

    async def fake_search(self: ArxivSource, query: str, limit: int = 10) -> list[Paper]:
        first = _paper("one")
        second = _paper("two")
        first.id = "paper-one"
        second.id = "paper-two"
        return [first, second]

    monkeypatch.setattr(ArxivSource, "search_papers", fake_search)
    result = runner.invoke(
        app,
        [
            "source", "arxiv", "search", "deepseek", "--persist",
            "--limit", "2", "--storage-path", str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(_query_papers_sync(db)) == 2


def test_source_get_output_writes_json_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = tmp_path / "out.json"

    async def fake_get_by_id(self: ArxivSource, arxiv_id: str) -> Paper:
        return _paper("Output paper")

    monkeypatch.setattr(ArxivSource, "get_paper_by_arxiv_id", fake_get_by_id)
    result = runner.invoke(
        app,
        [
            "source", "arxiv", "get", "2501.12948", "--output", str(out),
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert "Output paper" in out.read_text(encoding="utf-8")


def test_source_get_fulltext_flag_runs_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``get --fulltext`` runs the full-text pipeline after the fetch (CR-3)."""
    db = tmp_path / "fulltext.db"

    async def fake_get_by_id(self: ArxivSource, arxiv_id: str) -> Paper:
        return _paper("Fulltext paper")

    async def fake_fetch(
        self: FulltextPipeline,
        paper: Paper,
        sources: tuple[str, ...] = ("unpaywall", "core", "arxiv"),
        persist: bool = True,
    ) -> FullText:
        fulltext = FullText(
            paper_id=paper.id or "x",
            source="arxiv",
            paragraph_count=2,
            segments=[Segment(text="hello", page=1), Segment(text="world", page=1)],
        )
        if persist and self.storage is not None:
            await self.storage.save_full_text(fulltext)
        return fulltext

    monkeypatch.setattr(ArxivSource, "get_paper_by_arxiv_id", fake_get_by_id)
    monkeypatch.setattr(FulltextPipeline, "fetch", fake_fetch)
    result = runner.invoke(
        app,
        [
            "source", "arxiv", "get", "2501.12948", "--fulltext", "--persist",
            "--storage-path", str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2 paragraphs" in result.output


def test_source_fulltext_flag_ignored_for_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_search(self: ArxivSource, query: str, limit: int = 10) -> list[Paper]:
        return [_paper(f"Result for {query}")]

    monkeypatch.setattr(ArxivSource, "search_papers", fake_search)
    result = runner.invoke(
        app,
        [
            "source", "arxiv", "search", "deepseek", "--fulltext",
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "--fulltext only applies to the get operation" in result.output


# ---------------------------------------------------------------------------
# Capability resolution (dual key conventions)
# ---------------------------------------------------------------------------


def test_source_supports_operation_key_convention() -> None:
    source = _OpStyleSource()
    assert _source_supports(source, "search") is True
    assert _source_supports(source, "get") is True
    assert _source_supports(source, "citations") is False
    assert _source_supports(source, "fulltext") is False


def test_source_supports_legacy_method_key_convention() -> None:
    source = _MethodStyleSource()
    assert _source_supports(source, "search") is True
    assert _source_supports(source, "citations") is False
    # Undeclared -> fail closed
    assert _source_supports(source, "fulltext") is False


def test_register_source_mounts_adapter_command() -> None:
    import typer

    standalone = typer.Typer()
    register_source(standalone, _FakeSource())
    result = runner.invoke(standalone, ["fake", "--help"])
    assert result.exit_code == 0
    assert "fake" in result.stdout


# ---------------------------------------------------------------------------
# ai legacy shim
# ---------------------------------------------------------------------------


def test_ai_legacy_shim_prints_rename_and_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        ai_legacy_shim()
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "renamed to 'paper'" in captured.err
