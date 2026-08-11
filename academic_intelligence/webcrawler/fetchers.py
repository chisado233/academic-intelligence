"""Fetcher layer for the webcrawler (WP3).

Fetchers are the transport abstraction of the crawler:

- :class:`HTTPFetcher` — default static fetcher, backed by ``httpx``
  (a formal dependency).
- :class:`CurlFetcher` — optional TLS-fingerprint transport backed by
  ``curl_cffi`` (import-detected; the webcrawler degrades to
  :class:`HTTPFetcher` when absent).  Wraps
  :mod:`academic_intelligence.utils.curl_fetcher`.
- :class:`BrowserFetcher` — optional JS-rendering transport backed by
  Scrapling (import-detected, heavy optional dependency).

Anti-detection boundary (red line, technical-design.md §1.2 /
functional-design.md §6.2): curl_cffi / Scrapling are only used for public
pages that *need* JS rendering or basic anti-bot handling.  When the target
answers with a challenge page, captcha or 403 anti-crawl interception the
crawler marks the document ``blocked`` and stops — no automatic challenge
solving, no captcha bypass.  This module only transports bytes; the
``blocked`` decision lives in the crawler orchestration.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from academic_intelligence.utils.curl_fetcher import (
    CURL_CFFI_AVAILABLE,
)
from academic_intelligence.utils.curl_fetcher import (
    CurlFetcher as SyncCurlFetcher,
)
from academic_intelligence.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Default polite-mode user agent (functional-design.md §6.3).
DEFAULT_USER_AGENT = "paper-research-crawler/0.2 (learning use)"

# Upper bound for a single fetched page (bytes).  Prevents a pathological
# response from exhausting memory before Trafilatura parses it.
DEFAULT_MAX_PAGE_BYTES = 10 * 1024 * 1024


class FetchTooLargeError(Exception):
    """Raised when a page body exceeds the configured byte limit."""


class FetchTransportError(Exception):
    """Raised when a transport cannot complete the request.

    Wraps httpx/curl_cffi/Scrapling failures into one type the crawler
    turns into a ``failed`` document with a diagnostic.
    """


@dataclass(frozen=True)
class FetchResult:
    """Normalized outcome of one fetch.

    Attributes:
        url: Final URL after redirects.
        status_code: HTTP status code.
        headers: Response headers.
        text: Decoded body text.
        fetcher: Transport name — ``"httpx"`` / ``"curl_cffi"`` / ``"scrapling"``.
        reason: HTTP reason phrase.
        notes: Human-readable transport notes (e.g. optional-dependency
            fallback reasons), surfaced in ``WebDocument.metadata``.
    """

    url: str
    status_code: int
    headers: dict[str, str]
    text: str
    fetcher: str
    reason: str = ""
    notes: tuple[str, ...] = ()


class BaseFetcher(ABC):
    """Common contract for all fetchers."""

    name: str = ""

    @abstractmethod
    async def fetch(self, url: str, *, timeout: float | None = None) -> FetchResult:
        """Fetch *url* and return a normalized :class:`FetchResult`.

        Raises:
            FetchTransportError: Transport-level failure.
            FetchTooLargeError: Body exceeds the byte limit.
        """
        raise NotImplementedError

    def _check_size(self, content: bytes, *, max_bytes: int | None) -> None:
        if max_bytes is not None and len(content) > max_bytes:
            raise FetchTooLargeError(f"page body {len(content)} bytes exceeds limit {max_bytes}")


class HTTPFetcher(BaseFetcher):
    """Default static fetcher backed by ``httpx``.

    Applies the polite-mode user agent, follows redirects, and acquires the
    shared :class:`RateLimiter` before each request so the global 1 req/s
    default holds across the whole crawler.
    """

    name = "httpx"

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: RateLimiter | None = None,
        max_bytes: int | None = DEFAULT_MAX_PAGE_BYTES,
    ) -> None:
        """Initialize the fetcher.

        Args:
            user_agent: User-Agent header value.
            timeout: Request timeout in seconds.
            transport: Optional httpx transport (tests inject ``MockTransport``).
            rate_limiter: Optional shared rate limiter.
            max_bytes: Optional body-size cap; ``None`` disables it.
        """
        self._user_agent = user_agent
        self._timeout = timeout
        self._transport = transport
        self._rate_limiter = rate_limiter
        self._max_bytes = max_bytes
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                transport=self._transport,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
        return self._client

    async def close(self) -> None:
        """Release the underlying httpx client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str, *, timeout: float | None = None) -> FetchResult:
        client = await self._ensure_client()
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire()
        try:
            response = await client.get(url, timeout=timeout)
        except httpx.RequestError as exc:
            raise FetchTransportError(f"httpx GET {url} failed: {exc}") from exc
        self._check_size(response.content, max_bytes=self._max_bytes)
        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
            fetcher=self.name,
            reason=response.reason_phrase or "",
        )


class CurlFetcher(BaseFetcher):
    """Optional TLS-fingerprint fetcher backed by ``curl_cffi``.

    Degrades cleanly when ``curl_cffi`` is absent: :attr:`available` is
    ``False`` and the crawler falls back to :class:`HTTPFetcher`.
    """

    name = "curl_cffi"

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        impersonate: str = "chrome",
        max_bytes: int | None = DEFAULT_MAX_PAGE_BYTES,
    ) -> None:
        """Initialize the fetcher (no-op if ``curl_cffi`` is missing).

        Args:
            user_agent: User-Agent header value.
            timeout: Request timeout in seconds.
            impersonate: curl_cffi ``impersonate`` target.
            max_bytes: Optional body-size cap; ``None`` disables it.
        """
        self._user_agent = user_agent
        self._timeout = timeout
        self._impersonate = impersonate
        self._max_bytes = max_bytes
        self._sync: SyncCurlFetcher | None = None

    @property
    def available(self) -> bool:
        """Whether the optional ``curl_cffi`` dependency is installed."""
        return CURL_CFFI_AVAILABLE

    def _get_sync(self) -> SyncCurlFetcher:
        if self._sync is None:
            self._sync = SyncCurlFetcher(
                timeout=self._timeout,
                user_agent=self._user_agent,
                impersonate=self._impersonate,
            )
        return self._sync

    async def fetch(self, url: str, *, timeout: float | None = None) -> FetchResult:
        if not self.available:
            raise FetchTransportError(
                "curl_cffi is not installed; falling back to the httpx transport"
            )
        sync = self._get_sync()
        try:
            result = await asyncio.to_thread(sync.fetch, url, timeout=timeout)
        except Exception as exc:
            raise FetchTransportError(f"curl_cffi GET {url} failed: {exc}") from exc
        self._check_size(result.text.encode("utf-8"), max_bytes=self._max_bytes)
        return FetchResult(
            url=result.url,
            status_code=result.status_code,
            headers=result.headers,
            text=result.text,
            fetcher=self.name,
            reason=result.reason,
        )


_SCRAPLING_AVAILABLE: bool = False
try:
    import importlib.util

    _SCRAPLING_AVAILABLE = importlib.util.find_spec("scrapling") is not None
except Exception:  # pragma: no cover - find_spec is robust, defensive anyway
    _SCRAPLING_AVAILABLE = False


def _scrapling_classes() -> tuple[Any, Any] | None:
    """Import Scrapling fetchers lazily; return ``None`` when unavailable.

    Returns ``(DynamicFetcher, StealthyFetcher)`` when ``scrapling`` is
    importable, otherwise ``None``.
    """
    if not _SCRAPLING_AVAILABLE:
        return None
    try:
        from scrapling.fetchers import (  # type: ignore[import-not-found]
            DynamicFetcher,
            StealthyFetcher,
        )

        return (DynamicFetcher, StealthyFetcher)
    except Exception:  # pragma: no cover - defensive import failure
        logger.warning("scrapling import failed; browser fetcher disabled")
        return None


class BrowserFetcher(BaseFetcher):
    """Optional JS-rendering fetcher backed by Scrapling.

    Used *only* when the crawler detects a JS-required public page AND
    ``enable_browser=True`` was requested.  The red line still applies:
    any challenge/captcha/403 response is reported ``blocked`` downstream.
    """

    name = "scrapling"

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        headless: bool = True,
        max_bytes: int | None = DEFAULT_MAX_PAGE_BYTES,
    ) -> None:
        """Initialize the fetcher.

        Args:
            user_agent: User-Agent header value.
            timeout: Browser navigation timeout in seconds.
            headless: Run the browser headless.
            max_bytes: Optional body-size cap; ``None`` disables it.
        """
        self._user_agent = user_agent
        self._timeout = timeout
        self._headless = headless
        self._max_bytes = max_bytes

    @property
    def available(self) -> bool:
        """Whether the optional ``scrapling`` dependency is installed."""
        return _SCRAPLING_AVAILABLE and _scrapling_classes() is not None

    async def fetch(self, url: str, *, timeout: float | None = None) -> FetchResult:
        classes = _scrapling_classes()
        if classes is None:
            raise FetchTransportError(
                "scrapling is not installed; install the [crawler] extra to "
                "enable JS-rendered page fetching"
            )
        dynamic_cls, _stealthy_cls = classes

        def _run() -> Any:
            fetcher = dynamic_cls(
                headless=self._headless,
                user_agent=self._user_agent,
                timeout=timeout if timeout is not None else self._timeout,
            )
            return fetcher.get(url)

        try:
            response: Any = await asyncio.to_thread(_run)
        except Exception as exc:
            raise FetchTransportError(f"scrapling GET {url} failed: {exc}") from exc

        html: str = ""
        try:
            html = str(response.body or response.html or response.content or "")
        except Exception:  # pragma: no cover - attribute access varies by version
            logger.warning("scrapling response body read failed for %s", url)
        raw_bytes = html.encode("utf-8")
        self._check_size(raw_bytes, max_bytes=self._max_bytes)
        status_code: int = 200
        try:
            status_code = int(getattr(response, "status", 200) or 200)
        except (TypeError, ValueError):  # pragma: no cover
            status_code = 200
        return FetchResult(
            url=url,
            status_code=status_code,
            headers={},
            text=html,
            fetcher=self.name,
            reason="",
        )


__all__ = [
    "BaseFetcher",
    "BrowserFetcher",
    "CurlFetcher",
    "DEFAULT_MAX_PAGE_BYTES",
    "DEFAULT_USER_AGENT",
    "FetchResult",
    "FetchTooLargeError",
    "FetchTransportError",
    "HTTPFetcher",
]
