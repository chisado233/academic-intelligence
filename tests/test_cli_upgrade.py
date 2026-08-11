"""CLI integration tests for the crawler-upgrade top-level command tree (IM-1).

Covers the commands delivered by fix order A's CLI surface work:
- ``paper fulltext <id>`` — M15 id normalization + full-text pipeline.
- ``paper web crawl <url>`` — crawl outcome rendering (ok / blocked).
- ``paper pdf parse <file>`` — local PDF segment extraction to JSONL.
- ``paper sources`` matrix / ``paper sources status`` / ``paper budget``.
- ``paper --help`` exposes the whole tree.

All tests are offline: adapter methods and the pipeline/web crawler are
monkeypatched; ``paper pdf parse`` uses the checked-in fixture PDF.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from academic_intelligence import AcademicIntelligence
from academic_intelligence.cli import app
from academic_intelligence.core.models import Paper
from academic_intelligence.core.types import Config
from academic_intelligence.fulltext.models import FullText, Segment
from academic_intelligence.fulltext.pipeline import FulltextPipeline
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.webcrawler.crawler import WebCrawler
from academic_intelligence.webcrawler.models import CrawlStatus, WebDocument

runner = CliRunner()

_FIXTURE_PDF = Path(__file__).resolve().parent / "fixtures" / "sample_paper.pdf"


def _arxiv_paper(arxiv_id: str = "2501.12948") -> Paper:
    return Paper(
        id=arxiv_id,
        title="DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via RL",
        authors=[],
        year=2025,
        arxiv_id=arxiv_id,
        doi="10.48550/arxiv.2501.12948",
    )


async def _stored_full_text(db_path: Path) -> FullText | None:
    cfg = Config(storage_type="sqlite", storage_path=str(db_path))
    async with AcademicIntelligence(cfg) as ai:
        return await ai.storage.get_full_text("2501.12948")


# ---------------------------------------------------------------------------
# Command tree completeness (IM-1)
# ---------------------------------------------------------------------------


def test_root_help_exposes_upgrade_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("fulltext", "web", "pdf", "budget", "sources"):
        assert command in result.stdout


# ---------------------------------------------------------------------------
# paper fulltext <id> (M15 id normalization)
# ---------------------------------------------------------------------------


def test_fulltext_arxiv_id_resolves_and_persists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ft.db"

    async def fake_get_by_id(self: ArxivSource, arxiv_id: str) -> Paper:
        assert arxiv_id == "2501.12948"
        return _arxiv_paper()

    async def fake_fetch(
        self: FulltextPipeline,
        paper: Paper,
        sources: tuple[str, ...] = ("unpaywall", "core", "arxiv"),
        persist: bool = True,
    ) -> FullText:
        fulltext = FullText(
            paper_id=paper.id or "x",
            source="arxiv",
            oa_license=None,
            paragraph_count=3,
            segments=[
                Segment(text="Reinforcement learning reasoning", page=1),
                Segment(text="Second paragraph", page=1),
                Segment(text="Third paragraph", page=2),
            ],
        )
        if persist and self.storage is not None:
            await self.storage.save_full_text(fulltext)
        return fulltext

    monkeypatch.setattr(ArxivSource, "get_paper_by_arxiv_id", fake_get_by_id)
    monkeypatch.setattr(FulltextPipeline, "fetch", fake_fetch)
    result = runner.invoke(
        app,
        [
            "fulltext", "2501.12948", "--sources", "arxiv", "--persist",
            "--storage-path", str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "3 paragraphs" in result.output

    stored = asyncio.run(_stored_full_text(db))
    assert stored is not None
    assert stored.paragraph_count == 3
    assert len(stored.segments) == 3


def test_fulltext_doi_lookup_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A DOI input resolves through the metadata sources (crossref first)."""
    from academic_intelligence.sources.crossref import CrossrefSource

    called: list[str] = []

    async def fake_crossref_get(self: CrossrefSource, doi: str) -> Paper:
        called.append("crossref")
        return Paper(id="c-1", title="Crossref paper", doi=doi)

    async def fake_fetch(
        self: FulltextPipeline,
        paper: Paper,
        sources: tuple[str, ...] = ("unpaywall", "core", "arxiv"),
        persist: bool = True,
    ) -> FullText:
        return FullText(
            paper_id=paper.id or "x",
            source="unpaywall",
            paragraph_count=1,
            segments=[Segment(text="text", page=1)],
        )

    monkeypatch.setattr(CrossrefSource, "get_paper_by_doi", fake_crossref_get)
    monkeypatch.setattr(FulltextPipeline, "fetch", fake_fetch)
    result = runner.invoke(
        app,
        [
            "fulltext", "10.1038/s41586-025-09422-z", "--sources", "unpaywall",
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert called == ["crossref"]


def test_fulltext_unresolvable_identifier_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["fulltext", "garbage-not-an-id", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 2
    assert "无法识别" in result.output


def test_fulltext_unknown_source_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fulltext", "2501.12948", "--sources", "bogus",
            "--storage-path", str(tmp_path / "t.db"),
        ],
    )
    assert result.exit_code == 2
    assert "unknown fulltext source" in result.output


# ---------------------------------------------------------------------------
# paper web crawl <url>
# ---------------------------------------------------------------------------


def test_web_crawl_ok_renders_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_crawl(
        self: WebCrawler,
        url: str,
        schema: object = None,
        *,
        use_browser: bool = False,
    ) -> WebDocument:
        return WebDocument(
            url=url,
            status=CrawlStatus.OK,
            title="Paper Title",
            content="extracted body text",
            extracted={"title": "Paper Title", "authors": ["A", "B"]},
        )

    monkeypatch.setattr(WebCrawler, "crawl", fake_crawl)
    result = runner.invoke(
        app,
        ["web", "crawl", "https://example.com/paper"],
    )
    assert result.exit_code == 0, result.output
    assert "Crawled https://example.com/paper" in result.output
    assert "Paper Title" in result.output


def test_web_crawl_blocked_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_crawl(
        self: WebCrawler,
        url: str,
        schema: object = None,
        *,
        use_browser: bool = False,
    ) -> WebDocument:
        return WebDocument(
            url=url,
            status=CrawlStatus.BLOCKED,
            metadata={"diagnostic": "robots.txt pre-check denied crawling"},
        )

    monkeypatch.setattr(WebCrawler, "crawl", fake_crawl)
    result = runner.invoke(app, ["web", "crawl", "https://example.com/paper"])
    assert result.exit_code == 2
    assert "Blocked" in result.output
    assert "robots.txt" in result.output


def test_web_crawl_writes_output_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = tmp_path / "crawl.json"

    async def fake_crawl(
        self: WebCrawler,
        url: str,
        schema: object = None,
        *,
        use_browser: bool = False,
    ) -> WebDocument:
        return WebDocument(url=url, status=CrawlStatus.OK, title="T", content="c")

    monkeypatch.setattr(WebCrawler, "crawl", fake_crawl)
    result = runner.invoke(
        app,
        ["web", "crawl", "https://example.com/paper", "--output", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8"))["url"] == "https://example.com/paper"


def test_web_crawl_extract_schema_loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({"fields": [{"field": "title", "selector": "h1", "mode": "css"}]}),
        encoding="utf-8",
    )
    seen: list[object] = []

    async def fake_crawl(
        self: WebCrawler,
        url: str,
        schema: object = None,
        *,
        use_browser: bool = False,
    ) -> WebDocument:
        seen.append(schema)
        return WebDocument(url=url, status=CrawlStatus.OK, title="T")

    monkeypatch.setattr(WebCrawler, "crawl", fake_crawl)
    result = runner.invoke(
        app,
        ["web", "crawl", "https://example.com/paper", "--extract", str(schema_path)],
    )
    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    assert getattr(seen[0], "fields", None) is not None
    assert seen[0].fields[0].field == "title"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# paper pdf parse <file>
# ---------------------------------------------------------------------------


def test_pdf_parse_writes_jsonl(tmp_path: Path) -> None:
    out = tmp_path / "parsed.jsonl"
    result = runner.invoke(
        app,
        ["pdf", "parse", str(_FIXTURE_PDF), "--output", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) >= 5
    assert all("text" in line and "page" in line for line in lines)


def test_pdf_parse_missing_file_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["pdf", "parse", str(tmp_path / "nope.pdf")])
    assert result.exit_code == 2
    assert "not a file" in result.output


# ---------------------------------------------------------------------------
# paper sources / sources status / budget
# ---------------------------------------------------------------------------


def test_sources_status_renders_matrix_and_budgets(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["sources", "status", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 0, result.output
    assert "capability matrix" in result.output
    assert "arxiv" in result.output
    assert "Budget quotas" in result.output
    assert "openalex" in result.output


def test_budget_renders_quota_table(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["budget", "--storage-path", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 0, result.output
    assert "Budget quotas" in result.output
    assert "crossref" in result.output
    assert "semantic_scholar" in result.output
