"""FIX-Q ticket tests (B7-P35 round 17 real-network defects — execution round).

- F1 (Q2): the adaptive rate limiter no longer treats slow-but-successful
  (200) responses as overload — only 429/5xx/errors raise the delay, so a
  slow source (arxiv) can no longer push the shared HTTPClient delay up and
  penalize every other source (openalex/pubmed).
- F2 (Q3): OpenAlex author candidate selection gained CJK handling — a CJK
  query prefers Chinese ``display_name`` candidates over English
  transliteration aliases, exact CJK candidates rank by h-index (not
  citations), and the no-exact fallback ranks by h-index descending instead
  of blind ``results[0]`` (with a warning), so "李飞飞" no longer silently
  resolves to an h=2 chemistry scholar.
- F3 (Q1): ``collect_citations`` recomputes ``avg_confidence`` /
  ``paper_count`` / ``dedup`` stats on the merged result after the citing
  papers pass, instead of keeping the first pass's stats (avg_confidence
  0.0) plus a hand-patched paper_count.
- F4 (Q4): cross-source quality gate — records carrying a suspicious DOI
  prefix (``10.65215``) or an implausible year surface a warning on
  ``CollectionResult.warnings`` instead of being silently trusted.
- F5 (Q5): deduplicator stats are reset at the start of every call, so
  ``compared``/``merged``/``clusters`` all describe that call only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.openalex import _select_author_candidate
from academic_intelligence.utils.rate_limiter import (
    AdaptiveDelayConfig,
    AdaptiveRateLimiter,
    RateLimitConfig,
)


def _ev(source: SourceType = SourceType.OPENALEX, conf: float = 0.8) -> Evidence:
    return Evidence(
        source=source,
        source_url=f"https://{source.value}/record",
        confidence=conf,
    )


def _paper(title: str, **kwargs: Any) -> Paper:
    defaults: dict[str, Any] = {
        "title": title,
        "authors": ["Ada"],
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Paper(**defaults)


def _author(
    author_id: str,
    name: str,
    cited: int | None = None,
    h_index: int | None = None,
) -> dict[str, Any]:
    """Canned OpenAlex ``/authors`` candidate (id bare or full URL)."""
    full = (
        author_id
        if author_id.startswith("https://")
        else f"https://openalex.org/{author_id}"
    )
    item: dict[str, Any] = {"id": full, "display_name": name}
    stats: dict[str, Any] = {}
    if cited is not None:
        item["cited_by_count"] = cited
        stats["cited_by_count"] = cited
    if h_index is not None:
        stats["h_index"] = h_index
    if stats:
        item["summary_stats"] = stats
    return item


# ---------------------------------------------------------------------------
# F1 (Q2): adaptive rate limiter policy
# ---------------------------------------------------------------------------


def _adaptive_limiter() -> AdaptiveRateLimiter:
    return AdaptiveRateLimiter(
        RateLimitConfig(requests_per_second=2.0, jitter=False),
        adaptive_config=AdaptiveDelayConfig(target_latency_ms=500.0),
    )


@pytest.mark.asyncio
async def test_adaptive_slow_200_does_not_raise_delay() -> None:
    """Q2: a slow-but-successful response must not keep pushing the shared
    delay up (the old ×1.5-per-slow-response runaway)."""
    limiter = _adaptive_limiter()
    initial = limiter.current_delay
    for _ in range(5):
        await limiter.report_status(200, 12_000.0)  # 12s latency, healthy 200
    assert limiter.current_delay == pytest.approx(initial)


@pytest.mark.asyncio
async def test_adaptive_slow_200_does_not_raise_elevated_delay() -> None:
    """Q2: even after a 429 backoff raised the delay, a slow 200 leaves the
    elevated delay unchanged (no further ×1.5 punishment of healthy sources)."""
    limiter = _adaptive_limiter()
    await limiter.report_status(429, 50.0)
    elevated = limiter.current_delay
    assert elevated > 0.5  # backoff applied
    await limiter.report_status(200, 12_000.0)
    assert limiter.current_delay == pytest.approx(elevated)


@pytest.mark.asyncio
async def test_adaptive_429_and_5xx_still_back_off() -> None:
    """Q2: throttling (429/503) and server errors (500) still raise the delay."""
    limiter = _adaptive_limiter()
    before = limiter.current_delay
    await limiter.report_status(429, 50.0)
    assert limiter.current_delay > before
    before = limiter.current_delay
    await limiter.report_status(503, 50.0)
    assert limiter.current_delay > before
    before = limiter.current_delay
    await limiter.report_status(500, 50.0)
    assert limiter.current_delay > before


@pytest.mark.asyncio
async def test_adaptive_mixed_sources_fast_not_penalized_by_slow() -> None:
    """Q2 (shared-client scenario): a slow source (arxiv-style 200, 12s) must
    not raise the delay, and the next fast source (openalex-style 200, 100ms)
    pulls the delay back toward the 1/rps baseline."""
    limiter = _adaptive_limiter()
    initial = limiter.current_delay
    await limiter.report_status(200, 12_000.0)  # slow source
    assert limiter.current_delay == pytest.approx(initial)  # not penalized
    await limiter.report_status(200, 100.0)  # fast source
    assert limiter.current_delay <= initial  # recovered, never above baseline


# ---------------------------------------------------------------------------
# F2 (Q3): CJK author-name disambiguation in OpenAlex candidate selection
# ---------------------------------------------------------------------------


def test_select_author_candidate_cjk_prefers_chinese_display_name() -> None:
    """Q3: a CJK query prefers candidates whose display_name is Chinese over
    English transliteration aliases, even when an English alias is the
    relevance top-1.  The Chinese candidate need not be token-exact (spacing
    variants and the like) — containing CJK characters outranks an English
    transliteration."""
    results = [
        _author("A1", "Fei-Fei Li", cited=30000),  # English transliteration, top-1
        _author("A2", "Feifei Li", cited=5000),  # English transliteration
        _author("A3", "李 飞飞", cited=900),  # Chinese display name
    ]
    chosen = _select_author_candidate("李飞飞", results)
    assert chosen is not None
    assert chosen["id"] == "https://openalex.org/A3"


def test_select_author_candidate_cjk_exact_ranks_by_h_index() -> None:
    """Q3 (P35 Q3 scenario): among exact same-name candidates a CJK query
    ranks by h-index (not citations), so the h=2 chemistry 李飞飞 no longer
    shadows the h=50 scholar even though the chemist has more citations."""
    results = [
        {
            "id": "https://openalex.org/A1",
            "display_name": "李飞飞",
            "summary_stats": {"h_index": 2, "cited_by_count": 3000},  # chemistry
        },
        {
            "id": "https://openalex.org/A2",
            "display_name": "李飞飞",
            "summary_stats": {"h_index": 50, "cited_by_count": 2500},
        },
    ]
    chosen = _select_author_candidate("李飞飞", results)
    assert chosen is not None
    assert chosen["id"] == "https://openalex.org/A2"


def test_select_author_candidate_cjk_exact_ignores_english_top1() -> None:
    """Q3: an English-named top-1 (the AI person "Fei-Fei Li" is stored under
    its transliteration) must not win a Chinese query when a Chinese-named
    exact candidate exists."""
    results = [
        _author("A1", "Fei-Fei Li", cited=30000),
        _author("A2", "李飞飞", cited=500),
        _author("A3", "李飞飞", cited=1200),
    ]
    chosen = _select_author_candidate("李飞飞", results)
    assert chosen is not None
    assert chosen["id"] == "https://openalex.org/A3"


def test_select_author_candidate_no_exact_ranks_by_h_index() -> None:
    """Q3: with no exact candidate the fallback ranks by h-index descending
    (instead of blind ``results[0]``) so a low-impact same-name scholar never
    shadows a prominent one."""
    results = [
        {
            "id": "https://openalex.org/A1",
            "display_name": "Jing Li",
            "summary_stats": {"h_index": 2, "cited_by_count": 118984},
        },
        {
            "id": "https://openalex.org/A2",
            "display_name": "Jun Li",
            "summary_stats": {"h_index": 50, "cited_by_count": 90000},
        },
    ]
    chosen = _select_author_candidate("J. Li", results)
    assert chosen is not None
    assert chosen["id"] == "https://openalex.org/A2"


def test_select_author_candidate_no_exact_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Q3: a non-exact fallback pick is never silent — the mismatch is logged
    at warning level so the caller can verify the intended person."""
    results = [
        _author("A1", "Jing Li", cited=118984),
        _author("A2", "Jun Li", cited=90000),
    ]
    with caplog.at_level(
        logging.WARNING, logger="academic_intelligence.sources.openalex"
    ):
        chosen = _select_author_candidate("J. Li", results)
    assert chosen is not None
    assert "no exact-name candidate matched" in caplog.text


# ---------------------------------------------------------------------------
# F3 (Q1): collect_citations stats reflect the citing papers
# ---------------------------------------------------------------------------


class _FakeSource(BaseSource):
    """Minimal source exposing search + citation collection."""

    name = "fake_q"
    source_type = SourceType.OPENALEX
    capabilities = {
        **BaseSource.capabilities,
        # C1 fail-closed dispatch: declare the citation ops it has.
        "citations": True,
        "get_citations": True,
        "get_citing_papers": True,
    }

    def __init__(
        self,
        papers: list[Paper] | None = None,
        citations: list[Citation] | None = None,
        citing_papers: list[Paper] | None = None,
    ) -> None:
        self._papers = papers or []
        self._citations = citations or []
        self._citing_papers = citing_papers or []

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return list(self._papers)

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        return []

    async def get_author_profile(self, author_name: str) -> Author | None:
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return list(self._citations)

    async def get_citing_papers(self, paper_id: str) -> list[Paper]:
        return list(self._citing_papers)


@pytest.mark.asyncio
async def test_collect_citations_stats_reflect_citing_papers() -> None:
    """Q1: after the citing-papers pass, ``avg_confidence`` reflects the real
    citing-paper confidences (not 0.0) and ``paper_count``/``dedup`` describe
    the merged result."""
    citation = Citation(
        citing_paper_id="W2257979135",
        cited_paper_id="W2919115771",
        evidence=_ev(conf=0.8),
    )
    citing = [
        _paper("Citing One", id="W2257979135", evidence=_ev(conf=0.7)),
        _paper("Citing Two", id="W2257979136", evidence=_ev(conf=0.9)),
    ]
    source = _FakeSource(citations=[citation], citing_papers=citing)
    collector = MultiSourceCollector(config=Config(), sources=[source])

    result = await collector.collect_citations("W2919115771")

    assert len(result.citations) == 1
    assert result.stats["paper_count"] == 2
    assert result.stats["avg_confidence"] == pytest.approx(0.8)  # not 0.0
    assert result.stats["dedup"]["clusters"] == 2
    assert result.stats["dedup"]["compared"] == 1


@pytest.mark.asyncio
async def test_collect_citations_stats_without_citing_capability() -> None:
    """Q1: sources without ``get_citing_papers`` keep the single-pass stats."""
    source = _FakeSource()  # no citing papers, but capability exists (empty)
    collector = MultiSourceCollector(config=Config(), sources=[source])
    result = await collector.collect_citations("W2919115771")
    assert result.stats["paper_count"] == 0
    assert result.stats["avg_confidence"] == 0.0


# ---------------------------------------------------------------------------
# F4 (Q4): cross-source quality gate (suspicious DOI prefix / year anomaly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quality_gate_flags_suspicious_doi_prefix() -> None:
    """Q4: the polluted record shape from P35 (DOI 10.65215/2q58a426 ranked
    top-1 for a canonical paper) surfaces a warning at collection time."""
    polluted = _paper(
        "Attention Is All You Need",
        id="W1",
        doi="10.65215/2q58a426",
        year=2025,
    )
    source = _FakeSource(papers=[polluted])
    collector = MultiSourceCollector(config=Config(), sources=[source])

    result = await collector.collect("attention is all you need")

    assert any("suspicious DOI prefix" in w and "10.65215" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_quality_gate_clean_record_no_suspicious_warning() -> None:
    """Q4: a normal DOI (and no year anomaly) produces no quality warning."""
    clean = _paper("A Clean Paper", id="W2", doi="10.1038/nature14539", year=2017)
    source = _FakeSource(papers=[clean])
    collector = MultiSourceCollector(config=Config(), sources=[source])

    result = await collector.collect("clean paper")

    assert not any("suspicious" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_quality_gate_flags_future_year() -> None:
    """Q4: a record whose publication year is in the future is flagged."""
    future_year = datetime.now(UTC).year + 1  # valid per Paper bounds (current+1)
    future = _paper("From The Future", id="W3", year=future_year)
    source = _FakeSource(papers=[future])
    collector = MultiSourceCollector(config=Config(), sources=[source])

    result = await collector.collect("future paper")

    assert any("publication year" in w and "future" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# F5 (Q5): dedup stats describe each call only
# ---------------------------------------------------------------------------


def test_dedup_stats_reset_per_call() -> None:
    """Q5: ``compared``/``merged``/``clusters`` are reset at the start of every
    ``deduplicate_papers`` call, so the second call's stats describe only that
    call (previously compared/merged accumulated while clusters was
    overwritten — the same field, two semantics)."""
    dedup = Deduplicator()

    first = dedup.deduplicate_papers(
        [
            _paper("Alpha", id="p-a1", doi="10.1000/alpha"),
            _paper("Alpha", id="p-a2", doi="10.1000/alpha"),  # exact duplicate
            _paper("Beta", id="p-b", doi="10.1001/beta"),
        ]
    )
    assert len(first) == 2
    stats1 = dedup.get_stats()
    assert stats1["clusters"] == 2
    assert stats1["merged"] == 1

    second = dedup.deduplicate_papers(
        [
            _paper("Gamma", id="p-g", doi="10.1002/gamma"),
            _paper("Delta", id="p-d", doi="10.1003/delta"),
        ]
    )
    assert len(second) == 2
    stats2 = dedup.get_stats()
    # Only the second call's single pair was compared; nothing carried over.
    assert stats2["compared"] == 1
    assert stats2["merged"] == 0
    assert stats2["clusters"] == 2
