"""Import and CLI smoke tests."""

from __future__ import annotations

from typer.testing import CliRunner

from academic_intelligence import AcademicIntelligence, Config, Paper, errors
from academic_intelligence.cli import app
from academic_intelligence.sources import (
    GoogleScholarSource,
    OpenAlexSource,
    SemanticScholarSource,
)


def test_public_imports() -> None:
    assert AcademicIntelligence is not None
    assert Config is not None
    assert Paper is not None
    assert errors.AcademicIntelligenceError is not None
    assert GoogleScholarSource.name == "google_scholar"
    assert SemanticScholarSource.name == "semantic_scholar"
    assert OpenAlexSource.name == "openalex"


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Academic Intelligence" in result.stdout or "collect" in result.stdout


def test_cli_stats_empty(tmp_path) -> None:
    runner = CliRunner()
    db = tmp_path / "empty.db"
    result = runner.invoke(
        app,
        ["stats", "--storage-type", "sqlite", "--storage-path", str(db)],
    )
    assert result.exit_code == 0
    assert "total_papers" in result.stdout
