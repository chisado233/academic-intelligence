"""FIX-N ticket tests (B7-P31 round 13 defects).

- F1 (N1): ``save_batch`` resolves every name-only byline with a bounded
  number of DB queries instead of one author ``select`` per paper (the
  pre-fix loop degraded papers-only 10k name-only batches to ~5s and pushed
  the FIX-G F2 throughput guard to its 5s edge).
- F2 (N2): venue conflict detection normalizes journal-name abbreviation
  forms ("Med Image Anal." vs "Medical image analysis" vs "Medical Image
  Analysis") so the same journal written differently no longer raises a
  fake venue-conflict warning; genuinely different venues still conflict.
- F3 (N3): semantic_scholar / ieee per-source evidence confidence aligns
  with the scorer baseline — covered by the extended FIX-M F4 test in
  ``test_fix_m.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from academic_intelligence.core.models import AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(source: SourceType, conf: float, sid: str) -> Evidence:
    return Evidence(
        source=source,
        source_url=f"https://{source.value}/record",
        source_id=sid,
        confidence=conf,
        collected_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# F1 (N1): save_batch name resolution is a single batch query, not one
# query per paper
# ---------------------------------------------------------------------------


class _ExecuteCounter:
    """AsyncSession wrapper that counts ``execute`` calls (F1 query guard).

    ``save_batch`` internally creates its own session; the test swaps
    ``store._session`` for a factory returning this wrapper so every
    statement issued by the batch write path is observable.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self.execute_calls = 0

    async def __aenter__(self) -> _ExecuteCounter:
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._session.__aexit__(*exc_info)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.execute_calls += 1
        return await self._session.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@pytest.mark.asyncio
async def test_fix_n_f1_name_resolution_uses_bounded_batch_queries(
    tmp_path: Path,
) -> None:
    """N1: 100 papers with name-only bylines and no in-batch Author records
    must not fire one author ``select`` per paper (pre-fix ~106 execute()
    calls); the batched resolution keeps the whole save well under 20."""
    store = SQLiteStorage(str(tmp_path / "n1.db"))
    await store.connect()
    try:
        papers = [
            Paper(
                id=f"p-{i:03d}",
                title=f"Paper {i}",
                authors=[AuthorRef(name=f"Researcher {i}", position=1)],
                year=2020,
                evidence_list=[_ev(SourceType.ARXIV, 0.95, f"arx-{i}")],
            )
            for i in range(100)
        ]
        original_session = store._session
        counters: list[_ExecuteCounter] = []

        def counting_session() -> _ExecuteCounter:
            proxy = _ExecuteCounter(original_session())
            counters.append(proxy)
            return proxy

        store._session = counting_session  # type: ignore[method-assign]
        try:
            ids = await store.save_batch(papers=papers)
        finally:
            store._session = original_session  # type: ignore[method-assign]

        assert len(ids["papers"]) == 100
        total_executes = sum(c.execute_calls for c in counters)
        assert total_executes < 20, (
            f"save_batch issued {total_executes} execute() calls for 100 "
            "name-only papers (pre-fix ~106: one author select per paper)"
        )
        stats = await store.get_stats()
        assert stats["total_papers"] == 100
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_n_f1_batch_resolution_still_links_known_names(
    tmp_path: Path,
) -> None:
    """N1: the batched resolution keeps the FIX-M F1 linkage semantics — a
    byline name matching an Author row persisted in an earlier batch (or in
    the same batch) is re-keyed to the Author id so author->paper edges
    exist."""
    from academic_intelligence.core.models import Author

    store = SQLiteStorage(str(tmp_path / "n1b.db"))
    await store.connect()
    try:
        author_ids = await store.save_batch(
            authors=[Author(name="Alice Smith", evidence=_ev(SourceType.PUBMED, 0.92, "pm-a"))],
            papers=[],
        )
        author_id = author_ids["authors"][0]
        await store.save_batch(
            papers=[
                Paper(
                    id="p-alice",
                    title="Alice's Paper",
                    authors=[AuthorRef(name="Alice Smith", position=1)],
                    year=2024,
                    evidence_list=[_ev(SourceType.PUBMED, 0.92, "pm-p")],
                )
            ]
        )
        # the authorship edge points at the author record, not ~name
        assert await store.get_author_papers(author_id) == ["p-alice"]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F2 (N2): venue conflict abbreviation normalization
# ---------------------------------------------------------------------------


def _venue_paper(venue: str, source: SourceType, conf: float, sid: str) -> Paper:
    return Paper(
        title="FIX-N Venue Paper",
        authors=[AuthorRef(name="Jane Doe", position=1)],
        year=2023,
        venue=venue,
        evidence_list=[_ev(source, conf, sid)],
    )


def test_fix_n_f2_venue_abbreviation_forms_do_not_conflict() -> None:
    """N2: the same journal written as an arXiv abbreviation, a PubMed
    lowercase-full-name, and an OpenAlex title-case-full-name must merge
    without raising a fake venue-conflict warning (real data: "Med Image
    Anal." / "Medical image analysis" / "Medical Image Analysis")."""
    arxiv_p = _venue_paper("Med Image Anal.", SourceType.ARXIV, 0.95, "a1")
    pubmed_p = _venue_paper("Medical image analysis", SourceType.PUBMED, 0.92, "p1")
    openalex_p = _venue_paper(
        "Medical Image Analysis", SourceType.OPENALEX, 0.90, "o1"
    )
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers([arxiv_p, pubmed_p, openalex_p])

    assert len(merged) == 1
    assert merged[0].venue == "Med Image Anal."
    assert dedup.get_warnings() == []


def test_fix_n_f2_stopword_abbreviation_variants_do_not_conflict() -> None:
    """N2: a full journal name with stopwords vs its dotted abbreviation
    ("Journal of Neuroscience" vs "J. Neurosci.") must not conflict."""
    full_p = _venue_paper("Journal of Neuroscience", SourceType.ARXIV, 0.95, "a1")
    abbr_p = _venue_paper("J. Neurosci.", SourceType.PUBMED, 0.92, "p1")
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers([full_p, abbr_p])

    assert len(merged) == 1
    assert dedup.get_warnings() == []


def test_fix_n_f2_truly_different_venues_still_conflict() -> None:
    """N2: normalization must not hide a genuine venue difference — "Nature"
    and "Science" still raise a venue-conflict warning."""
    nature_p = _venue_paper("Nature", SourceType.ARXIV, 0.95, "a1")
    science_p = _venue_paper("Science", SourceType.PUBMED, 0.92, "p1")
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers([nature_p, science_p])

    assert len(merged) == 1
    assert any(w.startswith("venue conflict") for w in dedup.get_warnings())
