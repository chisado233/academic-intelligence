"""FIX-G F2: save_batch throughput regression guard.

The batched write path must sustain well above the pre-fix ~48 papers/s
(G1): a 2000-paper batch (each with 2 authors + evidence) must land inside
30s even on a loaded CI machine. The actual before/after numbers are
reported by the FIX-G perf script (``work/fix_g_perf.py`` in the dispatch
work dir).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from academic_intelligence.core.models import AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.storage.sqlite_store import SQLiteStorage

pytestmark = [pytest.mark.performance, pytest.mark.slow]


def _performance_budget(seconds: float) -> float:
    """Keep real timing gates strict; compensate only for coverage tracing."""
    tracer = sys.gettrace()
    if tracer is not None and tracer.__class__.__module__.startswith("coverage"):
        return seconds * 2
    return seconds


def _ev() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://e.com",
        confidence=0.8,
    )


@pytest.mark.asyncio
async def test_save_batch_2000_papers_throughput(tmp_path: Path) -> None:
    """2000 papers persist well inside 5s (~400/s floor): the pre-fix per-row
    ORM path measured ~12.7s/2000 (~158/s) and the batched path ~0.5s
    (~3700/s), so 5s separates the two with 10x headroom over the new code."""
    store = SQLiteStorage(str(tmp_path / "perf_batch.db"))
    await store.connect()
    try:
        papers = [
            Paper(
                id=f"perf-{i:05d}",
                title=f"Synthetic Paper {i}",
                authors=[
                    AuthorRef(name=f"Author {i}", position=1),
                    AuthorRef(name="Coauthor", position=2),
                ],
                year=2000 + (i % 25),
                venue="Fake Venue",
                abstract=f"Abstract of paper {i} with a few words.",
                evidence=_ev(),
            )
            for i in range(2000)
        ]
        start = time.perf_counter()
        ids = await store.save_batch(papers=papers)
        elapsed = time.perf_counter() - start

        assert len(ids["papers"]) == 2000
        rate = 2000 / elapsed
        assert elapsed < 5, f"2000-paper save_batch took {elapsed:.2f}s ({rate:.0f}/s)"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_batch_10k_papers_name_only_throughput(tmp_path: Path) -> None:
    """FIX-N F1: a papers-only 10k batch with name-only bylines must stay
    well inside 5s. The pre-fix name-resolution loop fired one author
    ``select`` per paper and measured 5.05s on the P31 box (vs 0.46s for a
    batch with no authors); the single batched resolution returns to the
    ~0.5-1s regime."""
    store = SQLiteStorage(str(tmp_path / "perf_batch_10k.db"))
    await store.connect()
    try:
        papers = [
            Paper(
                id=f"p10k-{i:05d}",
                title=f"Synthetic Paper {i}",
                authors=[AuthorRef(name=f"Researcher {i}", position=1)],
                year=2000 + (i % 25),
                venue="Fake Venue",
                abstract=f"Abstract of paper {i}.",
                evidence=_ev(),
            )
            for i in range(10_000)
        ]
        start = time.perf_counter()
        ids = await store.save_batch(papers=papers)
        elapsed = time.perf_counter() - start

        assert len(ids["papers"]) == 10_000
        budget = _performance_budget(5)
        assert elapsed < budget, (
            f"10k name-only save_batch took {elapsed:.2f}s (budget {budget:.0f}s)"
        )
    finally:
        await store.close()
