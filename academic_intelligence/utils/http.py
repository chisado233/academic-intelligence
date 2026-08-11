"""HTTP client utilities with anti-crawl support.

Provides an async HTTP client with built-in rate limiting, proxy rotation,
retry logic, and user-agent rotation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from academic_intelligence.core.types import AntiCrawlStrategy
from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.proxy import ProxyPool
from academic_intelligence.utils.rate_limiter import (
    RateLimiter,
    create_rate_limiter,
)
from academic_intelligence.utils.retry import RetryConfig, RetryHandler

logger = logging.getLogger(__name__)

# Query-param names whose values must never appear in error messages or logs
# (FIX-AF F1 / AF-1): SerpAPI passes its key as ``api_key``, IEEE as
# ``apikey``.  A transport failure embeds the full request URL (params
# included) into the exception message, which then flows into
# ``SourceUnavailableError``, retry logs, ``CollectionResult.errors`` and
# ``AllSourcesFailedError``.  The list also covers the other common
# credential-style query params so a future source cannot re-introduce the
# leak through a different spelling.
_SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "key",
        "token",
        "access_token",
        "auth",
        "authorization",
        "sig",
        "signature",
        "secret",
        "password",
        "passwd",
        "client_secret",
        "credential",
    }
)

_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:"
    + "|".join(re.escape(p) for p in _SENSITIVE_QUERY_PARAMS)
    + r")=)([^&#\s]*)"
)

_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
    }
)


def redact_url_secrets(text: str) -> str:
    """Replace sensitive query-param values in *text* with ``***``.

    Applies anywhere a URL can be embedded — httpx exception messages,
    retry log lines, wrapped source errors.  Only the *values* of the
    sensitive parameters are masked (``?api_key=SECRET`` → ``?api_key=***``,
    matched case-insensitively); the rest of the URL — scheme, host, path
    and every other query parameter — survives byte-for-byte, keeping the
    error diagnosable (AF-1).
    """
    return _QUERY_SECRET_RE.sub(r"\1***", text)


def _redact_exception(exc: BaseException) -> BaseException:
    """Return an exception whose message *and HTTP attributes* are safe.

    ``httpx`` keeps the original request on transport/status exceptions, so
    rewriting ``args[0]`` alone still exposes query keys and authorization
    headers to callers.  Rebuild HTTP exceptions with a diagnostic request
    that retains method/host/path/non-secret parameters but masks credentials.
    """
    message = redact_url_secrets(str(exc))

    if isinstance(exc, httpx.HTTPStatusError):
        status_request = _redacted_request(exc.request)
        response = _redacted_response(exc.response, status_request)
        return httpx.HTTPStatusError(
            message, request=status_request, response=response
        )

    if isinstance(exc, httpx.RequestError):
        safe_request: httpx.Request | None
        try:
            safe_request = _redacted_request(exc.request)
        except RuntimeError:
            safe_request = None
        if safe_request is not None:
            try:
                return type(exc)(message, request=safe_request)
            except TypeError:
                return httpx.RequestError(message, request=safe_request)

    if exc.args and isinstance(exc.args[0], str):
        redacted = redact_url_secrets(exc.args[0])
        if redacted != exc.args[0]:
            exc.args = (redacted, *exc.args[1:])
    return exc


def _redacted_request(request: httpx.Request) -> httpx.Request:
    """Build a credential-free diagnostic copy of an HTTP request."""
    safe_headers = {
        name: "***" if name.casefold() in _SENSITIVE_HEADERS else value
        for name, value in request.headers.multi_items()
    }
    return httpx.Request(
        request.method,
        redact_url_secrets(str(request.url)),
        headers=safe_headers,
        extensions=dict(request.extensions),
    )


def _redacted_response(
    response: httpx.Response,
    request: httpx.Request,
) -> httpx.Response:
    """Build a safe status-response copy linked to the redacted request."""
    safe_headers = {
        name: (
            "***"
            if name.casefold() in _SENSITIVE_HEADERS
            else redact_url_secrets(value)
        )
        for name, value in response.headers.multi_items()
    }
    try:
        content = redact_url_secrets(response.text).encode(response.encoding or "utf-8")
    except (httpx.ResponseNotRead, UnicodeError):
        content = b""
    return httpx.Response(
        response.status_code,
        headers=safe_headers,
        content=content,
        request=request,
        extensions=dict(response.extensions),
    )

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (compatible; AcademicIntelligence/0.1; +https://github.com/paper-research-crawler)",
]


class HTTPClient:
    """Async HTTP client with anti-crawl capabilities.

    Wraps httpx.AsyncClient with:
    - Rate limiting (adaptive delays)
    - Proxy rotation
    - User-agent rotation
    - Retry with exponential backoff
    - Optional response caching
    """

    def __init__(
        self,
        strategy: AntiCrawlStrategy | None = None,
        proxies: list[str] | None = None,
        *,
        rate_limiter: RateLimiter | None = None,
        cache: Cache | None = None,
        timeout: float = 30.0,
        enable_cache: bool = True,
        requests_per_second: float | None = None,
        max_concurrent_requests: int = 4,
    ) -> None:
        """Initialize HTTP client.

        Args:
            strategy: Anti-crawl strategy configuration.
            proxies: List of proxy URLs for rotation.
            rate_limiter: Optional custom rate limiter.
            cache: Optional response cache.
            timeout: Default request timeout in seconds.
            enable_cache: Whether GET responses should be cached.
            requests_per_second: Global request-rate ceiling. When omitted,
                derive it from ``strategy.base_delay`` for compatibility.
            max_concurrent_requests: Maximum in-flight HTTP requests.
        """
        self.strategy = strategy or AntiCrawlStrategy()
        proxy_list = list(proxies or []) + list(self.strategy.proxy_pool)
        # Deduplicate
        seen: set[str] = set()
        unique_proxies: list[str] = []
        for p in proxy_list:
            if p not in seen:
                seen.add(p)
                unique_proxies.append(p)
        self.proxies = unique_proxies
        self._proxy_pool = ProxyPool(unique_proxies)
        self._rate_limiter = rate_limiter or create_rate_limiter(
            "adaptive" if self.strategy.adaptive_delay else "fixed",
            requests_per_second=(
                requests_per_second
                if requests_per_second is not None
                else 1.0 / max(self.strategy.base_delay, 0.01)
            ),
        )
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be >= 1")
        self._request_semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._cache = cache if enable_cache else None
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._request_count = 0
        self._ua_index = 0
        self._retry_handler = RetryHandler(
            RetryConfig(
                max_retries=self.strategy.max_retries,
                backoff=self.strategy.retry_backoff,
                base_delay=self.strategy.base_delay,
                jitter=self.strategy.jitter,
                retry_on_status=self.strategy.retry_on_status,
            )
        )

    async def __aenter__(self) -> HTTPClient:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def connect(self) -> None:
        """Initialize underlying HTTP client."""
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"Accept": "application/json, text/html, */*"},
        )

    async def close(self) -> None:
        """Close HTTP client and release resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HTTPClient is not connected; call connect() first")
        return self._client

    def _get_proxy(self) -> str | None:
        """Select next proxy from rotation pool."""
        if not self.proxies:
            return None
        # Rotate every N requests when configured
        if (
            self.strategy.proxy_rotation_interval > 0
            and self._request_count % self.strategy.proxy_rotation_interval == 0
        ):
            return self._proxy_pool.get_next("round_robin")
        return self._proxy_pool.get_next("round_robin")

    def _get_user_agent(self) -> str:
        """Select next user-agent from rotation pool."""
        ua = USER_AGENTS[self._ua_index % len(USER_AGENTS)]
        self._ua_index += 1
        return ua

    async def _apply_rate_limit(self) -> None:
        """Apply rate limiting delay before request."""
        await self._rate_limiter.acquire()

    def _merge_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        merged: dict[str, str] = {"User-Agent": self._get_user_agent()}
        if headers:
            merged.update(headers)
        return merged

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        use_cache: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform an HTTP request with anti-crawl measures."""
        client = self._ensure_client()

        cache_key: str | None = None
        if use_cache and self._cache is not None and method.upper() == "GET":
            cache_key = Cache.make_key(method, url, params or {})
            cached = await self._cache.get(cache_key)
            if cached is not None and isinstance(cached, dict) and "text" in cached:
                # Reconstruct a lightweight Response from cached body for GET hits.
                # The cached body is already decoded text, so strip the framing /
                # compression headers: keeping ``content-encoding`` would make
                # httpx decompress the decoded body a second time (brotli double
                # decode), and ``content-length``/``transfer-encoding`` would no
                # longer match the stored text.
                headers = {
                    key: value
                    for key, value in (cached.get("headers") or {}).items()
                    if key.lower()
                    not in {"content-encoding", "content-length", "transfer-encoding"}
                }
                request = httpx.Request(method, url, params=params)
                return httpx.Response(
                    status_code=int(cached.get("status_code", 200)),
                    headers=headers,
                    text=str(cached.get("text", "")),
                    request=request,
                )

        async def _do_request() -> httpx.Response:
            await self._apply_rate_limit()
            proxy = self._get_proxy()
            req_headers = self._merge_headers(headers)
            self._request_count += 1
            start = time.monotonic()
            try:
                # httpx 0.28+: proxy is a client-level option, not request()
                try:
                    if proxy:
                        async with httpx.AsyncClient(
                            timeout=self._timeout,
                            follow_redirects=True,
                            proxy=proxy,
                        ) as proxy_client:
                            response = await proxy_client.request(
                                method,
                                url,
                                headers=req_headers,
                                params=params,
                                json=json,
                                data=data,
                                **kwargs,
                            )
                    else:
                        response = await client.request(
                            method,
                            url,
                            headers=req_headers,
                            params=params,
                            json=json,
                            data=data,
                            **kwargs,
                        )
                except httpx.ProxyError:
                    if proxy:
                        self._proxy_pool.mark_unhealthy(proxy)
                    raise
                latency_ms = (time.monotonic() - start) * 1000.0
                await self._rate_limiter.report_status(response.status_code, latency_ms)

                if response.status_code in self.strategy.retry_on_status:
                    err = httpx.HTTPStatusError(
                        f"HTTP {response.status_code} for {url}",
                        request=response.request,
                        response=response,
                    )
                    raise err

                return response
            except BaseException as exc:
                # (FIX-AF F1 / AF-1) Redact query-param secrets (``api_key``
                # / ``apikey`` ...) from the message before it reaches the
                # RetryHandler log or the source-level ``except`` that wraps
                # it into ``SourceUnavailableError`` / ``CollectionResult``.
                safe_exc = _redact_exception(exc)
                if safe_exc is exc:
                    raise
                raise safe_exc from None

        async def _do() -> httpx.Response:
            async with self._request_semaphore:
                return await _do_request()

        response = await self._retry_handler.execute(_do)

        if (
            use_cache
            and self._cache is not None
            and method.upper() == "GET"
            and cache_key is not None
            and response.status_code == 200
        ):
            try:
                await self._cache.set(
                    cache_key,
                    {
                        "status_code": response.status_code,
                        "text": response.text,
                        "headers": dict(response.headers),
                    },
                )
            except Exception as exc:
                logger.debug("Failed to cache response: %s", exc)

        return response

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform GET request with anti-crawl measures."""
        return await self._request("GET", url, headers=headers, params=params, **kwargs)

    async def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform POST request with anti-crawl measures."""
        return await self._request(
            "POST",
            url,
            headers=headers,
            json=json,
            data=data,
            use_cache=False,
            **kwargs,
        )

    async def get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """GET and parse JSON body."""
        response = await self.get(url, headers=headers, params=params, **kwargs)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _redact_exception(exc) from None
        return response.json()
