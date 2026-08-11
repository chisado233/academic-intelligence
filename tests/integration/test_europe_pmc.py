"""Network integration tests for the Europe PMC source.

Uses offline cassette replay (see ``tests/cassettes/europe_pmc_search.json``)
so the tests do not call the live API.  Marked ``network`` + ``integration``
for filtering.
"""

from __future__ import annotations

import pytest

from academic_intelligence.core.models import Paper
from academic_intelligence.sources.europe_pmc import EuropePmcSource
from tests.cassette_replay import install_cassette

pytestmark = [pytest.mark.integration, pytest.mark.network]


class TestEuropePmcSource:
    """Europe PMC source integration tests (REST cassette)."""

    @pytest.mark.asyncio
    async def test_search_papers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "europe_pmc_search")
        source = EuropePmcSource()
        try:
            papers = await source.search_papers("deepseek", limit=3)
            assert len(papers) > 0
            assert len(papers) <= 3
            assert all(isinstance(p, Paper) for p in papers)
            assert all(p.title for p in papers)
            assert all(
                p.primary_evidence is not None
                and p.primary_evidence.source.value == "europe_pmc"
                for p in papers
            )
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_paper_by_doi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "europe_pmc_search")
        source = EuropePmcSource()
        try:
            paper = await source.get_paper_by_doi("10.1038/s41586-020-2649-2")
            assert paper is not None
            assert paper.doi == "10.1038/s41586-020-2649-2"
            assert paper.pmid == "32939066"
            assert paper.title is not None
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_paper_by_pmid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "europe_pmc_search")
        source = EuropePmcSource()
        try:
            paper = await source.get_paper_by_pmid("33033895")
            assert paper is not None
            assert paper.pmid == "33033895"
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_fulltext_oa(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "europe_pmc_search")
        source = EuropePmcSource()
        try:
            paper = await source.get_paper_by_doi("10.1038/s41586-020-2649-2")
            assert paper is not None
            xml = await source.get_fulltext(paper)
            assert xml is not None
            assert "<article" in xml or "article" in xml.lower()
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_fulltext_non_oa_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_cassette(monkeypatch, "europe_pmc_search")
        source = EuropePmcSource()
        try:
            paper = await source.get_paper_by_pmid("33033895")
            assert paper is not None
            # isOpenAccess=N on the record: no request is made, returns None.
            assert await source.get_fulltext(paper) is None
        finally:
            await source.close()
