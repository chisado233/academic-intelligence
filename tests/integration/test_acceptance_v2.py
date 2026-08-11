"""Acceptance integration tests for 3A v2 design §17 (Phase 1).

Each of the six acceptance criteria in
``docs/superpowers/specs/2026-07-26-technical-design-v2.md`` (§17, Phase 1)
maps to one or more offline integration tests:

1. ``ai paper "10.1038/nature14539"`` returns complete paper metadata
   (title, authors, year, abstract, citation count).
2. The same paper collected from OpenAlex and Semantic Scholar is
   automatically deduplicated into a single record.
3. The ``evidence`` table holds two rows (one per source) and the composite
   confidence is computed correctly.
4. ``ai author "Geoffrey Hinton"`` returns a scholar profile (affiliation,
   h-index, paper count).
5. The SQLite database file can be opened and queried independently, and its
   schema matches the design (papers / authors / authorships / citations /
   coauthorships / evidence tables exist).
6. The full test suite passes with coverage >= 80% — guarded by
   ``test_acceptance_06_coverage_requirement`` in
   ``tests/test_zz_acceptance_coverage.py`` (self-contained, no stale
   ``coverage.xml`` dependency).

All HTTP traffic is replayed from the VCR-style cassettes in
``tests/cassettes/`` (see ``tests/cassette_replay.py``), so these tests never
touch a live third-party API and run fully offline.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.types import Config
from tests.cassette_replay import install_merged_cassettes

# DOI of "Deep learning" (LeCun, Bengio, Hinton; Nature 2015) — the
# acceptance-criteria paper used throughout §17.
_DEEP_LEARNING_DOI = "10.1038/nature14539"

# Tables required by 3A v2 §8.1 (acceptance criterion §17.5).
_REQUIRED_TABLES = {
    "papers",
    "authors",
    "authorships",
    "citations",
    "coauthorships",
    "evidence",
}


pytestmark = [pytest.mark.integration, pytest.mark.network, pytest.mark.slow]


def _cassette_config(tmp_path: Path, storage_path: str) -> Config:
    """Build a Config that replays the OpenAlex + Semantic Scholar cassettes."""
    return Config(
        sources=["openalex", "semantic_scholar"],
        storage_type="sqlite",
        storage_path=storage_path,
        cache_enabled=False,
        serpapi_key=None,
        semantic_scholar_api_key=None,
    )


class TestAcceptanceCriteria:
    """One test per 3A v2 §17 acceptance criterion (Phase 1)."""

    @pytest.mark.asyncio
    async def test_acceptance_01_paper_lookup_returns_full_info(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """§17.1 — DOI lookup returns complete paper metadata."""
        install_merged_cassettes(
            monkeypatch, ["openalex_search", "semantic_scholar_search"]
        )
        config = _cassette_config(tmp_path, str(tmp_path / "acceptance_01.db"))
        ai = AcademicIntelligence(config)
        try:
            result = await ai.collect_paper(
                _DEEP_LEARNING_DOI,
                sources=["openalex", "semantic_scholar"],
            )
            assert len(result.papers) == 1
            paper = result.papers[0]
            # Title
            assert paper.title and "deep learning" in paper.title.lower()
            # Authors (order + names preserved via AuthorRef)
            assert len(paper.authors) == 3
            assert [a.name for a in paper.authors] == [
                "Yann LeCun",
                "Yoshua Bengio",
                "Geoffrey Hinton",
            ]
            assert [a.position for a in paper.authors] == [1, 2, 3]
            # Year
            assert paper.year == 2015
            # Abstract
            assert paper.abstract and len(paper.abstract) > 20
            # Citation count
            assert paper.citations == 50000
            # DOI
            assert paper.doi == _DEEP_LEARNING_DOI
        finally:
            await ai.close()

    @pytest.mark.asyncio
    async def test_acceptance_02_multi_source_dedup_single_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """§17.2 — OpenAlex + Semantic Scholar records merge into one."""
        install_merged_cassettes(
            monkeypatch, ["openalex_search", "semantic_scholar_search"]
        )
        config = _cassette_config(tmp_path, str(tmp_path / "acceptance_02.db"))
        ai = AcademicIntelligence(config)
        try:
            result = await ai.collect_paper(
                _DEEP_LEARNING_DOI,
                sources=["openalex", "semantic_scholar"],
            )
            # Both sources answered and were fused into exactly one record.
            used = result.stats.get("sources_used") or []
            assert set(used) == {"openalex", "semantic_scholar"}
            assert len(result.papers) == 1
            paper = result.papers[0]
            assert paper.doi == _DEEP_LEARNING_DOI
            # Both confirming sources are retained in the evidence chain.
            sources = {e.source.value for e in paper.evidence_list}
            assert sources == {"openalex", "semantic_scholar"}
        finally:
            await ai.close()

    @pytest.mark.asyncio
    async def test_acceptance_03_evidence_two_rows_and_confidence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """§17.3 — evidence table holds two rows; confidence is computed."""
        install_merged_cassettes(
            monkeypatch, ["openalex_search", "semantic_scholar_search"]
        )
        config = _cassette_config(tmp_path, str(tmp_path / "acceptance_03.db"))
        ai = AcademicIntelligence(config)
        try:
            result = await ai.collect_paper(
                _DEEP_LEARNING_DOI,
                sources=["openalex", "semantic_scholar"],
                persist=True,
            )
            assert len(result.papers) == 1
            paper = result.papers[0]
            paper_id = paper.id
            assert paper_id

            # In-memory evidence chain: one entry per source, with the
            # per-source adapter confidence (S2 0.88, OpenAlex 0.90) aligned
            # with the scorer baseline table (FIX-M M4, FIX-N F3).
            per_source: dict[str, float] = {
                e.source.value: e.confidence for e in paper.evidence_list
            }
            assert per_source == {
                "semantic_scholar": pytest.approx(0.88),
                "openalex": pytest.approx(0.90),
            }

            # Composite confidence: base = max source baseline (0.90),
            # +0.05 multi-source bonus, +0.05 DOI exact match => 1.0.
            primary = paper.primary_evidence
            assert primary is not None
            assert primary.confidence == pytest.approx(1.0)

            # Persisted evidence table: exactly one row per confirming source.
            stored = await ai.storage.get_evidence("paper", paper_id)
            stored_sources = {e.source.value for e in stored}
            assert stored_sources == {"openalex", "semantic_scholar"}
            assert all(0.0 <= e.confidence <= 1.0 for e in stored)
        finally:
            await ai.close()

    @pytest.mark.asyncio
    async def test_acceptance_04_author_profile(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """§17.4 — author lookup returns affiliation, h-index, paper count."""
        install_merged_cassettes(
            monkeypatch, ["openalex_search", "semantic_scholar_search"]
        )
        config = _cassette_config(tmp_path, str(tmp_path / "acceptance_04.db"))
        ai = AcademicIntelligence(config)
        try:
            result = await ai.collect_author_papers(
                "Geoffrey Hinton",
                sources=["openalex", "semantic_scholar"],
            )
            # Paper count is exposed through the collected papers list (the
            # Author model carries profile fields; paper_count is derived).
            assert len(result.papers) > 0
            assert len(result.authors) >= 1
            hinton = next(a for a in result.authors if "Hinton" in a.name)
            # Affiliation
            assert hinton.affiliation and "Toronto" in hinton.affiliation
            # h-index
            assert hinton.h_index == 180
            # Cross-source identity merge (OpenAlex + S2 authority IDs)
            assert hinton.openalex_id == "A5023888391"
            assert hinton.semantic_scholar_id == "1695689"
        finally:
            await ai.close()

    @pytest.mark.asyncio
    async def test_acceptance_05_sqlite_schema_and_independent_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """§17.5 — DB file opens independently; schema matches design."""
        install_merged_cassettes(
            monkeypatch, ["openalex_search", "semantic_scholar_search"]
        )
        db_path = str(tmp_path / "acceptance_05.db")
        config = _cassette_config(tmp_path, db_path)
        ai = AcademicIntelligence(config)
        try:
            result = await ai.collect_paper(
                _DEEP_LEARNING_DOI,
                sources=["openalex", "semantic_scholar"],
                persist=True,
            )
            assert len(result.papers) == 1
        finally:
            # Close the engine so the file is not locked on Windows.
            await ai.close()

        # Open the file independently with the stdlib sqlite3 driver — no
        # Academic Intelligence code involved.
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            tables = {
                row[0]
                for row in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert _REQUIRED_TABLES.issubset(tables), (
                f"missing tables: {_REQUIRED_TABLES - tables}"
            )

            # The persisted paper is queryable.
            row = cur.execute(
                "SELECT title, doi, year FROM papers WHERE id = ?",
                (result.papers[0].id,),
            ).fetchone()
            assert row is not None
            assert "deep learning" in row[0].lower()
            assert row[1] == _DEEP_LEARNING_DOI
            assert row[2] == 2015

            # Evidence table rows (one per confirming source).
            evidence_rows = cur.execute(
                "SELECT source, confidence FROM evidence "
                "WHERE entity_type = 'paper' AND entity_id = ? "
                "ORDER BY source",
                (result.papers[0].id,),
            ).fetchall()
            assert {r[0] for r in evidence_rows} == {
                "openalex",
                "semantic_scholar",
            }

            # Spot-check v2 schema columns from 3A v2 §8.1.
            paper_cols = {r[1] for r in cur.execute("PRAGMA table_info(papers)")}
            for col in (
                "arxiv_id",
                "pmid",
                "fields_of_study",
                "references",
                "citations_list",
                "evidence",
            ):
                assert col in paper_cols, f"papers missing column {col}"
            author_cols = {r[1] for r in cur.execute("PRAGMA table_info(authors)")}
            for col in (
                "orcid",
                "semantic_scholar_id",
                "openalex_id",
                "disambiguation_status",
            ):
                assert col in author_cols, f"authors missing column {col}"
            edge_cols = {
                "paper_id",
                "author_id",
                "position",
                "is_corresponding",
                "raw_name",
            }
            authorship_cols = {
                r[1] for r in cur.execute("PRAGMA table_info(authorships)")
            }
            assert edge_cols.issubset(authorship_cols)
        finally:
            conn.close()
