"""Network integration tests for data sources.

Uses offline cassette replay (see ``tests/cassettes/``) so tests do not
call live third-party APIs.  Marked ``network`` + ``integration`` for
filtering.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx
import pytest

from academic_intelligence.core.models import Paper
from academic_intelligence.sources.google_scholar import GoogleScholarSource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource
from academic_intelligence.utils.http import HTTPClient
from tests.cassette_replay import install_cassette


pytestmark = [pytest.mark.integration, pytest.mark.network]


class TestGoogleScholarSource:
    """Google Scholar source integration tests (SerpAPI cassette)."""

    @pytest.mark.asyncio
    async def test_search_papers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test searching papers via Google Scholar / SerpAPI."""
        install_cassette(monkeypatch, "google_scholar_search")
        source = GoogleScholarSource(serpapi_key="test_key")
        try:
            papers = await source.search_papers("machine learning", limit=5)
            assert len(papers) > 0
            assert len(papers) <= 5
            assert all(isinstance(p, Paper) for p in papers)
            assert all(p.title for p in papers)
            assert all(p.evidence.source.value == "google_scholar" for p in papers)
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_paper_by_doi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test retrieving a paper by DOI through Scholar search."""
        install_cassette(monkeypatch, "google_scholar_search")
        source = GoogleScholarSource(serpapi_key="test_key")
        try:
            paper = await source.get_paper_by_doi("10.1038/nature14539")
            assert paper is not None
            assert paper.title is not None
            assert "deep learning" in paper.title.lower()
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_rate_limit_handling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rapid concurrent searches should not all fail (cassette + retries)."""
        install_cassette(monkeypatch, "google_scholar_search")
        source = GoogleScholarSource(serpapi_key="test_key")
        try:
            tasks = [source.search_papers("test", limit=1) for _ in range(10)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            successes = [r for r in results if not isinstance(r, Exception)]
            assert any(successes), f"all failed: {results[:3]}"
            assert all(isinstance(p, Paper) for batch in successes for p in batch)
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self) -> None:
        """Without SerpAPI key, search should raise AuthenticationError."""
        from academic_intelligence.core.exceptions import AuthenticationError

        source = GoogleScholarSource(serpapi_key=None)
        try:
            with pytest.raises(AuthenticationError):
                await source.search_papers("anything", limit=1)
        finally:
            await source.close()


class TestOpenAlexSource:
    """OpenAlex source integration tests (public API cassette)."""

    @pytest.mark.asyncio
    async def test_search_papers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "openalex_search")
        source = OpenAlexSource(email="test@example.com")
        try:
            papers = await source.search_papers("machine learning", limit=5)
            assert len(papers) > 0
            assert all(isinstance(p, Paper) for p in papers)
            assert all(p.evidence.source.value == "openalex" for p in papers)
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_paper_by_doi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "openalex_search")
        source = OpenAlexSource()
        try:
            paper = await source.get_paper_by_doi("10.1038/nature14539")
            assert paper is not None
            assert paper.title is not None
            assert paper.doi is not None
            assert "nature14539" in paper.doi
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_author_papers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "openalex_search")
        source = OpenAlexSource()
        try:
            papers = await source.get_author_papers("Geoffrey Hinton")
            assert len(papers) > 0
            assert all(isinstance(p, Paper) for p in papers)
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_author_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "openalex_search")
        source = OpenAlexSource()
        try:
            author = await source.get_author_profile("Geoffrey Hinton")
            assert author is not None
            assert "Hinton" in author.name
        finally:
            await source.close()


class TestSemanticScholarSource:
    """Semantic Scholar source integration tests (Graph API cassette)."""

    @pytest.mark.asyncio
    async def test_search_papers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "semantic_scholar_search")
        source = SemanticScholarSource(api_key="test_key")
        try:
            papers = await source.search_papers("machine learning", limit=5)
            assert len(papers) > 0
            assert all(isinstance(p, Paper) for p in papers)
            assert all(p.evidence.source.value == "semantic_scholar" for p in papers)
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_paper_by_doi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "semantic_scholar_search")
        source = SemanticScholarSource()
        try:
            paper = await source.get_paper_by_doi("10.1038/nature14539")
            assert paper is not None
            assert paper.title is not None
            assert paper.doi == "10.1038/nature14539"
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_get_author_papers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_cassette(monkeypatch, "semantic_scholar_search")
        source = SemanticScholarSource()
        try:
            papers = await source.get_author_papers("Geoffrey Hinton")
            assert len(papers) > 0
        finally:
            await source.close()

    @pytest.mark.asyncio
    async def test_rate_limit_status_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 429 from Semantic Scholar should surface as RateLimitError."""
        from academic_intelligence.core.exceptions import RateLimitError

        async def _always_429(
            self: HTTPClient,
            url: str,
            headers: Optional[Dict[str, str]] = None,
            params: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
        ) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(
                429,
                headers={"Retry-After": "1"},
                text="Too Many Requests",
                request=request,
            )

        monkeypatch.setattr(HTTPClient, "get", _always_429)
        source = SemanticScholarSource()
        try:
            with pytest.raises(RateLimitError):
                await source.search_papers("anything", limit=1)
        finally:
            await source.close()
