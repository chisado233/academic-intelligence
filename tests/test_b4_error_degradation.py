"""B4: error handling and graceful degradation (3A v2 §11.2).

- a single failing source never blocks the other sources;
- when every source fails, the raised AllSourcesFailedError carries the
  per-source failure reasons.
"""

from __future__ import annotations

from typing import Optional

import pytest

from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.exceptions import AllSourcesFailedError
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.sources.base import BaseSource


def _ev() -> Evidence:
    return Evidence(source=SourceType.OPENALEX, source_url="https://e.com", confidence=0.8)


class _FlakySource(BaseSource):
    name = "flaky"
    source_type = SourceType.OPENALEX

    def __init__(self, fail_with: Optional[str] = None) -> None:
        self.fail_with = fail_with

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        return [Paper(title=query, authors=["A"], evidence=_ev())]

    async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        return Paper(title=doi, authors=["A"], evidence=_ev())

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        return [Paper(title=author_name, authors=["A"], evidence=_ev())]

    async def get_author_profile(self, author_name: str) -> Optional[Author]:
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        return Author(name=author_name, evidence=_ev())

    async def get_citations(self, paper_id: str) -> list[Citation]:
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        return [Citation(citing_paper_id="c1", cited_paper_id=paper_id, evidence=_ev())]

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_single_source_failure_does_not_block_others() -> None:
    good = _FlakySource()
    bad = _FlakySource(fail_with="connection reset")
    collector = MultiSourceCollector(config=Config(), sources=[good, bad])
    result = await collector.collect("query")
    assert len(result.papers) == 1
    assert result.errors
    assert any("flaky" in e and "connection reset" in e for e in result.errors)
    stats = result.stats
    assert stats["requests_success"] == 1
    assert stats["requests_failed"] == 1


@pytest.mark.asyncio
async def test_all_sources_failed_carries_per_source_reasons() -> None:
    a = _FlakySource(fail_with="boom alpha")
    b = _FlakySource(fail_with="boom beta")
    collector = MultiSourceCollector(config=Config(), sources=[a, b])
    with pytest.raises(AllSourcesFailedError) as excinfo:
        await collector.collect("q")
    exc = excinfo.value
    assert exc.sources_attempted == ["flaky", "flaky"]
    assert "flaky" in exc.failures
    message = str(exc)
    assert "boom" in message
    assert "flaky:" in message


@pytest.mark.asyncio
async def test_all_sources_failed_reasons_from_distinct_sources() -> None:
    class _SourceA(BaseSource):
        name = "alpha"
        source_type = SourceType.OPENALEX

        async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
            raise RuntimeError("alpha exploded")

        async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
            return None

        async def get_author_papers(self, author_name: str) -> list[Paper]:
            return []

        async def get_author_profile(self, author_name: str) -> Optional[Author]:
            return None

        async def get_citations(self, paper_id: str) -> list[Citation]:
            return []

    class _SourceB(BaseSource):
        name = "beta"
        source_type = SourceType.OPENALEX

        async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
            raise RuntimeError("beta exploded")

        async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
            return None

        async def get_author_papers(self, author_name: str) -> list[Paper]:
            return []

        async def get_author_profile(self, author_name: str) -> Optional[Author]:
            return None

        async def get_citations(self, paper_id: str) -> list[Citation]:
            return []

    collector = MultiSourceCollector(config=Config(), sources=[_SourceA(), _SourceB()])
    with pytest.raises(AllSourcesFailedError) as excinfo:
        await collector.collect("q")
    exc = excinfo.value
    assert set(exc.failures) == {"alpha", "beta"}
    assert "alpha exploded" in exc.failures["alpha"]
    assert "beta exploded" in exc.failures["beta"]
    message = str(exc)
    assert "alpha" in message and "beta" in message


def test_all_sources_failed_exception_shape() -> None:
    exc = AllSourcesFailedError(
        "all failed",
        query="q",
        sources_attempted=["a", "b"],
        failures={"a": "reason-a"},
    )
    assert exc.failures == {"a": "reason-a"}
    assert "all failed" in str(exc)
    assert "reason-a" in str(exc)

    # failures is optional for backwards compatibility
    plain = AllSourcesFailedError("m", query="q", sources_attempted=["x"])
    assert plain.failures == {}
    assert "m" in str(plain)


class _EmptySource(BaseSource):
    """A source that always succeeds but never finds anything."""

    name = "empty"
    source_type = SourceType.OPENALEX

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return []

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        return []

    async def get_author_profile(self, author_name: str) -> Author | None:
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return []


class _FoundSource(_EmptySource):
    """A source that succeeds and returns one paper."""

    name = "found"

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return [Paper(title="Found", authors=["A"], evidence=_ev())]

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return Paper(title="Found", authors=["A"], evidence=_ev())


@pytest.mark.asyncio
async def test_collection_empty_flag_on_successful_zero_result() -> None:
    """I-13: a successful collection that finds nothing is flagged.

    Garbage input that contains no DOI is a legitimate search; when every
    source succeeds but returns nothing, ``result.stats["empty"]`` lets
    callers tell "nothing matched" apart from "sources failed".
    """
    collector = MultiSourceCollector(config=Config(), sources=[_EmptySource()])
    result = await collector.collect_paper("complete nonsense")
    assert result.papers == []
    assert result.stats["sources_used"] == ["empty"]
    assert result.stats.get("empty") is True

    found = MultiSourceCollector(config=Config(), sources=[_FoundSource()])
    result2 = await found.collect_paper("complete nonsense")
    assert len(result2.papers) == 1
    assert result2.stats.get("empty") is False
