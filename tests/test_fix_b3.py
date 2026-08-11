"""FIX-B3 regression tests.

Covers:
- F1 (I-11): CLI entry layer exception mapping — friendly errors with
  exit code 2 instead of raw tracebacks for invalid ``--storage-type``,
  unknown ``--sources`` and unknown ``--relations``.
- F2 (I-11 GBK): ``sys.stdout`` is reconfigured to UTF-8 on startup so
  Chinese output decodes on UTF-8 pipes.
- F4 (I-10): multi-source field conflicts surface as
  ``CollectionResult.warnings`` without changing merge behavior.
"""

from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from academic_intelligence.cli import _force_utf8_stdout, app, main
from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.models import Author, AuthorRef, Citation, Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.sources.base import BaseSource

runner = CliRunner()


def _ev(source: SourceType, conf: float = 0.8) -> Evidence:
    return Evidence(
        source=source,
        source_url=f"https://example.com/{source.value}",
        confidence=conf,
    )


# ---------------------------------------------------------------------------
# F1: CLI entry layer exception mapping (I-11)
# ---------------------------------------------------------------------------


def test_cli_stats_invalid_storage_type_friendly_error() -> None:
    """`ai stats --storage-type nonsense` -> exit 2, no traceback."""
    result = runner.invoke(app, ["stats", "--storage-type", "nonsense"])
    assert result.exit_code == 2
    assert "Error" in result.output
    assert "storage_type" in result.output
    assert "Traceback" not in result.output


def test_cli_collect_unknown_sources_rejected() -> None:
    """`--sources foo,bar` is rejected at the CLI layer, not by the collector."""
    result = runner.invoke(
        app,
        ["collect", "author", "Jane Doe", "--sources", "foo,bar"],
    )
    assert result.exit_code == 2
    assert "unknown data source" in result.output
    assert "foo" in result.output and "bar" in result.output
    assert "Traceback" not in result.output


def test_cli_expand_unknown_relation_lists_valid_values() -> None:
    """`--relations bogus` -> exit 2 and the legal relation names."""
    result = runner.invoke(app, ["expand", "p1", "--relations", "bogus"])
    assert result.exit_code == 2
    assert "unknown relation" in result.output
    assert "references,citations,authors,papers,coauthors" in result.output
    assert "Traceback" not in result.output


def test_cli_year_garbage_keeps_click_behavior() -> None:
    """Existing `--year garbage` graceful click error is preserved (exit 2)."""
    result = runner.invoke(app, ["query", "--year", "garbage"])
    assert result.exit_code == 2
    assert "Invalid year format" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# F2: stdout UTF-8 reconfigure (I-11 GBK)
# ---------------------------------------------------------------------------


def test_force_utf8_stdout_reconfigures_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() startup reconfigures stdout/stderr to UTF-8."""
    calls: list[dict[str, object]] = []

    class FakeStream:
        encoding = "cp936"

        def reconfigure(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", FakeStream())
    monkeypatch.setattr(sys, "stderr", FakeStream())

    _force_utf8_stdout()

    assert len(calls) == 2
    assert all(c.get("encoding") == "utf-8" for c in calls)


def test_main_invokes_utf8_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI entry point triggers the reconfigure before running the app."""
    invoked: list[bool] = []
    monkeypatch.setattr(
        "academic_intelligence.cli._force_utf8_stdout",
        lambda: invoked.append(True),
    )
    monkeypatch.setattr("academic_intelligence.cli.app", lambda: None)

    main()

    assert invoked == [True]


# ---------------------------------------------------------------------------
# F4: data-conflict warnings (I-10)
# ---------------------------------------------------------------------------


class _StaticSource(BaseSource):
    """A source adapter that always returns one canned paper."""

    name = "static"
    source_type = SourceType.OPENALEX

    def __init__(self, paper: Paper) -> None:
        self.paper = paper

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return [self.paper]

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return self.paper if self.paper.doi == doi else None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        return [self.paper]

    async def get_author_profile(self, author_name: str) -> Author | None:
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return []


def _conflicted_pair() -> tuple[Paper, Paper]:
    oa = Paper(
        title="Conflicted Work",
        authors=[AuthorRef(name="A")],
        year=2025,
        venue="Journal Alpha",
        doi="10.1234/conflicted",
        evidence=_ev(SourceType.OPENALEX, 0.9),
    )
    arxiv = Paper(
        title="Conflicted Work",
        authors=[AuthorRef(name="A")],
        year=2017,
        venue="Journal Beta",
        doi="10.1234/conflicted",
        evidence=_ev(SourceType.ARXIV, 0.7),
    )
    return oa, arxiv


def test_deduplicator_merge_reports_field_conflicts() -> None:
    """Merging records with a >1-year gap / venue mismatch yields warnings."""
    from academic_intelligence.processors.deduplicator import Deduplicator

    dedup = Deduplicator()
    oa, arxiv = _conflicted_pair()
    merged = dedup.deduplicate_papers([oa, arxiv])

    assert len(merged) == 1
    warnings = dedup.pop_warnings()
    assert any(
        "year conflict" in w and "openalex=2025" in w and "arxiv=2017" in w
        for w in warnings
    )
    assert any("venue conflict" in w for w in warnings)
    # Merge behavior is unchanged: the higher-confidence year won.
    assert merged[0].year == 2025


@pytest.mark.asyncio
async def test_collection_warnings_surface_on_collection_result() -> None:
    """Conflicts detected during dedup appear on CollectionResult.warnings."""
    oa, arxiv = _conflicted_pair()
    collector = MultiSourceCollector(
        config=Config(),
        sources=[_StaticSource(oa), _StaticSource(arxiv)],
    )

    result = await collector.collect("Conflicted Work")

    assert len(result.papers) == 1
    assert any("year conflict" in w for w in result.warnings)
    assert any("venue conflict" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_collection_without_conflicts_has_empty_warnings() -> None:
    """Consistent multi-source records produce no warnings."""
    oa = Paper(
        title="Consistent Work",
        authors=[AuthorRef(name="A")],
        year=2020,
        venue="Shared Venue",
        doi="10.1234/consistent",
        evidence=_ev(SourceType.OPENALEX, 0.9),
    )
    arxiv = Paper(
        title="Consistent Work",
        authors=[AuthorRef(name="A")],
        year=2020,
        venue="Shared Venue",
        doi="10.1234/consistent",
        evidence=_ev(SourceType.ARXIV, 0.7),
    )
    collector = MultiSourceCollector(
        config=Config(),
        sources=[_StaticSource(oa), _StaticSource(arxiv)],
    )

    result = await collector.collect("Consistent Work")

    assert len(result.papers) == 1
    assert result.warnings == []


def test_collection_result_warnings_roundtrip() -> None:
    """warnings survives to_dict/from_dict (defaults to [] for old payloads)."""
    from academic_intelligence.core.models import CollectionResult

    plain = CollectionResult(papers=[])
    assert plain.warnings == []
    restored = CollectionResult.from_dict(plain.to_dict())
    assert restored.warnings == []

    with_warn = CollectionResult(warnings=["year conflict: openalex=2025 vs arxiv=2017"])
    restored_warn = CollectionResult.from_dict(with_warn.to_dict())
    assert restored_warn.warnings == with_warn.warnings

    merged = plain.merge(with_warn)
    assert merged.warnings == with_warn.warnings
