"""Integration-wiring fix tests (fix order B: IM-2/3/4/5).

Covers the four Important items from the 2026-08-11 review report:

- **IM-4** (C1 contract): ``sources/base.py`` downgrades the author/citation
  abstract methods to ``NotSupportedError`` defaults, adds the ``fulltext``
  and C1 operation-name capability keys, and makes ``supports()`` fail-closed
  (no duck-typing fallback) — new sources instantiate without implementing
  the optional methods, old sources keep their behavior.
- **IM-3** (Config sections): ``Config.budget`` / ``Config.crawler`` override
  the budget defaults and the webcrawler knobs.
- **IM-2** (collector budget wiring): requests are pre-checked / consumed /
  failure-reported through the ``BudgetManager``; over-limit sources are
  skipped fail-soft, never fatal; USD-class sources trip their circuit
  breaker on 429/billing signals and recover at the UTC day boundary.
- **IM-5** (``crawl_cache``): the SQLite table round-trips and the
  WebCrawler records every crawl outcome and reuses previous successful
  crawls across sessions.

All tests are offline.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from academic_intelligence.budget import BudgetManager, BudgetSpec
from academic_intelligence.budget.models import BudgetStatus
from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.exceptions import NotSupportedError, RateLimitError
from academic_intelligence.core.models import Author, Citation, Paper
from academic_intelligence.core.types import Config, SourceType, WebCrawlerConfig
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.crossref import CrossrefSource
from academic_intelligence.sources.ieee import IEEESource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.sources.pubmed import PubMedSource
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from academic_intelligence.webcrawler import CrawlStatus, WebCrawler
from academic_intelligence.webcrawler.fetchers import DEFAULT_USER_AGENT
from academic_intelligence.webcrawler.robots import RobotsChecker
from tests.webcrawler.fixtures import (
    ALLOW_ALL_ROBOTS,
    ARTICLE_HTML,
    build_transport,
)

# ----------------------------------------------------------------------
# IM-4: C1 contract on the base class
# ----------------------------------------------------------------------


class _MinimalSource(BaseSource):
    """A brand-new source that implements ONLY the metadata operations."""

    name = "minimal"
    source_type = SourceType.CROSSREF

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        return []

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        return None


def test_new_source_instantiates_without_author_or_citation_methods() -> None:
    """IM-4: a new source need not implement the optional operations."""
    source = _MinimalSource()
    assert source.capabilities["search_papers"] is True
    assert source.capabilities["get_paper_by_doi"] is True
    assert source.capabilities["get_author_papers"] is False
    assert source.capabilities["get_author_profile"] is False
    assert source.capabilities["get_citations"] is False
    assert source.capabilities["fulltext"] is False


@pytest.mark.asyncio
async def test_base_default_author_and_citation_methods_raise_not_supported() -> None:
    """IM-4: the downgraded base methods raise NotSupportedError."""
    source = _MinimalSource()
    with pytest.raises(NotSupportedError):
        await source.get_author_papers("Ada Lovelace")
    with pytest.raises(NotSupportedError):
        await source.get_author_profile("Ada Lovelace")
    with pytest.raises(NotSupportedError):
        await source.get_citations("10.1000/xyz")


def test_base_capabilities_carry_c1_keys_and_fulltext() -> None:
    """IM-4: base capabilities expose the C1 operation keys + fulltext."""
    caps = BaseSource.capabilities
    assert caps["search"] is True and caps["get"] is True
    assert caps["citations"] is False
    assert caps["fulltext"] is False
    # long-form author/citation keys default to False (opt-in by sources).
    for key in ("get_author_papers", "get_author_profile", "get_citations"):
        assert caps[key] is False


def test_supports_is_fail_closed_without_duck_typing() -> None:
    """IM-4: supports() reads declarations only — no callable fallback."""
    source = CrossrefSource()
    # declared keys resolve normally.
    assert source.supports("search") is True
    assert source.supports("get") is True
    assert source.supports("fulltext") is False
    # an undeclared method name is UNSUPPORTED even when the method exists
    # (CrossrefSource has no get_fulltext, but even sources that do have the
    # method must declare the capability).
    assert source.supports("get_fulltext") is False
    assert source.supports("no_such_operation") is False


def test_old_sources_keep_their_capability_behavior() -> None:
    """IM-4 zero regression: the 6 old sources declare what they implement."""
    arxiv = ArxivSource()
    assert arxiv.supports("get_author_papers") is True
    assert arxiv.supports("get_author_profile") is True
    assert arxiv.supports("get_citations") is False
    assert arxiv.supports("get_paper_by_arxiv_id") is True

    ieee = IEEESource()
    assert ieee.supports("get_author_papers") is True
    assert ieee.supports("get_citations") is False

    oa = OpenAlexSource()
    assert oa.supports("search") is True and oa.supports("citations") is True
    assert oa.supports("get_citations") is True
    assert oa.supports("get_citing_papers") is True
    assert oa.supports("get_paper_by_id") is True

    ss = SemanticScholarSource()
    assert ss.supports("get_author_papers") is True
    assert ss.supports("get_citations") is True

    pubmed = PubMedSource()
    assert pubmed.supports("get_author_papers") is True
    assert pubmed.supports("get_citations") is True


# ----------------------------------------------------------------------
# IM-3: Config budget / crawler sections
# ----------------------------------------------------------------------


def test_config_budget_section_overrides_defaults() -> None:
    """IM-3: Config.budget maps onto BudgetManager budgets (default fallback)."""
    empty = Config()
    assert empty.budget == {}
    defaults = BudgetManager(budgets=None if not empty.budget else list(empty.budget.values()))
    assert {s.source for s in defaults._budgets.values()} == {
        "openalex",
        "semantic_scholar",
        "crossref",
        "arxiv",
    }

    configured = Config(
        budget={
            "openalex": BudgetSpec(source="openalex", limit=0, unit="usd", period="day")
        }
    )
    manager = BudgetManager(
        budgets=list(configured.budget.values()) if configured.budget else None
    )
    statuses = {s.source: s for s in _sync_status(manager)}
    assert set(statuses) == {"openalex"}  # config replaced the defaults entirely
    assert statuses["openalex"].limit == 0.0


def test_config_budget_keys_normalized_to_spec_source() -> None:
    """IM-3: budget dict keys are normalized to each spec's source field."""
    cfg = Config.model_validate(
        {
            "budget": {
                "aliased_key": BudgetSpec(
                    source="openalex", limit=0, unit="usd", period="day"
                )
            }
        }
    )
    assert list(cfg.budget) == ["openalex"]


def test_config_crawler_section_maps_to_webcrawler_kwargs() -> None:
    """IM-3: Config.crawler overrides the webcrawler knobs."""
    cfg = Config(
        crawler=WebCrawlerConfig(
            fetch_mode="http",
            user_agent="test-agent/1.0",
            rate_limit=0.5,
            enable_robots=False,
        )
    )
    kwargs = cfg.crawler.to_crawler_kwargs()
    assert kwargs["user_agent"] == "test-agent/1.0"
    assert kwargs["rate_limit"] == 0.5
    assert kwargs["enable_robots"] is False
    assert kwargs["prefer_curl"] is False  # http mode pins httpx
    assert kwargs["enable_browser"] is False
    # default crawler config keeps the polite-mode banner and robots on.
    default_kwargs = Config().crawler.to_crawler_kwargs()
    assert default_kwargs["user_agent"] == "paper-research-crawler/0.2 (learning use)"
    assert default_kwargs["enable_robots"] is True


# ----------------------------------------------------------------------
# IM-2: collector budget wiring (fail-soft skip + breaker + day recovery)
# ----------------------------------------------------------------------


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


class _MockSource(BaseSource):
    """Offline source with declared capabilities and call counting."""

    name = "mock"
    source_type = SourceType.OPENALEX
    capabilities = {
        **BaseSource.capabilities,
        "citations": True,
        "get_author_papers": True,
        "get_author_profile": True,
        "get_citations": True,
    }

    def __init__(
        self,
        *,
        name: str | None = None,
        papers: list[Paper] | None = None,
        error: Exception | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        self._papers = papers or []
        self._error = error
        self.calls = 0

    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._papers)

    async def get_paper_by_doi(self, doi: str) -> Paper | None:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._papers[0] if self._papers else None

    async def get_author_papers(self, author_name: str) -> list[Paper]:
        return []

    async def get_author_profile(self, author_name: str) -> Author | None:
        return None

    async def get_citations(self, paper_id: str) -> list[Citation]:
        return []


def _paper(title: str) -> Paper:
    return Paper(id=title, title=title)


def _sync_status(manager: BudgetManager) -> list[BudgetStatus]:
    """Run the async status() on a fresh loop and return the snapshot."""
    import asyncio

    return asyncio.run(manager.status())


@pytest.mark.asyncio
async def test_zero_limit_budget_skips_source_fail_soft() -> None:
    """IM-2 acceptance: a source pinned to 0 is skipped, others continue,
    and the budget report rides on the result (never fatal)."""
    clock = _Clock(datetime(2026, 8, 10, tzinfo=UTC))
    manager = BudgetManager(
        budgets=[
            BudgetSpec(source="blocked", limit=0, unit="req", period="day"),
            BudgetSpec(source="fine", limit=10, unit="req", period="300s"),
        ],
        now_fn=clock,
    )
    blocked = _MockSource(name="blocked", papers=[_paper("B")])
    fine = _MockSource(name="fine", papers=[_paper("F")])
    collector = MultiSourceCollector(
        config=Config(sources=["blocked", "fine"]), sources=[blocked, fine],
        budget_manager=manager,
    )

    result = await collector.collect("query")

    assert blocked.calls == 0  # never reached the adapter
    assert fine.calls == 1
    assert result.stats["sources_used"] == ["fine"]
    assert result.stats["budget_skipped"] == ["blocked"]
    assert any("budget skip for blocked" in w for w in result.warnings)
    events = result.stats["budget"]
    assert any(e["source"] == "blocked" and e["kind"] == "denied" for e in events)


@pytest.mark.asyncio
async def test_all_sources_budget_denied_does_not_raise() -> None:
    """IM-2: an all-budget-skip collection returns normally (design §5)."""
    clock = _Clock(datetime(2026, 8, 10, tzinfo=UTC))
    manager = BudgetManager(
        budgets=[BudgetSpec(source="a", limit=0, unit="req", period="day")],
        now_fn=clock,
    )
    a = _MockSource(name="a", papers=[_paper("A")])
    collector = MultiSourceCollector(config=Config(), sources=[a], budget_manager=manager)

    result = await collector.collect("query")

    assert result.stats["budget_skipped"] == ["a"]
    assert result.stats["sources_used"] == []
    assert len(result.errors) == 0  # skipped, not failed


@pytest.mark.asyncio
async def test_req_class_source_consumes_requests_then_is_skipped() -> None:
    """IM-2: req-class budgets pre-check and consume 1.0 per request."""
    clock = _Clock(datetime(2026, 8, 10, 12, 0, tzinfo=UTC))
    manager = BudgetManager(
        budgets=[BudgetSpec(source="s2", limit=3, unit="req", period="300s")],
        now_fn=clock,
    )
    source = _MockSource(name="s2", papers=[_paper("P")])
    collector = MultiSourceCollector(config=Config(), sources=[source], budget_manager=manager)

    for _ in range(3):
        result = await collector.collect("q")
        assert "s2" in result.stats["sources_used"]
    # the 4th request is denied fail-soft (used == limit == 3)
    result = await collector.collect("q")
    assert result.stats["budget_skipped"] == ["s2"]
    assert source.calls == 3
    assert any("budget skip for s2" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_usd_source_429_trips_breaker_and_recovers_next_utc_day() -> None:
    """IM-2 acceptance: USD source 429 → circuit breaker; UTC day recovery."""
    clock = _Clock(datetime(2026, 8, 10, 23, 59, tzinfo=UTC))
    manager = BudgetManager(
        budgets=[BudgetSpec(source="openalex", limit=1.0, unit="usd", period="day")],
        now_fn=clock,
    )
    boom = _MockSource(
        name="openalex",
        error=RateLimitError("429", source_name="openalex", context={"http_status": 429}),
    )
    ok = _MockSource(name="arxiv", papers=[_paper("A")])
    collector = MultiSourceCollector(config=Config(), sources=[boom, ok], budget_manager=manager)

    # first run: openalex fails with 429 → report_failure trips the breaker
    first = await collector.collect("q")
    assert "openalex" in first.stats["sources_failed"]
    assert first.stats["sources_used"] == ["arxiv"]
    assert any(
        s.quota_exhausted for s in await manager.status() if s.source == "openalex"
    )

    # second run (same UTC day): openalex is skipped BEFORE the adapter
    second = await collector.collect("q")
    assert "openalex" in second.stats["budget_skipped"]
    assert boom.calls == 1  # only the first request reached the adapter

    # the next UTC day rolls the period: the breaker clears
    clock.advance(seconds=61)
    boom._error = None
    third = await collector.collect("q")
    assert "openalex" in third.stats["sources_used"]
    assert third.stats["budget_skipped"] == []


@pytest.mark.asyncio
async def test_metered_success_accumulates_estimate_without_immediate_trip() -> None:
    """IM-2: successful USD requests accumulate a small estimate (not 1.0),
    so a single collection does not blow the daily budget."""
    clock = _Clock(datetime(2026, 8, 10, tzinfo=UTC))
    manager = BudgetManager(
        budgets=[BudgetSpec(source="openalex", limit=1.0, unit="usd", period="day")],
        now_fn=clock,
    )
    source = _MockSource(name="openalex", papers=[_paper("P")])
    collector = MultiSourceCollector(config=Config(), sources=[source], budget_manager=manager)

    for _ in range(5):
        result = await collector.collect("q")
        assert "openalex" in result.stats["sources_used"]

    status = next(s for s in await manager.status() if s.source == "openalex")
    assert status.used < 1.0
    assert not status.quota_exhausted


@pytest.mark.asyncio
async def test_collector_budget_wiring_from_config_and_facade(
    tmp_path: Path,
) -> None:
    """IM-2/IM-3: AcademicIntelligence wires Config.budget into the manager
    and the collector (zero-limit source skipped, others continue)."""
    from unittest.mock import AsyncMock

    from academic_intelligence import AcademicIntelligence

    db = tmp_path / "facade.db"
    ai = AcademicIntelligence(
        {
            "sources": ["openalex", "arxiv", "crossref"],
            "storage_path": str(db),
            "budget": {
                "openalex": BudgetSpec(
                    source="openalex", limit=0, unit="usd", period="day"
                )
            },
        }
    )
    await ai.connect()
    try:
        assert ai.budget_manager is not None
        assert {s.source for s in await ai.budget_manager.status()} == {"openalex"}
        for _name, source in ai._sources.items():
            source.search_papers = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await ai.collect_paper("query")
        assert "openalex" in result.stats["budget_skipped"]
        assert set(result.stats["sources_used"]) == {"arxiv", "crossref"}
    finally:
        await ai.close()


# ----------------------------------------------------------------------
# IM-5: crawl_cache table + WebCrawler persistence
# ----------------------------------------------------------------------


async def test_crawl_cache_sqlite_roundtrip(tmp_path: Path) -> None:
    """IM-5: save/get roundtrip + upsert on the crawl_cache table."""
    db = tmp_path / "crawl.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        from academic_intelligence.webcrawler.models import CrawlCacheRecord

        assert await store.get_crawl_cache("https://example.com/x") is None
        record = CrawlCacheRecord(
            url="https://example.com/x",
            status="ok",
            fetched_at="2026-08-11T00:00:00+00:00",
            web_doc={"url": "https://example.com/x", "status": "ok", "title": "T"},
        )
        await store.save_crawl_cache(record)
        got = await store.get_crawl_cache("https://example.com/x")
        assert got is not None
        assert got.status == "ok"
        assert got.web_doc is not None and got.web_doc["title"] == "T"
        # upsert replaces the row (status flip, web_doc cleared)
        await store.save_crawl_cache(
            CrawlCacheRecord(url="https://example.com/x", status="blocked")
        )
        got = await store.get_crawl_cache("https://example.com/x")
        assert got is not None and got.status == "blocked" and got.web_doc is None
    finally:
        await store.close()


async def test_crawl_cache_table_created_on_legacy_db(tmp_path: Path) -> None:
    """IM-5: a pre-upgrade database gets the crawl_cache table on connect."""
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT)")
    con.commit()
    con.close()

    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        from academic_intelligence.webcrawler.models import CrawlCacheRecord

        await store.save_crawl_cache(
            CrawlCacheRecord(url="https://example.com/legacy", status="failed")
        )
        got = await store.get_crawl_cache("https://example.com/legacy")
        assert got is not None and got.status == "failed"
    finally:
        await store.close()
    tables = {
        row[0]
        for row in sqlite3.connect(str(db)).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "crawl_cache" in tables
    assert "papers" in tables


@pytest.mark.asyncio
async def test_webcrawler_persists_outcomes_and_reuses_across_sessions(
    tmp_path: Path,
) -> None:
    """IM-5: WebCrawler records ok/blocked outcomes into the table and a
    fresh crawler on the same database reuses a previous successful crawl."""
    db = tmp_path / "crawler.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        transport, hits = build_transport(
            {"/article": (200, ARTICLE_HTML), "/locked": (403, "Forbidden")}
        )
        checker = RobotsChecker.from_text(ALLOW_ALL_ROBOTS, user_agent=DEFAULT_USER_AGENT)
        crawler = WebCrawler(
            transport=transport,
            robots_checker=checker,
            rate_limit=1000.0,
            prefer_curl=False,
            cache_store=store,
        )
        try:
            ok_doc = await crawler.crawl("https://example.com/article")
            assert ok_doc.status == CrawlStatus.OK
            blocked_doc = await crawler.crawl("https://example.com/locked")
            assert blocked_doc.status == CrawlStatus.BLOCKED
        finally:
            await crawler.close()

        ok_rec = await store.get_crawl_cache("https://example.com/article")
        blocked_rec = await store.get_crawl_cache("https://example.com/locked")
        assert ok_rec is not None and ok_rec.status == "ok"
        assert blocked_rec is not None and blocked_rec.status == "blocked"
    finally:
        await store.close()

    # cross-session: a fresh crawler + fresh store on the same db must serve
    # the previous successful crawl from the table (zero network hits).
    store2 = SQLiteStorage(str(db))
    await store2.connect()
    try:
        transport2, hits2 = build_transport({"/article": (200, ARTICLE_HTML)})
        checker2 = RobotsChecker.from_text(ALLOW_ALL_ROBOTS, user_agent=DEFAULT_USER_AGENT)
        crawler2 = WebCrawler(
            transport=transport2,
            robots_checker=checker2,
            rate_limit=1000.0,
            prefer_curl=False,
            cache_store=store2,
        )
        try:
            doc2 = await crawler2.crawl("https://example.com/article")
            assert doc2.status == CrawlStatus.OK
            assert hits2() == 0  # served from the crawl_cache table
        finally:
            await crawler2.close()
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_webcrawler_without_store_keeps_memory_cache() -> None:
    """IM-5: without a store the crawler still works in pure memory mode."""
    transport, hits = build_transport({"/page": (200, ARTICLE_HTML)})
    checker = RobotsChecker.from_text(ALLOW_ALL_ROBOTS, user_agent=DEFAULT_USER_AGENT)
    crawler = WebCrawler(
        transport=transport,
        robots_checker=checker,
        rate_limit=1000.0,
        prefer_curl=False,
        cache_store=None,
    )
    try:
        first = await crawler.crawl("https://example.com/page")
        assert first.status == CrawlStatus.OK
        second = await crawler.crawl("https://example.com/page")
        assert second.status == CrawlStatus.OK
        assert hits() == 1  # second served from the in-memory TTL cache
    finally:
        await crawler.close()


@pytest.mark.asyncio
async def test_webcrawler_enable_robots_off_skips_precheck() -> None:
    """IM-3: Config.crawler.enable_robots=False skips the robots pre-check."""
    deny = "User-agent: *\nDisallow: /private\n"
    transport, _ = build_transport({"/private": (200, ARTICLE_HTML)})
    checker = RobotsChecker.from_text(deny, user_agent=DEFAULT_USER_AGENT)
    crawler = WebCrawler(
        transport=transport,
        robots_checker=checker,
        rate_limit=1000.0,
        prefer_curl=False,
        enable_robots=False,
    )
    try:
        doc = await crawler.crawl("https://example.com/private")
        assert doc.status == CrawlStatus.OK  # robots denial ignored by design
    finally:
        await crawler.close()
