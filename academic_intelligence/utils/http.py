"""HTTP client utilities with anti-crawl support.

Provides an async HTTP client with built-in rate limiting, proxy rotation,
retry logic, and user-agent rotation.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Dict, List, Optional

import httpx

from academic_intelligence.core.types import AntiCrawlStrategy
from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.proxy import ProxyPool
from academic_intelligence.utils.rate_limiter import (
    AdaptiveRateLimiter,
    RateLimitConfig,
    RateLimiter,
    create_rate_limiter,
)
from academic_intelligence.utils.retry import RetryConfig, RetryHandler

logger = logging.getLogger(__name__)

USER_AGENTS: List[str] = [
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
        strategy: Optional[AntiCrawlStrategy] = None,
        proxies: Optional[List[str]] = None,
        *,
        rate_limiter: Optional[RateLimiter] = None,
        cache: Optional[Cache] = None,
        timeout: float = 30.0,
        enable_cache: bool = True,
    ) -> None:
        """Initialize HTTP client.

        Args:
            strategy: Anti-crawl strategy configuration.
            proxies: List of proxy URLs for rotation.
            rate_limiter: Optional custom rate limiter.
            cache: Optional response cache.
            timeout: Default request timeout in seconds.
            enable_cache: Whether GET responses should be cached.
        """
        self.strategy = strategy or AntiCrawlStrategy()
        proxy_list = list(proxies or []) + list(self.strategy.proxy_pool)
        # Deduplicate
        seen: set[str] = set()
        unique_proxies: List[str] = []
        for p in proxy_list:
            if p not in seen:
                seen.add(p)
                unique_proxies.append(p)
        self.proxies = unique_proxies
        self._proxy_pool = ProxyPool(unique_proxies)
        self._rate_limiter = rate_limiter or create_rate_limiter(
            "adaptive" if self.strategy.adaptive_delay else "fixed",
            requests_per_second=1.0 / max(self.strategy.base_delay, 0.01),
        )
        self._cache = cache if enable_cache else None
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._request_count = 0
        self._ua_index = 0
        self._retry_handler = RetryHandler(
            RetryConfig(
                max_retries=self.strategy.max_retries,
                backoff=self.strategy.retry_backoff,
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

    def _get_proxy(self) -> Optional[str]:
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

    def _merge_headers(self, headers: Optional[Dict[str, str]]) -> Dict[str, str]:
        merged: Dict[str, str] = {"User-Agent": self._get_user_agent()}
        if headers:
            merged.update(headers)
        return merged

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform an HTTP request with anti-crawl measures."""
        client = self._ensure_client()

        cache_key: Optional[str] = None
        if use_cache and self._cache is not None and method.upper() == "GET":
            cache_key = Cache.make_key(method, url, params or {})
            cached = await self._cache.get(cache_key)
            if cached is not None and isinstance(cached, dict) and "text" in cached:
                # Reconstruct a lightweight Response from cached body for GET hits
                request = httpx.Request(method, url, params=params)
                return httpx.Response(
                    status_code=int(cached.get("status_code", 200)),
                    headers=cached.get("headers") or {},
                    text=str(cached.get("text", "")),
                    request=request,
                )

        async def _do() -> httpx.Response:
            await self._apply_rate_limit()
            proxy = self._get_proxy()
            req_headers = self._merge_headers(headers)
            self._request_count += 1
            start = time.monotonic()
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
                setattr(err, "status_code", response.status_code)
                raise err

            return response

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
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform GET request with anti-crawl measures."""
        return await self._request("GET", url, headers=headers, params=params, **kwargs)

    async def post(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
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
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """GET and parse JSON body."""
        response = await self.get(url, headers=headers, params=params, **kwargs)
        response.raise_for_status()
        return response.json()
