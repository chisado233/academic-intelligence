"""WebCrawler orchestration (WP3 webcrawler layer).

:class:`WebCrawler.crawl` implements the pipeline from
technical-design.md §1.2:

1. cache lookup (cache first — same URL is not re-fetched within the TTL)
2. robots.txt pre-check (denial → ``blocked``, never fetched past)
3. fetcher selection (static → curl_cffi/httpx; JS-required page →
   optional Scrapling browser fetcher)
4. anti-crawl interception checks (403 / challenge / captcha pages →
   ``blocked`` with diagnostics — the red line stops here, no escalation)
5. Trafilatura main-text extraction
6. schema extraction (CSS/XPath rules; optional Crawl4AI LLM mode)

Every outcome — success, blocked, or failure — is returned as a
:class:`WebDocument`; ``crawl`` itself does not raise for per-page problems.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

import httpx

from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.http import redact_url_secrets
from academic_intelligence.utils.rate_limiter import (
    RateLimiter,
    create_rate_limiter,
)

from .extractors import (
    RuleSchemaExtractor,
    SchemaExtractionError,
    crawl4ai_available,
    extract_content,
    extract_llm_schema,
)
from .fetchers import (
    DEFAULT_MAX_PAGE_BYTES,
    DEFAULT_USER_AGENT,
    BrowserFetcher,
    CurlFetcher,
    FetchTooLargeError,
    FetchTransportError,
    HTTPFetcher,
)
from .models import (
    CrawlCacheRecord,
    CrawlCacheStore,
    CrawlStatus,
    SchemaSpec,
    WebDocument,
    utc_now_iso,
)
from .robots import RobotsChecker

logger = logging.getLogger(__name__)

# Body markers of anti-crawl shield / challenge / captcha pages (matched
# case-insensitively against the first chunk of decoded text).  Hitting one
# classifies the crawl as ``blocked`` per the red line — the crawler never
# attempts to solve or bypass these.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "cf-challenge",
    "__cf_chl_",
    "cf-browser-verification",
    "just a moment",
    "checking your browser",
    "verify you are human",
    "verify you're human",
    "verify your identity",
    "captcha",
    "recaptcha",
    "hcaptcha",
    "access denied",
    "attention required",
    "request blocked",
    "unusual traffic",
    "you have been blocked",
    "enable javascript and cookies to continue",
    "please enable javascript and cookies",
)

# Anti-crawl HTTP statuses → ``blocked`` (with diagnostics).  429 is treated
# as a target-side rate-limit wall: polite mode stops rather than hammers.
_BLOCKED_STATUS_CODES: frozenset[int] = frozenset({401, 403, 429})

# Conservative JS-required page heuristics (SPA root containers).  A page
# must contain one of these AND look client-rendered to trigger the optional
# browser upgrade — server-rendered sites with an ``#app`` div are left alone.
_SPA_ROOT_MARKERS: tuple[str, ...] = (
    '<div id="app"',
    '<div id="root"',
    '<div id="__next"',
    '<div id="__nuxt"',
    '<div id="main-container"',
    '<div id="application"',
)

_NOSCRIPT_JS_REQUIRED_RE = re.compile(
    r"<noscript[^>]*>.*?(enable javascript|javascript is required|"
    r"please enable javascript|javascript must be enabled).*?</noscript>",
    re.IGNORECASE | re.DOTALL,
)


def _looks_like_js_required(html: str) -> bool:
    """Heuristic: is *html* a client-rendered page that needs JS execution?"""
    head = html[:20000].lower()
    if not any(marker in head for marker in _SPA_ROOT_MARKERS):
        return False
    if _NOSCRIPT_JS_REQUIRED_RE.search(head) is not None:
        return True
    # No meaningful server-side text besides a near-empty root container.
    text = re.sub(r"<[^>]+>", " ", head)
    return len(text.strip()) < 200


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _robots_url(url: str) -> str:
    """Return the conventional ``<origin>/robots.txt`` URL for diagnostics."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/robots.txt"


def _detect_challenge(html: str) -> str | None:
    """Return the matched challenge/captcha marker, or ``None``.

    Scans the first 64 KB of decoded text plus the ``<title>`` so a shield
    page whose body is a giant script still gets caught via its title.
    """
    sample = html[:65536].lower()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if title_match is not None:
        sample += "\n" + title_match.group(1).lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in sample:
            return marker
    return None


class WebCrawler:
    """Polite web page crawler producing :class:`WebDocument` results.

    Args:
        user_agent: User-Agent header; defaults to the polite-mode banner
            ``paper-research-crawler/0.2 (learning use)``.
        timeout: Request timeout in seconds.
        rate_limit: Global request ceiling (requests/second).
        max_concurrency: Cap on concurrent in-flight fetches.
        cache: Optional :class:`Cache` for crawled documents; a default
            in-memory one is created when ``enable_cache`` is true.
        cache_ttl: TTL (seconds) for the default cache.
        enable_cache: Master switch for the crawl document cache.
        transport: Optional httpx transport (tests inject ``MockTransport``).
        robots_checker: Optional :class:`RobotsChecker`; a real one sharing
            *transport* is created when omitted.
        enable_browser: Allow the optional Scrapling browser fetcher for
            JS-required pages (heavy optional dependency, import-detected).
        prefer_curl: Prefer ``curl_cffi`` over httpx for static pages when
            the optional dependency is installed.
        max_page_bytes: Body-size cap per page.
        enable_robots: Whether to pre-check ``robots.txt`` before fetching
            (denial → ``blocked``).  Polite-mode default is ``True``;
            disabling it is not recommended.
        cache_store: Optional persistent ``crawl_cache`` store
            (:class:`CrawlCacheStore`); when configured, every crawl outcome
            (ok/blocked/failed) is recorded and previous successful crawls
            are reused across sessions (technical-design.md §2), replacing
            the pure in-memory cache as the cross-session record.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        rate_limit: float = 1.0,
        max_concurrency: int = 4,
        cache: Cache | None = None,
        cache_ttl: int = 3600,
        enable_cache: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        robots_checker: RobotsChecker | None = None,
        enable_browser: bool = False,
        prefer_curl: bool = True,
        max_page_bytes: int | None = DEFAULT_MAX_PAGE_BYTES,
        enable_robots: bool = True,
        cache_store: CrawlCacheStore | None = None,
    ) -> None:
        """Initialize the crawler (see class docstring for parameters)."""
        if rate_limit <= 0:
            raise ValueError("rate_limit must be > 0")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._user_agent = user_agent
        self._timeout = timeout
        self._max_page_bytes = max_page_bytes
        self._enable_browser = enable_browser
        self._prefer_curl = prefer_curl
        self._enable_robots = enable_robots
        self._cache_store = cache_store
        self._rate_limiter: RateLimiter = create_rate_limiter(
            "fixed", requests_per_second=rate_limit
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cache: Cache | None
        if enable_cache:
            self._cache = cache if cache is not None else Cache(ttl=cache_ttl)
        else:
            self._cache = None
        self._transport = transport
        self._robots = robots_checker or RobotsChecker(
            user_agent=user_agent,
            timeout=timeout,
            transport=transport,
            rate_limiter=self._rate_limiter,
        )
        self._http_fetcher = HTTPFetcher(
            user_agent=user_agent,
            timeout=timeout,
            transport=transport,
            rate_limiter=self._rate_limiter,
            max_bytes=max_page_bytes,
        )
        self._curl_fetcher = CurlFetcher(
            user_agent=user_agent,
            timeout=timeout,
            max_bytes=max_page_bytes,
        )
        self._browser_fetcher = BrowserFetcher(
            user_agent=user_agent,
            timeout=timeout,
            max_bytes=max_page_bytes,
        )
        self._schema_extractor = RuleSchemaExtractor()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def crawl(
        self,
        url: str,
        schema: SchemaSpec | None = None,
        *,
        use_browser: bool = False,
    ) -> WebDocument:
        """Crawl *url* through the full §1.2 pipeline.

        Args:
            url: Target HTTP(S) URL.
            schema: Optional structured-extraction spec (rule mode; ``llm``
                enables the optional Crawl4AI pass when installed).
            use_browser: Force the browser fetcher for this page (subject
                to ``enable_browser`` and Scrapling availability).

        Returns:
            A :class:`WebDocument`; ``blocked``/``failed`` outcomes carry a
            diagnostic in ``metadata["diagnostic"]``.  Never raises for a
            per-page failure.
        """
        try:
            async with self._semaphore:
                document = await self._crawl_inner(
                    url, schema, use_browser=use_browser
                )
        except Exception as exc:  # pragma: no cover - defensive outer guard
            logger.exception("unexpected crawl failure for %s", url)
            document = self._make_failed(url, f"unexpected crawl error: {exc!r}")
        # Record every terminal outcome (ok/blocked/failed) into the
        # persistent crawl_cache when a store is configured (IM-5); the
        # in-memory cache only serves successful crawls (see _cache_set).
        await self._cache_set(url, document)
        return document

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def _crawl_inner(
        self,
        url: str,
        schema: SchemaSpec | None,
        *,
        use_browser: bool,
    ) -> WebDocument:
        url = url.strip()
        if not _is_http_url(url):
            return self._make_failed(url, "not an absolute http(s) URL")

        cached = await self._cache_get(url)
        if cached is not None:
            return cached

        # 1. robots pre-check — denial is terminal (skipped when the
        #    enable_robots switch is turned off, which is not recommended).
        if self._enable_robots:
            decision = await self._robots.check(url)
            if not decision.allowed:
                return self._make_blocked(
                    url,
                    f"robots.txt pre-check denied crawling: {decision.reason}",
                    robots_allowed=False,
                    robots_url=decision.source,
                )

        # 2. fetch (static transport, or browser when JS is required).
        result = await self._fetch(url, use_browser=use_browser)

        # 3. anti-crawl interception checks — red line, stop on blocked.
        if result.status_code in _BLOCKED_STATUS_CODES:
            return self._make_blocked(
                url,
                f"anti-crawl interception: HTTP {result.status_code} "
                f"{result.reason.strip()}".rstrip(),
                status_code=result.status_code,
                robots_allowed=True,
            )
        marker = _detect_challenge(result.text)
        if marker is not None:
            return self._make_blocked(
                url,
                "anti-crawl shield detected "
                f"(challenge/captcha marker {marker!r}); stopping per red-line "
                "policy — no automatic challenge solving",
                status_code=result.status_code,
                robots_allowed=True,
            )
        if result.status_code >= 400:
            return self._make_failed(
                url,
                f"HTTP {result.status_code} {result.reason.strip()}".rstrip(),
                status_code=result.status_code,
            )

        # 4. content extraction (Trafilatura).
        extraction = extract_content(result.text, result.url)

        # 5. schema extraction (rule mode; optional LLM mode).
        extracted: dict[str, Any] | None = None
        schema_mode: str | None = None
        schema_diagnostic: str | None = None
        if schema is not None:
            if schema.llm and crawl4ai_available():
                schema_mode = "llm"
                llm_result = await extract_llm_schema(result.url, schema)
                if llm_result is not None:
                    extracted = llm_result
                else:
                    schema_diagnostic = "crawl4ai LLM extraction returned no result; using rules"
            elif schema.llm:
                schema_diagnostic = (
                    "crawl4ai not installed; using rule-mode extraction "
                    "(install the [crawler] extra to enable LLM mode)"
                )
            if extracted is None:
                try:
                    extracted = self._schema_extractor.extract(result.text, schema)
                    schema_mode = schema_mode or "rule"
                except SchemaExtractionError as exc:
                    schema_diagnostic = str(exc)
                    schema_mode = schema_mode or "rule"

        metadata: dict[str, Any] = {
            "status_code": result.status_code,
            "reason": result.reason,
            "fetcher": result.fetcher,
            "fetched_at": utc_now_iso(),
            "content_extractor": extraction.extractor,
            "content_extracted": extraction.content_extracted,
            "links_count": len(extraction.links),
            "robots_allowed": True,
            "robots_url": _robots_url(url),
        }
        if result.notes:
            metadata["transport_notes"] = list(result.notes)
        if schema_mode is not None:
            metadata["schema_mode"] = schema_mode
        if schema_diagnostic is not None:
            metadata["schema_diagnostic"] = schema_diagnostic

        document = WebDocument(
            url=url,
            status=CrawlStatus.OK,
            title=extraction.title,
            content=extraction.content,
            links=extraction.links,
            metadata=metadata,
            extracted=extracted,
        )
        return document

    async def _fetch(
        self,
        url: str,
        *,
        use_browser: bool,
    ) -> Any:
        """Select and run a fetcher; return a :class:`FetchResult`.

        Selection order: browser fetcher (only when explicitly requested or
        the page looks JS-required, and only when enabled/available) →
        curl_cffi (when installed and preferred) → httpx.
        """
        browser_notes: list[str] = []
        if use_browser:
            if not self._enable_browser:
                browser_notes.append("browser fetcher disabled (enable_browser=False)")
            elif not self._browser_fetcher.available:
                browser_notes.append(
                    "scrapling not installed; install the [crawler] extra for JS-rendered pages"
                )
            else:
                return await self._browser_fetcher.fetch(url, timeout=self._timeout)
        elif self._enable_browser and not self._browser_fetcher.available:
            browser_notes.append(
                "scrapling not installed; install the [crawler] extra for JS-rendered pages"
            )

        if self._prefer_curl and self._curl_fetcher.available:
            try:
                return await self._curl_fetcher.fetch(url, timeout=self._timeout)
            except FetchTooLargeError:
                raise
            except FetchTransportError as exc:
                # curl_cffi transport hiccup → fall back to httpx, do not fail.
                logger.info("curl_cffi fallback for %s: %s", url, exc)
                browser_notes.append(f"curl_cffi transport failed, using httpx: {exc}")
        elif not self._curl_fetcher.available:
            browser_notes.append(
                "curl_cffi not installed; using httpx transport (install the "
                "[crawler] extra to enable the TLS-fingerprint fetcher)"
            )

        result = await self._http_fetcher.fetch(url, timeout=self._timeout)

        # Auto-upgrade to the browser fetcher when the static page looks
        # client-rendered (only when the optional browser is enabled).
        if (
            self._enable_browser
            and self._browser_fetcher.available
            and _looks_like_js_required(result.text)
        ):
            try:
                rendered = await self._browser_fetcher.fetch(url, timeout=self._timeout)
                if rendered.status_code not in _BLOCKED_STATUS_CODES:
                    browser_notes.append("page looked JS-required; browser-rendered")
                    return replace(
                        rendered,
                        notes=tuple(browser_notes) + rendered.notes,
                    )
            except FetchTransportError as exc:
                logger.info("browser render failed for %s: %s", url, exc)
                browser_notes.append(f"browser render failed, using static page: {exc}")

        if browser_notes:
            return replace(result, notes=tuple(browser_notes) + result.notes)
        return result

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    async def _cache_get(self, url: str) -> WebDocument | None:
        """Return a cached document for *url* (memory first, store fallback).

        The in-memory cache serves successful crawls within the TTL (the
        pre-existing semantics).  When a persistent :class:`CrawlCacheStore`
        is configured, a *previous successful* crawl is also reused across
        sessions (technical-design.md §2); ``blocked``/``failed`` rows are
        recorded for observability but never served as hits.
        """
        if self._cache is not None:
            raw = await self._cache.get(url)
            if raw is not None:
                try:
                    return WebDocument.model_validate(raw)
                except Exception as exc:
                    logger.debug(
                        "discarding malformed crawl cache entry for %s: %s", url, exc
                    )
        if self._cache_store is not None:
            try:
                record = await self._cache_store.get_crawl_cache(url)
            except Exception as exc:  # pragma: no cover - defensive store failure
                logger.debug("failed to read crawl cache for %s: %s", url, exc)
                return None
            if (
                record is not None
                and record.status == CrawlStatus.OK.value
                and record.web_doc
            ):
                try:
                    return WebDocument.model_validate(record.web_doc)
                except Exception as exc:
                    logger.debug(
                        "discarding malformed persisted crawl cache for %s: %s",
                        url,
                        exc,
                    )
        return None

    async def _cache_set(self, url: str, document: WebDocument) -> None:
        """Cache *document* for *url* (memory for OK, store for every outcome).

        Only successful crawls enter the in-memory TTL cache (unchanged
        semantics — blocked/failed pages are re-attempted within a session).
        Every terminal outcome is recorded into the persistent crawl_cache
        store when one is configured (IM-5), so crawl status
        (ok/blocked/failed) is queryable across sessions.
        """
        if self._cache is not None and document.status == CrawlStatus.OK:
            try:
                await self._cache.set(url, document.model_dump(mode="json"))
            except Exception as exc:  # pragma: no cover - defensive cache failure
                logger.debug("failed to cache crawl result for %s: %s", url, exc)
        if self._cache_store is not None:
            try:
                await self._cache_store.save_crawl_cache(
                    CrawlCacheRecord(
                        url=url,
                        status=document.status.value,
                        fetched_at=document.metadata.get("fetched_at"),
                        web_doc=document.model_dump(mode="json"),
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive store failure
                logger.debug("failed to persist crawl cache for %s: %s", url, exc)

    # ------------------------------------------------------------------
    # Result builders
    # ------------------------------------------------------------------

    def _base_doc(
        self,
        url: str,
        status: CrawlStatus,
        diagnostic: str,
        *,
        status_code: int | None = None,
        robots_allowed: bool | None = None,
        robots_url: str | None = None,
        fetcher: str = "",
    ) -> WebDocument:
        metadata: dict[str, Any] = {
            "fetcher": fetcher,
            "fetched_at": utc_now_iso(),
            "diagnostic": redact_url_secrets(diagnostic),
        }
        if status_code is not None:
            metadata["status_code"] = status_code
        if robots_allowed is not None:
            metadata["robots_allowed"] = robots_allowed
        if robots_url is not None:
            metadata["robots_url"] = robots_url
        return WebDocument(url=url, status=status, metadata=metadata)

    def _make_blocked(
        self,
        url: str,
        diagnostic: str,
        *,
        status_code: int | None = None,
        robots_allowed: bool | None = None,
        robots_url: str | None = None,
    ) -> WebDocument:
        return self._base_doc(
            url,
            CrawlStatus.BLOCKED,
            diagnostic,
            status_code=status_code,
            robots_allowed=robots_allowed,
            robots_url=robots_url,
        )

    def _make_failed(
        self,
        url: str,
        diagnostic: str,
        *,
        status_code: int | None = None,
    ) -> WebDocument:
        return self._base_doc(
            url,
            CrawlStatus.FAILED,
            diagnostic,
            status_code=status_code,
            robots_allowed=False,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Release fetcher and robots-checker resources."""
        await self._http_fetcher.close()
        await self._robots.close()

    async def __aenter__(self) -> WebCrawler:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
