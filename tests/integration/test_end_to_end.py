"""End-to-end integration tests for AcademicIntelligence facade.

Multi-source collection is exercised against offline cassettes so the
full pipeline (fetch → enrich → dedup → validate) runs without live APIs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.types import Config
from tests.cassette_replay import install_merged_cassettes


pytestmark = [pytest.mark.integration, pytest.mark.network, pytest.mark.slow]


class TestEndToEnd:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_collect_author_papers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Collect author papers from OpenAlex + Semantic Scholar."""
        install_merged_cassettes(
            monkeypatch, ["openalex_search", "semantic_scholar_search"]
        )
        config = Config(
            sources=["openalex", "semantic_scholar"],
            storage_type="sqlite",
            storage_path=str(tmp_path / "e2e.db"),
            cache_enabled=False,
            serpapi_key=None,
        )
        ai = AcademicIntelligence(config)
        try:
            result = await ai.collect_author_papers(
                "Geoffrey Hinton",
                sources=["openalex", "semantic_scholar"],
            )
            assert len(result.papers) > 0
            sources_used = result.stats.get("sources_used") or []
            assert len(sources_used) > 0
            assert result.stats.get("paper_count", 0) == len(result.papers)
        finally:
            await ai.close()

    @pytest.mark.asyncio
    async def test_multi_source_deduplication(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Multi-source search should dedupe shared papers (DOI/title)."""
        install_merged_cassettes(
            monkeypatch, ["openalex_search", "semantic_scholar_search"]
        )
        config = Config(
            sources=["openalex", "semantic_scholar"],
            storage_type="sqlite",
            storage_path=str(tmp_path / "e2e_dedup.db"),
            cache_enabled=False,
        )
        ai = AcademicIntelligence(config)
        try:
            result = await ai.collect_paper(
                "attention is all you need",
                sources=["openalex", "semantic_scholar"],
            )
            raw = result.stats.get("items_collected", 0)
            assert len(result.papers) > 0
            assert len(result.papers) <= raw or raw == 0
            used = result.stats.get("sources_used") or []
            assert len(used) >= 1
            titles = [p.title.lower() for p in result.papers]
            assert any("attention" in t for t in titles)
        finally:
            await ai.close()

    @pytest.mark.asyncio
    async def test_collect_paper_by_doi(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """DOI lookup across OpenAlex + Semantic Scholar."""
        install_merged_cassettes(
            monkeypatch, ["openalex_search", "semantic_scholar_search"]
        )
        config = Config(
            sources=["openalex", "semantic_scholar"],
            storage_type="json",
            storage_path=str(tmp_path / "e2e_json"),
            cache_enabled=False,
        )
        ai = AcademicIntelligence(config)
        try:
            result = await ai.collect_paper(
                "10.1038/nature14539",
                sources=["openalex", "semantic_scholar"],
            )
            assert len(result.papers) >= 1
            assert len(result.papers) <= 2
            assert any(
                p.doi and "nature14539" in p.doi for p in result.papers
            ) or any("deep learning" in p.title.lower() for p in result.papers)
        finally:
            await ai.close()

    @pytest.mark.asyncio
    async def test_persist_and_query(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Collection with persist=True should write to storage."""
        install_merged_cassettes(monkeypatch, ["openalex_search"])
        config = Config(
            sources=["openalex"],
            storage_type="sqlite",
            storage_path=str(tmp_path / "persist.db"),
            cache_enabled=False,
        )
        ai = AcademicIntelligence(config)
        try:
            result = await ai.collect_paper(
                "machine learning",
                sources=["openalex"],
                persist=True,
                limit=5,
            )
            assert len(result.papers) > 0
            stats = await ai.get_stats()
            assert stats["total_papers"] >= 1
            found = await ai.query_papers(keyword="Machine", limit=10)
            assert isinstance(found, list)
        finally:
            await ai.close()
