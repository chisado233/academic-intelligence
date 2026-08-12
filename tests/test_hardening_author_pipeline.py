"""Regression coverage for author identity handling in the public pipeline."""

from __future__ import annotations

import pytest

from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.sources.base import BaseSource


class _ProfileSource(BaseSource):
    capabilities = {
        "search_papers": True,
        "get_paper_by_doi": True,
        "get_author_papers": True,
        "get_author_profile": True,
        "get_citations": True,
    }

    def __init__(self, name: str, author: Author) -> None:
        self.name = name
        self.source_type = SourceType.OPENALEX
        self._author = author

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return []

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        return []

    async def get_author_profile(self, author_name: str) -> Author | None:
        return self._author

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return []


def _author(orcid: str, affiliation: str, source_url: str) -> Author:
    return Author(
        name="Wei Zhang",
        orcid=orcid,
        affiliation=affiliation,
        interests=[affiliation],
        evidence=Evidence(
            source=SourceType.OPENALEX,
            source_url=source_url,
        ),
    )


@pytest.mark.asyncio
async def test_public_collection_keeps_same_name_conflicting_ids_distinct() -> None:
    first = _ProfileSource(
        "source-a",
        _author("0000-0001-2345-6789", "computer vision", "https://a.test"),
    )
    second = _ProfileSource(
        "source-b",
        _author("0000-0002-1825-0097", "biomedicine", "https://b.test"),
    )
    collector = MultiSourceCollector(
        Config(sources=["arxiv"]),
        sources=[first, second],
    )

    result = await collector.collect_author_papers("Wei Zhang")

    assert len(result.authors) == 2
    assert {author.orcid for author in result.authors} == {
        "0000-0001-2345-6789",
        "0000-0002-1825-0097",
    }


@pytest.mark.asyncio
async def test_public_collection_merges_exact_name_and_affiliation_across_id_systems() -> None:
    first = _ProfileSource(
        "source-a",
        Author(
            name="Geoffrey E. Hinton",
            openalex_id="A5023888391",
            affiliation="University of Toronto",
            evidence=Evidence(
                source=SourceType.OPENALEX,
                source_url="https://openalex.org/A5023888391",
            ),
        ),
    )
    second = _ProfileSource(
        "source-b",
        Author(
            name="Geoffrey E. Hinton",
            semantic_scholar_id="1695689",
            affiliation="University of Toronto",
            evidence=Evidence(
                source=SourceType.SEMANTIC_SCHOLAR,
                source_url="https://semanticscholar.org/author/1695689",
            ),
        ),
    )
    collector = MultiSourceCollector(
        Config(sources=["arxiv"]),
        sources=[first, second],
    )

    result = await collector.collect_author_papers("Geoffrey Hinton")

    # 2026-08-12 决策：采集管道不做作者自动合并（阈值合并不可靠）——
    # 同名不同 ID 的记录保留为独立作者，身份判断交给 agent/人工。
    assert len(result.authors) == 2
    assert {a.openalex_id for a in result.authors} == {"A5023888391", None}
    assert {a.semantic_scholar_id for a in result.authors} == {"1695689", None}


@pytest.mark.asyncio
async def test_public_collection_never_merges_conflicting_same_type_authority_ids() -> None:
    first = _ProfileSource(
        "source-a",
        _author(
            "0000-0001-2345-6789",
            "University of Toronto",
            "https://a.test",
        ),
    )
    second = _ProfileSource(
        "source-b",
        _author(
            "0000-0002-1825-0097",
            "University of Toronto",
            "https://b.test",
        ),
    )
    collector = MultiSourceCollector(
        Config(sources=["arxiv"]),
        sources=[first, second],
    )

    result = await collector.collect_author_papers("Wei Zhang")

    assert len(result.authors) == 2
