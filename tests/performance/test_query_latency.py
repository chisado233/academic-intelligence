"""FIX-AB-4 / AB-8: query-latency regression guards.

``get_paper`` and ``query_papers(keyword=...)`` must stay within generous
latency budgets on a ~5k-row database (the achievable latencies on the dev
box are ~16ms get_paper, ~25ms selective keyword through the FTS5
paper-text index, ~60ms for a keyword matching the whole corpus — the
documented degenerate case of the trigram index).  The budgets carry 4-10x
headroom over those numbers so loaded CI hosts do not flake (this host
swings short queries ~40x under core contention, so latency is best-of-N);
they separate the current optimized paths from an order-of-magnitude
regression.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest

from academic_intelligence.core.models import AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.storage.sqlite_store import SQLiteStorage

pytestmark = [pytest.mark.performance, pytest.mark.slow]

_GET_PAPER_BUDGET_MS = 250
_KEYWORD_BUDGET_MS = 300
_QUERY_ROWS = 5000


def _ev() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://example.com",
        confidence=0.9,
    )


def _synthetic_papers(n: int) -> list[Paper]:
    papers = [
        Paper(
            id=f"syn-{i:06d}",
            title=f"Synthetic Paper Number {i} With A Reasonable Title",
            authors=[
                AuthorRef(name=f"Author {i}", position=1),
                AuthorRef(name="Coauthor Shared", position=2),
            ],
            year=1995 + (i % 30),
            venue="Synthetic Journal",
            abstract=f"Abstract of synthetic paper {i} with some words about research.",
            doi=f"10.1000/syn.{i}" if i % 5 else None,
            evidence_list=[_ev()],
        )
        for i in range(n)
    ]
    # One sparse keyword match ("zyzzyva" appears nowhere else).
    papers.append(
        Paper(
            id="needle-000000",
            title="Uniquely Needled Zyzzyva Paper",
            authors=[AuthorRef(name="Needle Author", position=1)],
            year=2020,
            venue="Rare Journal",
            abstract="Only this abstract mentions zyzzyva-neddle-xyz.",
            evidence_list=[_ev()],
        )
    )
    return papers


async def _median_ms(fn, iters: int = 20, attempts: int = 3) -> float:
    """Return the best (lowest) median latency over *attempts* windows.

    This host is extremely load-sensitive (an idle-window query measured
    40x faster than a contended one), so a single timing window can be
    polluted by transient load.  The best-of-N median reports the
    achievable latency while a consistent regression (slow in every
    window) still trips the budget.
    """
    best = float("inf")
    for _ in range(attempts):
        for _ in range(5):
            await fn()
        samples: list[float] = []
        for _ in range(iters):
            start = time.perf_counter()
            await fn()
            samples.append((time.perf_counter() - start) * 1000)
        best = min(best, statistics.median(samples))
    return best


@pytest.mark.asyncio
async def test_get_paper_latency(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "perf_query.db"))
    await store.connect()
    try:
        await store.save_batch(papers=_synthetic_papers(_QUERY_ROWS))
        paper = await store.get_paper("syn-000000")
        assert paper is not None and paper.title
        ms = await _median_ms(lambda: store.get_paper("syn-000000"))
        assert ms < _GET_PAPER_BUDGET_MS, f"get_paper took {ms:.1f} ms"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_keyword_query_latency_sparse(tmp_path: Path) -> None:
    """A selective keyword (1 match) is served through the FTS5 paper-text
    index well inside the budget."""
    store = SQLiteStorage(str(tmp_path / "perf_kw_sparse.db"))
    await store.connect()
    try:
        await store.save_batch(papers=_synthetic_papers(_QUERY_ROWS))
        result = await store.query_papers(keyword="zyzzyva", limit=100)
        assert [p.id for p in result] == ["needle-000000"]
        ms = await _median_ms(lambda: store.query_papers(keyword="zyzzyva", limit=100))
        assert ms < _KEYWORD_BUDGET_MS, f"sparse keyword took {ms:.1f} ms"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_keyword_query_latency_dense(tmp_path: Path) -> None:
    """A keyword matching the whole corpus (degenerate case) still returns
    correct insertion-ordered rows within the budget."""
    store = SQLiteStorage(str(tmp_path / "perf_kw_dense.db"))
    await store.connect()
    try:
        await store.save_batch(papers=_synthetic_papers(_QUERY_ROWS))
        result = await store.query_papers(keyword="synthetic", limit=10)
        assert [p.id for p in result] == [f"syn-{i:06d}" for i in range(10)]
        ms = await _median_ms(lambda: store.query_papers(keyword="synthetic", limit=10))
        assert ms < _KEYWORD_BUDGET_MS, f"dense keyword took {ms:.1f} ms"
    finally:
        await store.close()
