"""WebCrawler layer (WP3): polite web page crawling with extraction.

Public API::

    from academic_intelligence.webcrawler import WebCrawler, SchemaSpec

    async with WebCrawler() as crawler:
        doc = await crawler.crawl("https://example.com/article", schema=schema)

:class:`~academic_intelligence.webcrawler.crawler.WebCrawler` implements the
robots pre-check → fetcher selection → Trafilatura extraction → schema
extraction pipeline (technical-design.md §1.2).  Outcomes are always
:class:`WebDocument` objects with a ``status`` of ``ok``/``blocked``/``failed``;
anti-crawl interceptions (403 / challenge / captcha) and robots denials are
``blocked`` and never escalated (red line, functional-design.md §6.2).
"""

from .crawler import WebCrawler
from .fetchers import (
    DEFAULT_MAX_PAGE_BYTES,
    DEFAULT_USER_AGENT,
    FetchResult,
    HTTPFetcher,
)
from .models import (
    CrawlCacheRecord,
    CrawlCacheStore,
    CrawlStatus,
    SchemaField,
    SchemaSpec,
    WebDocument,
)
from .robots import RobotsChecker, RobotsDecision

__all__ = [
    "CrawlCacheRecord",
    "CrawlCacheStore",
    "CrawlStatus",
    "DEFAULT_MAX_PAGE_BYTES",
    "DEFAULT_USER_AGENT",
    "FetchResult",
    "HTTPFetcher",
    "RobotsChecker",
    "RobotsDecision",
    "SchemaField",
    "SchemaSpec",
    "WebCrawler",
    "WebDocument",
]
