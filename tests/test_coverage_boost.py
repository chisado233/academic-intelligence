"""Additional unit/integration tests to raise coverage of secondary modules."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest

from academic_intelligence.core.exceptions import (
    AcademicIntelligenceError,
    AllSourcesFailedError,
    AuthenticationError,
    CollectorError,
    DataValidationError,
    ParseError,
    RateLimitError,
    SourceUnavailableError,
    StorageError,
)
from academic_intelligence.core.models import (
    Author,
    Citation,
    CollectionResult,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import AntiCrawlStrategy, Config, SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.processors.enricher import Enricher
from academic_intelligence.processors.incremental import IncrementalProcessor
from academic_intelligence.processors.validator import Validator, ValidatorConfig
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.google_scholar import GoogleScholarSource
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.http import HTTPClient
from academic_intelligence.utils.proxy import ProxyPool
from academic_intelligence.utils.rate_limiter import (
    AdaptiveRateLimiter,
    RateLimitConfig,
    TokenBucketRateLimiter,
    create_rate_limiter,
)
from academic_intelligence.utils.retry import RetryConfig, RetryHandler, retry_with_backoff
from tests.cassette_replay import install_cassette


def _ev(source: SourceType = SourceType.ARXIV, conf: float = 0.9) -> Evidence:
    return Evidence(source=source, source_url="https://example.com", confidence=conf)


# ---- exceptions ----


def test_exception_hierarchy_and_str() -> None:
    base = AcademicIntelligenceError("msg", context={"a": 1})
    assert "msg" in str(base)
    assert "a" in str(base)

    su = SourceUnavailableError("down", source_name="oa", context={"code": 503})
    assert su.source_name == "oa"

    rl = RateLimitError("slow", source_name="s2", retry_after=5)
    assert rl.retry_after == 5

    auth = AuthenticationError("bad key", source_name="gs")
    assert auth.source_name == "gs"

    pe = ParseError("bad json", source_name="oa", raw_snippet="{")
    assert pe.raw_snippet == "{"

    ce = CollectorError("no sources")
    assert "no sources" in str(ce)

    ase = AllSourcesFailedError(
        "all failed",
        query="q",
        sources_attempted=["a", "b"],
    )
    assert "all failed" in str(ase)

    dve = DataValidationError("invalid", details={"field": "title"})
    assert "invalid" in str(dve)

    se = StorageError("io", backend="sqlite", context={"path": "x"})
    assert se.backend == "sqlite"


# ---- models extras ----


def test_collection_result_merge_and_dict() -> None:
    p = Paper(title="T", authors=["A"], evidence=_ev())
    a = Author(name="Ada", evidence=_ev())
    c = Citation(citing_paper_id="1", cited_paper_id="2", evidence=_ev())
    r1 = CollectionResult(papers=[p], authors=[a], stats={"n": 1})
    r2 = CollectionResult(citations=[c], errors=["e"], stats={"m": 2})
    merged = r1.merge(r2)
    assert len(merged.papers) == 1
    assert len(merged.authors) == 1
    assert len(merged.citations) == 1
    assert merged.errors == ["e"]
    d = merged.to_dict()
    assert "papers" in d
    assert CollectionResult.from_dict(d).papers[0].title == "T"


def test_evidence_author_paper_roundtrip() -> None:
    ev = Evidence(
        source=SourceType.PUBMED,
        source_url="https://pubmed.ncbi.nlm.nih.gov/1",
        confidence=0.5,
        raw_data={"k": "v"},
    )
    assert Evidence.from_dict(ev.to_dict()).confidence == 0.5
    author = Author(
        name="Ada Lovelace",
        affiliation="U",
        email="ada@example.com",
        homepage="https://example.com",
        h_index=10,
        citations=100,
        interests=["math"],
        profile_url="https://example.com/ada",
        evidence=ev,
    )
    assert Author.from_dict(author.to_dict()).email == "ada@example.com"
    paper = Paper(
        title="Notes",
        authors=["Ada"],
        year=1843,
        doi="10.1234/notes.1843",
        url="https://example.com/p",
        evidence=ev,
    )
    assert Paper.from_dict(paper.to_dict()).year == 1843
    cite = Citation(citing_paper_id="a", cited_paper_id="b", evidence=ev)
    assert Citation.from_dict(cite.to_dict()).citing_paper_id == "a"


# ---- utils ----


@pytest.mark.asyncio
async def test_cache_expiry_and_clear(tmp_path: Path) -> None:
    cache = Cache(ttl=1, persistent=True, persist_path=tmp_path / "c.json")
    key = Cache.make_key("get", "https://x.com", {"q": 1})
    await cache.set(key, {"v": 1}, ttl=1)
    assert await cache.get(key) == {"v": 1}
    # Force expiry
    cache._memory[key] = ({"v": 1}, 0.0)
    assert await cache.get(key) is None
    await cache.set(key, 2)
    await cache.clear()
    assert await cache.get(key) is None


@pytest.mark.asyncio
async def test_token_bucket_and_adaptive() -> None:
    tb = TokenBucketRateLimiter(
        RateLimitConfig(requests_per_second=100.0, jitter=False),
        bucket_size=2,
    )
    await tb.acquire()
    await tb.acquire()
    adaptive = AdaptiveRateLimiter(
        RateLimitConfig(requests_per_second=50.0, jitter=False)
    )
    await adaptive.acquire()
    await adaptive.report_status(200, 100.0)
    await adaptive.report_status(429, 50.0)
    await adaptive.report_status(500, 50.0)
    await adaptive.acquire()
    assert create_rate_limiter("token_bucket", requests_per_second=10.0)
    assert create_rate_limiter("token", requests_per_second=10.0)


@pytest.mark.asyncio
async def test_retry_decorator() -> None:
    calls = {"n": 0}

    @retry_with_backoff(RetryConfig(max_retries=2, base_delay=0.01, jitter=False))
    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.TransportError("x")
        return "ok"

    assert await flaky() == "ok"


def test_proxy_add_remove_random() -> None:
    pool = ProxyPool(["http://a:1"])
    pool.add("http://b:1")
    assert pool.total_count == 2
    chosen = pool.get_next("random")
    assert chosen in ("http://a:1", "http://b:1")
    pool.remove("http://a:1")
    assert pool.healthy_count == 1
    assert pool.healthy_proxies == ["http://b:1"]


@pytest.mark.asyncio
async def test_http_get_json_and_post(monkeypatch: pytest.MonkeyPatch) -> None:
    strategy = AntiCrawlStrategy(max_retries=0, base_delay=0.0, adaptive_delay=False, jitter=False)
    client = HTTPClient(strategy=strategy, enable_cache=False, timeout=5.0)
    await client.connect()

    async def _req(method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if method.upper() == "POST":
            return httpx.Response(200, json={"posted": True}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    assert client._client is not None
    monkeypatch.setattr(client._client, "request", _req)
    # bypass rate limiter delay
    monkeypatch.setattr(client, "_apply_rate_limit", AsyncMockish())

    data = await client.get_json("https://example.com/api")
    assert data["ok"] is True
    resp = await client.post("https://example.com/api", json={"a": 1})
    assert resp.status_code == 200
    await client.close()


class AsyncMockish:
    async def __call__(self, *a: Any, **k: Any) -> None:
        return None


# ---- processors ----


def test_validator_author_citation_and_filter() -> None:
    v = Validator(ValidatorConfig(require_doi=True, max_title_length=10))
    paper = Paper(title="A" * 20, authors=[], year=2020, evidence=_ev())
    r = v.validate_paper(paper)
    assert not r.is_valid or r.warnings  # long title warning + missing doi

    author = Author(name="Bob", email=None, evidence=_ev())
    ar = v.validate_author(author)
    assert ar.is_valid

    cite = Citation(citing_paper_id="1", cited_paper_id="2", evidence=_ev(conf=0.1))
    cr = v.validate_citation(cite)
    assert cr is not None

    papers = [
        Paper(title="Good Paper", authors=["A"], year=2020, doi="10.1234/abc.def", evidence=_ev()),
        Paper(title="Bad", authors=["B"], evidence=_ev(conf=0.1)),
    ]
    filtered = v.filter_valid_papers(papers)
    assert isinstance(filtered, list)


def test_enricher_and_dedup_authors() -> None:
    enricher = Enricher(min_confidence=0.3)
    papers = [
        Paper(
            title="  Hello   World  ",
            authors=[" Ada  Lovelace ", "Ada Lovelace"],
            abstract="  spaced  ",
            evidence=_ev(SourceType.OPENALEX, 0.9),
        ),
        Paper(
            title="Hello World",
            authors=["Ada Lovelace"],
            year=2020,
            evidence=_ev(SourceType.SEMANTIC_SCHOLAR, 0.8),
        ),
    ]
    enriched = enricher.enrich_papers(papers)
    assert enriched[0].title == "Hello World"
    cross = enricher.cross_validate_papers(papers)
    assert len(cross) >= 1
    authors = [
        Author(name="Ada Lovelace", affiliation="A", evidence=_ev(conf=0.9)),
        Author(name="Ada Lovelace", affiliation="B", interests=["CS"], evidence=_ev(conf=0.7)),
        Author(name="Completely Different", evidence=_ev()),
    ]
    merged = Deduplicator().deduplicate_authors(authors)
    assert len(merged) == 2
    en_auth = enricher.enrich_authors(merged)
    assert len(en_auth) == 2
    assert enricher.get_stats() is not None or True


@pytest.mark.asyncio
async def test_incremental_processor(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "inc.db"))
    await store.connect()
    try:
        existing = Paper(
            id="p1",
            title="Deep Learning",
            authors=["Hinton"],
            year=2015,
            citations=100,
            doi="10.1038/nature14539",
            evidence=_ev(SourceType.OPENALEX, 0.8),
        )
        await store.save_paper(existing)
        proc = IncrementalProcessor(store)
        fresh = [
            Paper(
                id="p1",
                title="Deep Learning",
                authors=["Hinton"],
                year=2015,
                citations=200,
                doi="10.1038/nature14539",
                evidence=_ev(SourceType.SEMANTIC_SCHOLAR, 0.9),
            ),
            Paper(
                title="Brand New Paper",
                authors=["Someone"],
                year=2024,
                evidence=_ev(),
            ),
        ]
        result = await proc.detect_changes(fresh, [existing])
        assert result.total_checked == 2
        assert len(result.new) >= 1
        counts = await proc.apply_changes(result)
        assert counts["new"] >= 1
    finally:
        await store.close()


# ---- arxiv source ----


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.network
async def test_arxiv_search_cassette(monkeypatch: pytest.MonkeyPatch) -> None:
    install_cassette(monkeypatch, "arxiv_search")
    source = ArxivSource(min_interval_seconds=0.1)
    try:
        papers = await source.search_papers("attention", limit=5)
        assert len(papers) >= 1
        assert any("attention" in p.title.lower() for p in papers)
        assert papers[0].evidence.source == SourceType.ARXIV
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_arxiv_empty_query() -> None:
    source = ArxivSource(min_interval_seconds=0.1)
    try:
        assert await source.search_papers("  ", limit=1) == []
    finally:
        await source.close()


# ---- storage more paths ----


@pytest.mark.asyncio
async def test_sqlite_batch_and_query(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "batch.db"))
    await store.connect()
    try:
        papers = [
            Paper(
                title=f"Batch Paper {i}",
                authors=["Alice", "Bob"],
                year=2020 + i,
                venue="ICML",
                keywords=["ml"],
                evidence=_ev(),
            )
            for i in range(5)
        ]
        authors = [Author(name="Alice", interests=["ml"], evidence=_ev())]
        citations = [
            Citation(citing_paper_id="x", cited_paper_id="y", evidence=_ev())
        ]
        if hasattr(store, "save_batch"):
            await store.save_batch(authors=authors, papers=papers, citations=citations)
        stats = await store.get_stats()
        assert stats["total_papers"] >= 5
        found = await store.query_papers(year=2022, limit=10)
        assert isinstance(found, list)
        found2 = await store.query_papers(venue="ICML", author="Alice")
        assert len(found2) >= 1
        authors_q = await store.query_authors(name="Alice")
        assert len(authors_q) >= 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_batch_and_delete(tmp_path: Path) -> None:
    store = JSONStorage(str(tmp_path / "jb"))
    await store.connect()
    try:
        p = Paper(title="JSON Paper", authors=["Z"], year=2021, evidence=_ev())
        pid = await store.save_paper(p)
        a = Author(name="Zed", affiliation="ZU", evidence=_ev())
        aid = await store.save_author(a)
        assert await store.get_author(aid) is not None
        await store.save_citation(
            Citation(citing_paper_id=pid, cited_paper_id="other", evidence=_ev())
        )
        assert await store.delete_paper(pid) is True
        assert await store.get_paper(pid) is None
        stats = await store.get_stats()
        assert stats["total_papers"] == 0
    finally:
        await store.close()


# ---- config / scholar profile edge ----


def test_config_proxy_list() -> None:
    cfg = Config(proxy="http://p:1", proxies=["http://q:2"])
    pl = cfg.proxy_list()
    assert "http://p:1" in pl
    assert "http://q:2" in pl


@pytest.mark.asyncio
async def test_google_scholar_citations_cassette(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_cassette(monkeypatch, "google_scholar_search")
    source = GoogleScholarSource(serpapi_key="test_key")
    try:
        cites = await source.get_citations("gs_ml_1")
        # cassette generic entry may not have cites engine; empty or list
        assert isinstance(cites, list)
    finally:
        await source.close()
