"""End-to-end WebCrawler.crawl tests (offline, httpx MockTransport)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from academic_intelligence.webcrawler import (
    CrawlStatus,
    SchemaField,
    SchemaSpec,
    WebCrawler,
)
from academic_intelligence.webcrawler import fetchers as fetcher_module
from academic_intelligence.webcrawler.fetchers import DEFAULT_USER_AGENT
from academic_intelligence.webcrawler.robots import RobotsChecker

from .fixtures import (
    ALLOW_ALL_ROBOTS,
    ARTICLE_HTML,
    CHALLENGE_HTML,
    DENY_PRIVATE_ROBOTS,
    PLAIN_HTML,
    build_transport,
)


@pytest.mark.asyncio
async def test_crawl_static_page_ok(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler({"/article": (200, ARTICLE_HTML)})
    try:
        doc = await crawler.crawl("https://example.com/article")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.OK
    assert doc.url == "https://example.com/article"
    assert "Attention Is All You Need" in doc.title
    assert "Transformer" in doc.content
    assert "attention mechanisms" in doc.content
    assert doc.metadata["content_extractor"] == "trafilatura"
    assert doc.metadata["content_extracted"] is True
    assert doc.metadata["robots_allowed"] is True
    assert doc.metadata["fetcher"] in {"httpx", "curl_cffi"}
    # All links are absolute http(s).
    assert doc.links
    assert all(link.startswith(("http://", "https://")) for link in doc.links)
    assert "https://example.com/papers/transformer.pdf" in doc.links
    assert "https://external.example.com/related" in doc.links


@pytest.mark.asyncio
async def test_crawl_robots_denied_blocked(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler(
        {"/private/page": (200, ARTICLE_HTML)},
        robots_text=DENY_PRIVATE_ROBOTS,
    )
    try:
        doc = await crawler.crawl("https://example.com/private/page")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.BLOCKED
    assert doc.diagnostic is not None
    assert "robots" in doc.diagnostic.lower()
    assert doc.metadata["robots_allowed"] is False
    assert doc.metadata["robots_url"] == "https://example.com/robots.txt"


@pytest.mark.asyncio
async def test_crawl_robots_allowed_ok(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler(
        {"/public": (200, ARTICLE_HTML)},
        robots_text=DENY_PRIVATE_ROBOTS,
    )
    try:
        doc = await crawler.crawl("https://example.com/public")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.OK


@pytest.mark.asyncio
async def test_crawl_403_blocked_with_diagnostic(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler({"/locked": (403, "Forbidden")})
    try:
        doc = await crawler.crawl("https://example.com/locked")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.BLOCKED
    assert doc.metadata["status_code"] == 403
    assert doc.diagnostic is not None
    assert "403" in doc.diagnostic
    assert "anti-crawl" in doc.diagnostic.lower()


@pytest.mark.asyncio
async def test_crawl_challenge_page_blocked(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler({"/shield": (200, CHALLENGE_HTML)})
    try:
        doc = await crawler.crawl("https://example.com/shield")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.BLOCKED
    assert doc.diagnostic is not None
    assert "anti-crawl shield" in doc.diagnostic.lower()
    assert "challenge" in doc.diagnostic.lower() or "captcha" in doc.diagnostic.lower()


@pytest.mark.asyncio
async def test_crawl_404_failed(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler({})
    try:
        doc = await crawler.crawl("https://example.com/missing")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.FAILED
    assert doc.metadata["status_code"] == 404
    assert doc.diagnostic is not None


@pytest.mark.asyncio
async def test_crawl_invalid_url_failed(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler({})
    try:
        doc = await crawler.crawl("not a url")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.FAILED
    assert doc.diagnostic is not None
    assert "url" in doc.diagnostic.lower()


@pytest.mark.asyncio
async def test_crawl_curl_cffi_absent_falls_back_to_httpx(
    make_crawler: Callable[..., WebCrawler],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate the optional dependency being absent at runtime.
    monkeypatch.setattr(fetcher_module, "CURL_CFFI_AVAILABLE", False)
    crawler = make_crawler({"/article": (200, ARTICLE_HTML)})
    try:
        doc = await crawler.crawl("https://example.com/article")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.OK
    assert doc.metadata["fetcher"] == "httpx"
    notes = doc.metadata.get("transport_notes", [])
    assert any("curl_cffi" in str(note) for note in notes)


@pytest.mark.asyncio
async def test_crawl_browser_fetcher_unavailable_diagnostic(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler(
        {"/article": (200, ARTICLE_HTML)},
        enable_browser=True,
    )
    try:
        doc = await crawler.crawl("https://example.com/article")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.OK
    assert doc.metadata["fetcher"] == "httpx"
    notes = doc.metadata.get("transport_notes", [])
    assert any("scrapling not installed" in str(note) for note in notes)


@pytest.mark.asyncio
async def test_crawl_use_browser_flag_disabled(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler({"/article": (200, ARTICLE_HTML)})
    try:
        doc = await crawler.crawl("https://example.com/article", use_browser=True)
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.OK
    assert doc.metadata["fetcher"] == "httpx"
    notes = doc.metadata.get("transport_notes", [])
    assert any("browser fetcher disabled" in str(note) for note in notes)


@pytest.mark.asyncio
async def test_crawl_cache_hit_avoids_second_fetch() -> None:
    routes = {"/article": (200, ARTICLE_HTML)}
    transport, hits = build_transport(routes)

    crawler = WebCrawler(
        transport=transport,
        robots_checker=RobotsChecker.from_text(ALLOW_ALL_ROBOTS, user_agent=DEFAULT_USER_AGENT),
        rate_limit=1000.0,
        prefer_curl=False,
    )
    try:
        first = await crawler.crawl("https://example.com/article")
        second = await crawler.crawl("https://example.com/article")
    finally:
        await crawler.close()

    assert first.status == CrawlStatus.OK
    assert second.status == CrawlStatus.OK
    assert second.title == first.title
    assert second.content == first.content
    assert hits() == 1  # second call served from cache


@pytest.mark.asyncio
async def test_crawl_schema_rule_extraction(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    schema = SchemaSpec(
        fields=[
            SchemaField(field="heading", selector="h1", mode="css"),
            SchemaField(
                field="paragraphs",
                selector="//article/p",
                mode="xpath",
                multiple=True,
            ),
            SchemaField(
                field="pdf_href",
                selector="a[href$='.pdf']",
                mode="css",
                attribute="href",
            ),
            SchemaField(field="missing", selector=".nope", mode="css", default="N/A"),
        ]
    )
    crawler = make_crawler({"/article": (200, ARTICLE_HTML)})
    try:
        doc = await crawler.crawl("https://example.com/article", schema=schema)
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.OK
    assert doc.extracted is not None
    assert doc.extracted["heading"] == "Attention Is All You Need"
    assert len(doc.extracted["paragraphs"]) >= 2
    assert "Transformer" in " ".join(doc.extracted["paragraphs"])
    assert doc.extracted["pdf_href"] == "/papers/transformer.pdf"
    assert doc.extracted["missing"] == "N/A"
    assert doc.metadata["schema_mode"] == "rule"


@pytest.mark.asyncio
async def test_crawl_schema_llm_mode_falls_back_to_rules(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    # crawl4ai is not installed in this environment; LLM mode must degrade
    # to rule mode with a diagnostic instead of crashing.
    schema = SchemaSpec(
        fields=[SchemaField(field="heading", selector="h1", mode="css")],
        llm=True,
    )
    crawler = make_crawler({"/article": (200, ARTICLE_HTML)})
    try:
        doc = await crawler.crawl("https://example.com/article", schema=schema)
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.OK
    assert doc.extracted is not None
    assert doc.extracted["heading"] == "Attention Is All You Need"
    assert doc.metadata["schema_mode"] == "rule"
    assert doc.metadata["schema_diagnostic"] is not None
    assert "crawl4ai" in doc.metadata["schema_diagnostic"]


@pytest.mark.asyncio
async def test_crawl_plain_page_ok_no_content(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler({"/plain": (200, PLAIN_HTML)})
    try:
        doc = await crawler.crawl("https://example.com/plain")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.OK
    assert doc.metadata["content_extracted"] is False
    assert doc.content == ""


@pytest.mark.asyncio
async def test_crawl_429_blocked(
    make_crawler: Callable[..., WebCrawler],
) -> None:
    crawler = make_crawler({"/ratelimited": (429, "Too Many Requests")})
    try:
        doc = await crawler.crawl("https://example.com/ratelimited")
    finally:
        await crawler.close()

    assert doc.status == CrawlStatus.BLOCKED
    assert doc.metadata["status_code"] == 429
