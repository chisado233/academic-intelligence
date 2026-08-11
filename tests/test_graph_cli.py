"""CLI tests for the new `paper expand` and `paper export` commands."""

from __future__ import annotations

from typer.testing import CliRunner

from academic_intelligence.cli import app

runner = CliRunner()


def test_cli_expand_help() -> None:
    result = runner.invoke(app, ["expand", "--help"])
    assert result.exit_code == 0
    assert "relations" in result.stdout
    assert "depth" in result.stdout
    assert "sources" in result.stdout
    assert "output" in result.stdout


def test_cli_export_help() -> None:
    result = runner.invoke(app, ["export", "--help"])
    assert result.exit_code == 0
    assert "center" in result.stdout
    assert "radius" in result.stdout
    assert "output" in result.stdout


def test_cli_expand_requires_entity_id() -> None:
    result = runner.invoke(app, ["expand"])
    assert result.exit_code != 0


def test_cli_export_requires_center() -> None:
    result = runner.invoke(app, ["export"])
    assert result.exit_code != 0
