"""Offline cassette-replay integration tests for the Crossref source.

Replays VCR-style JSON cassettes under ``tests/cassettes/`` (record/replay
without touching the live API), matching the existing source adapters'
pattern (``tests/integration/test_sources.py``).  Marked ``network`` +
``integration`` for consistency with the rest of the suite.
"""

from __future__ import annotations

import pytest

from academic_intelligence.core.models import Paper
from academic_intelligence.sources.crossref import CrossrefSource
from tests.cassette_replay import install_cassette

pytestmark = [pytest.mark.integration, pytest.mark.network]


class TestCrossrefCassettes:
    """Crossref adapter against recorded API responses."""

    @pytest.mark.asyncio
    async def test_get_paper_by_doi_cassette(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "crossref_doi")
        source = CrossrefSource(mailto="test@example.com")
        try:
            paper = await source.get_paper_by_doi("10.1038/s41586-025-09422-z")
            assert paper is not None
            assert "DeepSeek-R1" in paper.title
            assert paper.venue == "Nature"
            assert paper.publisher == "Springer Nature"
            assert paper.year == 2025
            assert paper.citations == 128
            assert paper.evidence_list[0].source.value == "crossref"
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_search_cassette(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "crossref_search")
        source = CrossrefSource(mailto="test@example.com")
        try:
            papers = await source.search_papers("deep learning transformers", limit=5)
            assert len(papers) > 0
            assert all(isinstance(p, Paper) for p in papers)
            assert all(p.evidence_list[0].source.value == "crossref" for p in papers)
        finally:
            await source.close()
