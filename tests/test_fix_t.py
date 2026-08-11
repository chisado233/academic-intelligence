"""FIX-T: new-user experience fixes (P38 round 20).

- F1 (T2): CLI collect paper/author prints an actionable hint on empty
  results instead of dumping an empty JSON blob.
- F2 (T3): ``ai collect citations <id>`` subcommand.
- F3 (T6): author search that degrades to a keyword search surfaces a
  warning instead of silently returning unrelated papers.
- F4 (T1/T7): ``paper --version`` (README ``--persist`` fixes live in
  README.md / SKILL.md, not here).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from academic_intelligence.cli import app
from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.sources.base import BaseSource
from tests.cassette_replay import install_cassette


def _ev() -> Evidence:
    return Evidence(source=SourceType.OPENALEX, source_url="https://e.com", confidence=0.8)


class _FallbackAuthorSource(BaseSource):
    """Simulates a source whose no-match author search degraded to a keyword
    paper search (OpenAlex / Semantic Scholar behavior, P38 T6)."""

    name = "fallback_author"
    source_type = SourceType.OPENALEX
    capabilities = {
        **BaseSource.capabilities,
        # C1 fail-closed dispatch: this source implements author ops.
        "get_author_papers": True,
        "get_author_profile": True,
        "get_citations": True,
    }

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        # Off-target papers: nothing to do with the misspelled author.
        return [Paper(title=f"Keyword match for {query!r}", authors=["B"], evidence=_ev())]

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        # No author matched -> silent fallback to keyword search.
        return await self.search_papers(author_name, limit=20)

    async def get_author_profile(self, author_name: str) -> Author | None:
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return []


class _RealAuthorSource(BaseSource):
    """A well-behaved source that resolves the author and their papers."""

    name = "real_author"
    source_type = SourceType.OPENALEX
    capabilities = {
        **BaseSource.capabilities,
        # C1 fail-closed dispatch: this source implements author ops.
        "get_author_papers": True,
        "get_author_profile": True,
        "get_citations": True,
    }

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return []

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        return [Paper(title=f"{author_name} paper", authors=[author_name], evidence=_ev())]

    async def get_author_profile(self, author_name: str) -> Author | None:
        return Author(name=author_name, evidence=_ev())

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return []


# ---------------------------------------------------------------------------
# F3 (T6): author fallback warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_t_f3_misspelled_author_warns_fallback() -> None:
    """A misspelled author yields papers (from the keyword fallback) and no
    author profile; the result must carry a visible fallback warning."""
    collector = MultiSourceCollector(config=Config(), sources=[_FallbackAuthorSource()])
    result = await collector.collect_author_papers("Geoffrey Hintin")
    assert result.papers, "fallback keyword search should return papers"
    assert result.authors == []
    assert any("fell back to keyword search" in w for w in result.warnings)
    assert any("author not found" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_fix_t_f3_correct_author_has_no_fallback_warning() -> None:
    """A correctly-resolved author (papers + profile) must not be flagged as
    a degraded keyword search."""
    collector = MultiSourceCollector(config=Config(), sources=[_RealAuthorSource()])
    result = await collector.collect_author_papers("Geoffrey Hinton")
    assert result.papers
    assert result.authors
    assert not any("fell back to keyword search" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# F1 (T2): CLI empty-result actionable hint
# ---------------------------------------------------------------------------


def test_fix_t_f1_collect_paper_empty_results_hints(tmp_path, monkeypatch) -> None:
    """``ai collect paper`` with no matches prints an actionable hint, skips
    the empty JSON dump, and keeps exit code 0 (scripts that treat non-zero
    as a hard failure are not broken)."""
    install_cassette(monkeypatch, "openalex_empty")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "collect",
            "paper",
            "definitely-no-papers-xyz",
            "--sources",
            "openalex",
            "--storage-path",
            str(tmp_path / "t2.db"),
        ],
    )
    assert result.exit_code == 0
    assert "No results" in result.stdout
    # rich wraps the hint across lines; collapse whitespace before matching
    normalized = " ".join(result.stdout.split())
    assert "Check the spelling" in normalized
    assert "--sources arxiv" in normalized  # actionable arXiv hint
    # empty JSON must not be dumped
    assert "{\n" not in result.stdout


def test_fix_t_f1_collect_paper_with_results_unchanged(tmp_path, monkeypatch) -> None:
    """``ai collect paper`` with matches behaves exactly as before: Found
    line + JSON payload."""
    install_cassette(monkeypatch, "openalex_search")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "collect",
            "paper",
            "machine learning",
            "--sources",
            "openalex",
            "--limit",
            "5",
            "--storage-path",
            str(tmp_path / "t2b.db"),
        ],
    )
    assert result.exit_code == 0
    assert "Found 5 papers" in result.stdout
    assert "title" in result.stdout  # JSON dumped


def test_fix_t_f1_collect_author_empty_results_hints(tmp_path, monkeypatch) -> None:
    """``ai collect author`` with no matches also prints the hint."""
    install_cassette(monkeypatch, "openalex_empty")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "collect",
            "author",
            "definitely-no-author-xyz",
            "--sources",
            "openalex",
            "--storage-path",
            str(tmp_path / "t2c.db"),
        ],
    )
    assert result.exit_code == 0
    assert "No results" in result.stdout
    normalized = " ".join(result.stdout.split())
    assert "Check the spelling" in normalized


# ---------------------------------------------------------------------------
# F2 (T3): CLI citations subcommand
# ---------------------------------------------------------------------------


def test_fix_t_f2_citations_help() -> None:
    """``ai collect citations --help`` is registered and documented."""
    runner = CliRunner()
    result = runner.invoke(app, ["collect", "citations", "--help"])
    assert result.exit_code == 0
    assert "citations" in result.stdout
    assert "--persist" in result.stdout
    assert "--output" in result.stdout


def test_fix_t_f2_citations_collect(tmp_path, monkeypatch) -> None:
    """``ai collect citations <id>`` returns the citation count and the full
    citing-paper records from an offline cassette."""
    install_cassette(monkeypatch, "openalex_real_closure")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "collect",
            "citations",
            "W2919115771",
            "--sources",
            "openalex",
            "--storage-path",
            str(tmp_path / "t3.db"),
        ],
    )
    assert result.exit_code == 0
    assert "Found 3 citations, 3 citing papers" in result.stdout
    assert '"citations"' in result.stdout  # JSON payload contains citations


# ---------------------------------------------------------------------------
# F4 (T7): --version
# ---------------------------------------------------------------------------


def test_fix_t_f4_version() -> None:
    """``paper --version`` prints the package version and exits 0."""
    from academic_intelligence import __version__

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
