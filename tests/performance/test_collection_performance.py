"""Performance tests for collection and deduplication."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.models import Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from tests.cassette_replay import install_cassette


pytestmark = [pytest.mark.performance, pytest.mark.slow]


def _ev(source: SourceType = SourceType.OPENALEX, conf: float = 0.8) -> Evidence:
    return Evidence(
        source=source,
        source_url="https://example.com",
        confidence=conf,
    )


class TestCollectionPerformance:
    """Collection performance tests."""

    @pytest.mark.asyncio
    async def test_collection_speed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Single-source collection should finish quickly offline."""
        install_cassette(monkeypatch, "openalex_search")
        config = Config(
            sources=["openalex"],
            storage_type="sqlite",
            storage_path=str(tmp_path / "perf.db"),
            cache_enabled=False,
        )
        ai = AcademicIntelligence(config)
        try:
            start = time.perf_counter()
            result = await ai.collect_author_papers(
                "Test Author",
                sources=["openalex"],
            )
            elapsed = time.perf_counter() - start
            assert elapsed < 30, f"collection took {elapsed:.2f}s"
            assert isinstance(result.papers, list)
        finally:
            await ai.close()

    def test_deduplication_speed(self) -> None:
        """Deduplicate ~1000 papers within a tight budget."""
        dedup = Deduplicator()

        # 500 papers: 400 unique + 100 extras collapsing to 25 titles
        papers: list[Paper] = []
        for i in range(400):
            papers.append(
                Paper(
                    title=f"Unique Paper {i}",
                    authors=["A"],
                    year=2000 + (i % 20),
                    evidence=_ev(),
                )
            )
        for i in range(100):
            papers.append(
                Paper(
                    title=f"Duplicate Paper {i % 25}",
                    authors=["A"],
                    year=2010,
                    evidence=_ev(SourceType.SEMANTIC_SCHOLAR, 0.7),
                )
            )

        start = time.perf_counter()
        unique = dedup.deduplicate_papers(papers)
        elapsed = time.perf_counter() - start

        # O(n²) title/author similarity; budget leaves headroom for CI load
        assert elapsed < 5, f"dedup took {elapsed:.2f}s"
        assert len(unique) == 425

    def test_deduplication_identical_doi_cluster(self) -> None:
        """Many DOI-identical records collapse to one."""
        dedup = Deduplicator()
        papers = [
            Paper(
                title=f"Same Work Variant {i}",
                authors=["A"],
                year=2017,
                doi="10.5555/3295222.3295349",
                evidence=_ev(
                    SourceType.OPENALEX if i % 2 == 0 else SourceType.SEMANTIC_SCHOLAR
                ),
            )
            for i in range(100)
        ]
        start = time.perf_counter()
        unique = dedup.deduplicate_papers(papers)
        elapsed = time.perf_counter() - start
        assert elapsed < 2
        assert len(unique) == 1
