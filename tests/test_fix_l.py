"""FIX-L ticket tests (B7-P29 round 11 defects).

- F1: title-similarity merges are forbidden when both records carry the same
  ID type (DOI / PMID / arXiv ID) with *different* values — ID evidence wins
  over title similarity. This is a root cause of the mega-cluster over-merge
  (e.g. long titles differing only by a numeric suffix "...number 5" vs
  "...number 50").
- F2: pathological mega-clusters whose merged evidence exceeds the model's
  500-entry ``evidence_list`` cap degrade gracefully — the list is truncated
  to the highest-confidence 500 entries instead of raising a bare Pydantic
  ValidationError that takes the whole collect pipeline down.
- F3: arXiv ``journal_ref`` venue parsing strips volume / issue / pages /
  year / ISSN noise, keeping the bare journal name ("Medical Image Analysis,
  Volume 71, 2021, 102062, ISSN 1361-8415" -> "Medical Image Analysis").
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_intelligence.core.models import Author, AuthorRef, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.sources.arxiv import ArxivSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev(
    source: SourceType = SourceType.OPENALEX,
    conf: float = 0.8,
    sid: str | None = None,
) -> Evidence:
    return Evidence(
        source=source,
        source_url=f"https://{source.value}/record",
        confidence=conf,
        source_id=sid,
        collected_at=datetime.now(timezone.utc),
    )


def _arxiv_feed(journal_ref: str | None = None) -> str:
    """Build a single-entry arXiv Atom feed, optionally with a journal_ref."""
    jr = f"<arxiv:journal_ref>{journal_ref}</arxiv:journal_ref>" if journal_ref else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <published>2023-01-01T00:00:00Z</published>
    <title>A Test Paper Title</title>
    <summary>A short abstract.</summary>
    <author><name>Jane Doe</name></author>
    <link href="http://arxiv.org/abs/2301.00001v1" rel="alternate" type="text/html"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    {jr}
  </entry>
</feed>"""


async def _parse_single(journal_ref: str | None = None) -> Paper:
    http = MagicMock()
    http.get = AsyncMock(return_value=MagicMock(status_code=200, text=_arxiv_feed(journal_ref)))
    source = ArxivSource(http_client=http, min_interval_seconds=0.01)
    papers = await source.search_papers("test", limit=10)
    assert len(papers) == 1
    return papers[0]


# ---------------------------------------------------------------------------
# F1: ID conflict guard on title-similarity merges
# ---------------------------------------------------------------------------


def test_fix_l_f1_numeric_suffix_different_doi_not_merged() -> None:
    """Long titles differing only by a numeric suffix, with different DOIs,
    must NOT be fused by title similarity (ID evidence wins)."""
    a = Paper(
        title="A comprehensive survey of deep learning methods for medical image analysis number 5",
        authors=[AuthorRef(name="A", position=1)],
        year=2020,
        doi="10.5555/number.5",
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-5")],
    )
    b = Paper(
        title="A comprehensive survey of deep learning methods for medical image analysis number 50",
        authors=[AuthorRef(name="B", position=1)],
        year=2020,
        doi="10.5555/number.50",
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-50")],
    )
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 2


def test_fix_l_f1_same_title_same_doi_still_merges() -> None:
    """The guard must not touch exact ID matches: same title + same DOI
    still collapses to a single record."""
    a = Paper(
        title="Attention Is All You Need",
        authors=[AuthorRef(name="A", position=1)],
        year=2017,
        doi="10.5555/3295222.3295349",
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-1")],
    )
    b = Paper(
        title="Attention Is All You Need",
        authors=[AuthorRef(name="B", position=1)],
        year=2017,
        doi="10.5555/3295222.3295349",
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-1")],
    )
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 2


def test_fix_l_f1_one_sided_doi_still_merges_by_title() -> None:
    """A DOI on only one side is not a conflict: cross-id records
    (arxiv-only + DOI-only) with highly similar titles must keep merging."""
    arxiv_only = Paper(
        title="Deep Learning",
        authors=[AuthorRef(name="LeCun", position=1)],
        year=2015,
        arxiv_id="1404.1234",
        evidence_list=[_ev(SourceType.ARXIV, 0.92, "1404.1234")],
    )
    doi_only = Paper(
        title="Deep Learning",
        authors=[AuthorRef(name="Y. LeCun", position=1)],
        year=2015,
        doi="10.1038/nature14539",
        evidence_list=[_ev(SourceType.OPENALEX, 0.90, "10.1038/nature14539")],
    )
    assert len(Deduplicator().deduplicate_papers([arxiv_only, doi_only])) == 1


def test_fix_l_f1_one_side_doi_one_side_bare_title_merges() -> None:
    """A DOI on one side and none on the other must not block a title merge."""
    a = Paper(
        title="A Near Identical Title Shared By Two Records",
        authors=[AuthorRef(name="A", position=1)],
        year=2020,
        doi="10.5555/x.y",
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-1")],
    )
    b = Paper(
        title="A Near Identical Title Shared By Two Records",
        authors=[AuthorRef(name="B", position=1)],
        year=2020,
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-1")],
    )
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 2


def test_fix_l_f1_pmid_conflict_blocks_title_merge() -> None:
    """Both records with non-empty, different PMIDs must not fuse by title."""
    a = Paper(
        title="Same Title",
        authors=[AuthorRef(name="A", position=1)],
        year=2020,
        pmid="11111111",
        evidence_list=[_ev(SourceType.PUBMED, 0.92, "11111111")],
    )
    b = Paper(
        title="Same Title",
        authors=[AuthorRef(name="B", position=1)],
        year=2020,
        pmid="22222222",
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-2")],
    )
    assert len(Deduplicator().deduplicate_papers([a, b])) == 2


def test_fix_l_f1_arxiv_id_conflict_blocks_title_merge() -> None:
    """Both records with non-empty, different arXiv IDs must not fuse by
    title alone (two distinct preprints sharing a title are different works)."""
    a = Paper(
        title="A Generic Title Shared By Two Works",
        authors=[AuthorRef(name="A", position=1)],
        year=2020,
        arxiv_id="2001.00001",
        evidence_list=[_ev(SourceType.ARXIV, 0.92, "2001.00001")],
    )
    b = Paper(
        title="A Generic Title Shared By Two Works",
        authors=[AuthorRef(name="B", position=1)],
        year=2020,
        arxiv_id="2002.00002",
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-2")],
    )
    assert len(Deduplicator().deduplicate_papers([a, b])) == 2


def test_fix_l_f1_arxiv_version_suffix_not_a_conflict() -> None:
    """Normalized arXiv IDs strip the version suffix: v1/v2 records of the
    same paper are the same work, not an ID conflict."""
    a = Paper(
        title="The Same Arxiv Paper",
        authors=[AuthorRef(name="A", position=1)],
        year=2020,
        arxiv_id="2001.00001v1",
        evidence_list=[_ev(SourceType.ARXIV, 0.92, "2001.00001v1")],
    )
    b = Paper(
        title="The Same Arxiv Paper",
        authors=[AuthorRef(name="B", position=1)],
        year=2020,
        arxiv_id="2001.00001",
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-1")],
    )
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 2


# ---------------------------------------------------------------------------
# F2: mega-cluster evidence cap degradation
# ---------------------------------------------------------------------------


def test_fix_l_f2_mega_cluster_evidence_cap_degrades_gracefully() -> None:
    """600 records sharing title + DOI collapse into one mega-cluster; the
    merged evidence_list (600) exceeds the 500 cap and must be truncated to
    the highest-confidence entries instead of raising a Pydantic
    ValidationError."""
    papers = [
        Paper(
            title="The Same Exact Work",
            authors=[AuthorRef(name="A", position=1)],
            year=2020,
            doi="10.5555/3295222.3295349",
            evidence_list=[
                _ev(
                    SourceType.OPENALEX if i % 2 else SourceType.SEMANTIC_SCHOLAR,
                    0.8,
                    f"src-{i}",
                )
            ],
        )
        for i in range(600)
    ]
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers(papers)  # must not raise
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 500
    assert any("evidence truncated" in w for w in dedup.get_warnings())
    assert dedup.get_stats()["evidence_truncated"] == 1


def test_fix_l_f2_under_cap_no_truncation() -> None:
    """A large but legal cluster (evidence <= 500) is untouched: no warning,
    no truncation stat."""
    papers = [
        Paper(
            title="The Same Exact Work",
            authors=[AuthorRef(name="A", position=1)],
            year=2020,
            doi="10.5555/3295222.3295349",
            evidence_list=[_ev(SourceType.OPENALEX, 0.8, f"src-{i}")],
        )
        for i in range(400)
    ]
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers(papers)
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 400
    assert dedup.get_warnings() == []
    assert dedup.get_stats()["evidence_truncated"] == 0


def test_fix_l_f2_same_title_different_doi_no_mega_cluster() -> None:
    """F1 guard + F2 degradation: 600 records with the same title but distinct
    DOIs must no longer collapse into one mega-cluster (so no truncation
    fires and every record survives)."""
    papers = [
        Paper(
            title="A Long Shared Survey Title Across Many Works",
            authors=[AuthorRef(name="A", position=1)],
            year=2020,
            doi=f"10.5555/paper.{i}",
            evidence_list=[_ev(SourceType.OPENALEX, 0.8, f"oa-{i}")],
        )
        for i in range(600)
    ]
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers(papers)
    assert len(merged) == 600
    assert dedup.get_stats()["evidence_truncated"] == 0


def test_fix_l_f2_author_mega_cluster_evidence_cap() -> None:
    """Author fusion shares the same 500-entry evidence cap and degrades
    gracefully instead of raising."""
    authors = [
        Author(
            name="Ada Lovelace",
            evidence_list=[_ev(SourceType.OPENALEX, 0.8, f"a-{i}")],
        )
        for i in range(501)
    ]
    dedup = Deduplicator()
    merged = dedup.deduplicate_authors(authors)  # must not raise
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 500
    assert dedup.get_stats()["evidence_truncated"] == 1


@pytest.mark.asyncio
async def test_fix_l_f2_collect_pipeline_survives_mega_cluster() -> None:
    """End-to-end: the collect pipeline (cross-validate -> dedup -> enrich ->
    validate) must not be pierced by a bare ValidationError when a mega-
    cluster overflows the evidence cap; it returns a valid merged record."""
    from academic_intelligence.collectors.base import MultiSourceCollector
    from academic_intelligence.core.types import Config
    from academic_intelligence.sources.base import BaseSource

    class _MegaSource(BaseSource):
        name = "mega"
        source_type = SourceType.OPENALEX

        async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
            return [
                Paper(
                    title="The Same Exact Work",
                    authors=[AuthorRef(name="A", position=1)],
                    year=2020,
                    doi="10.5555/3295222.3295349",
                    evidence_list=[
                        _ev(SourceType.OPENALEX if i % 2 else SourceType.SEMANTIC_SCHOLAR, 0.8, f"s{i}")
                    ],
                )
                for i in range(501)
            ]

        async def get_paper_by_doi(self, doi: str) -> Paper | None:
            return None

        async def get_author_papers(self, author_name: str) -> list[Paper]:
            return []

        async def get_author_profile(self, author_name: str) -> Author | None:
            return None

        async def get_citations(self, paper_id: str) -> list[Citation]:
            return []

        async def close(self) -> None:
            pass

    collector = MultiSourceCollector(config=Config(), sources=[_MegaSource()])
    result = await collector.collect("same work")  # must not raise ValidationError
    assert result.errors == []
    assert len(result.papers) == 1
    assert len(result.papers[0].evidence_list) == 500


# ---------------------------------------------------------------------------
# F3: arXiv journal_ref venue cleaning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_l_f3_journal_ref_noise_stripped() -> None:
    """The P29 repro: a verbose journal_ref must reduce to the bare journal
    name."""
    paper = await _parse_single(
        "Medical Image Analysis, Volume 71, 2021, 102062, ISSN 1361-8415"
    )
    assert paper.venue == "Medical Image Analysis"


@pytest.mark.asyncio
async def test_fix_l_f3_no_journal_ref_falls_back_to_category() -> None:
    """Without a journal_ref the venue falls back to the primary arXiv
    category."""
    paper = await _parse_single(None)
    assert paper.venue == "arXiv:cs.CL"


@pytest.mark.asyncio
async def test_fix_l_f3_pure_journal_name_unchanged() -> None:
    """A clean journal_ref with no noise is preserved verbatim."""
    paper = await _parse_single("Nature")
    assert paper.venue == "Nature"


@pytest.mark.asyncio
async def test_fix_l_f3_abbrev_journal_with_volume_cleaned() -> None:
    """"Phys. Rev. Lett. 124, 244505 (2020)" keeps only the journal name."""
    paper = await _parse_single("Phys. Rev. Lett. 124, 244505 (2020)")
    assert paper.venue == "Phys. Rev. Lett."


def test_fix_l_f3_no_fake_venue_conflict_after_clean() -> None:
    """F3 end-to-end: a cleaned arXiv venue matches the OpenAlex venue, so
    the cross-ID fusion produces no fake venue-conflict warning (the P29
    noisy-journal_ref case)."""
    arxiv_p = Paper(
        title="Survey on Deep Learning in Medical Image Analysis",
        authors=[AuthorRef(name="A", position=1)],
        year=2021,
        venue="Medical Image Analysis",
        arxiv_id="1910.02923",
        evidence_list=[_ev(SourceType.ARXIV, 0.95, "1910.02923")],
    )
    oa_p = Paper(
        title="Survey on Deep Learning in Medical Image Analysis",
        authors=[AuthorRef(name="B", position=1)],
        year=2021,
        venue="Medical Image Analysis",
        doi="10.1016/j.media.2021.102062",
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "10.1016/j.media.2021.102062")],
    )
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers([arxiv_p, oa_p])
    assert len(merged) == 1
    assert merged[0].venue == "Medical Image Analysis"
    assert dedup.get_warnings() == []
