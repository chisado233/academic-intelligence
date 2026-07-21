"""Import smoke tests and CLI help."""

from __future__ import annotations

from typer.testing import CliRunner

from academic_intelligence import AcademicIntelligence, Config, Paper, SourceType
from academic_intelligence.cli import app
from academic_intelligence.sources import (
    GoogleScholarSource,
    OpenAlexSource,
    SemanticScholarSource,
)
from academic_intelligence.utils import HTTPClient, ProxyPool, RateLimiter, Cache


def test_package_imports() -> None:
    assert AcademicIntelligence is not None
    assert Config is not None
    assert SourceType.GOOGLE_SCHOLAR.value == "google_scholar"
    assert GoogleScholarSource.name == "google_scholar"
    assert SemanticScholarSource.name == "semantic_scholar"
    assert OpenAlexSource.name == "openalex"
    assert HTTPClient is not None
    assert ProxyPool is not None
    assert Cache is not None


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "collect" in result.stdout
    assert "query" in result.stdout
    assert "stats" in result.stdout


def test_cli_stats_empty(tmp_path) -> None:
    runner = CliRunner()
    db = tmp_path / "empty.db"
    result = runner.invoke(
        app,
        ["stats", "--storage-type", "sqlite", "--storage-path", str(db)],
    )
    assert result.exit_code == 0
    assert "total_papers" in result.stdout
