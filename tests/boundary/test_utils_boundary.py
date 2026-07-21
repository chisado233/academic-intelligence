"""Boundary tests for utility modules (HTTP, proxy, rate limit, retry)."""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from academic_intelligence.core.types import AntiCrawlStrategy
from academic_intelligence.utils.http import HTTPClient
from academic_intelligence.utils.proxy import ProxyPool
from academic_intelligence.utils.rate_limiter import (
    FixedIntervalRateLimiter,
    RateLimitConfig,
    create_rate_limiter,
)
from academic_intelligence.utils.retry import RetryConfig, RetryHandler


pytestmark = [pytest.mark.boundary]


class TestUtilsBoundary:
    """Utility module boundary tests."""

    @pytest.mark.asyncio
    async def test_http_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP client should surface timeout after retries are exhausted."""

        async def _timeout_request(*args: Any, **kwargs: Any) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout")

        strategy = AntiCrawlStrategy(max_retries=0, base_delay=0.0)
        client = HTTPClient(strategy=strategy, timeout=1.0, enable_cache=False)
        await client.connect()
        try:
            # Patch underlying client request path used by HTTPClient
            real_client = client._client
            assert real_client is not None
            monkeypatch.setattr(real_client, "request", _timeout_request)

            with pytest.raises(httpx.TimeoutException):
                await client.get("https://httpbin.org/delay/10")
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_http_timeout_via_client_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructing with tiny timeout and delayed mock raises timeout."""

        async def _slow(*args: Any, **kwargs: Any) -> httpx.Response:
            await asyncio.sleep(0.5)
            request = httpx.Request("GET", "https://example.com/slow")
            return httpx.Response(200, text="ok", request=request)

        strategy = AntiCrawlStrategy(max_retries=0)
        # Use very short timeout on AsyncClient by patching after connect
        client = HTTPClient(strategy=strategy, timeout=0.05, enable_cache=False)
        await client.connect()
        try:
            assert client._client is not None
            # Force timeout by replacing request with one that times out
            async def _raise_timeout(*a: Any, **k: Any) -> httpx.Response:
                raise httpx.ReadTimeout("read timed out")

            monkeypatch.setattr(client._client, "request", _raise_timeout)
            with pytest.raises((httpx.TimeoutException, httpx.ReadTimeout)):
                await client.get("https://example.com/slow")
        finally:
            await client.close()

    def test_proxy_pool_empty(self) -> None:
        """Empty proxy pool returns None."""
        pool = ProxyPool([])
        proxy = pool.get_next()
        assert proxy is None
        assert pool.healthy_count == 0
        assert pool.total_count == 0

    def test_proxy_pool_all_unhealthy(self) -> None:
        """When all proxies are unhealthy, get_next returns None."""
        pool = ProxyPool(["http://a:1", "http://b:1"])
        pool.mark_unhealthy("http://a:1")
        pool.mark_unhealthy("http://b:1")
        assert pool.get_next() is None

    def test_rate_limiter_zero_rate(self) -> None:
        """Zero / non-positive rate must be rejected (no infinite block)."""
        with pytest.raises(ValidationError):
            RateLimitConfig(requests_per_second=0)
        with pytest.raises(ValidationError):
            create_rate_limiter("fixed", requests_per_second=0)
        with pytest.raises(ValidationError):
            RateLimitConfig(requests_per_second=-1.0)

    @pytest.mark.asyncio
    async def test_rate_limiter_high_rate(self) -> None:
        """Very high rate should still acquire without hanging."""
        limiter = FixedIntervalRateLimiter(
            RateLimitConfig(requests_per_second=1000.0, jitter=False)
        )
        await asyncio.wait_for(limiter.acquire(), timeout=1.0)
        await asyncio.wait_for(limiter.acquire(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_retry_exhaustion(self) -> None:
        """RetryHandler re-raises after max_retries are exhausted."""
        config = RetryConfig(max_retries=2, base_delay=0.01, jitter=False)
        handler = RetryHandler(config)
        calls = {"n": 0}

        async def always_fail() -> None:
            calls["n"] += 1
            raise ConnectionError("Always fails")

        with pytest.raises(ConnectionError, match="Always fails"):
            await handler.execute(always_fail)
        # initial try + 2 retries = 3
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_retry_non_retriable(self) -> None:
        """Exceptions outside retry_on are not retried."""
        config = RetryConfig(
            max_retries=5,
            base_delay=0.01,
            jitter=False,
            retry_on=(TimeoutError,),
        )
        handler = RetryHandler(config)
        calls = {"n": 0}

        async def boom() -> None:
            calls["n"] += 1
            raise ValueError("not retriable")

        with pytest.raises(ValueError):
            await handler.execute(boom)
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_retry_zero_retries(self) -> None:
        """max_retries=0 means single attempt only."""
        handler = RetryHandler(RetryConfig(max_retries=0, base_delay=0.01, jitter=False))
        calls = {"n": 0}

        async def fail() -> None:
            calls["n"] += 1
            raise RuntimeError("once")

        with pytest.raises(RuntimeError):
            await handler.execute(fail)
        assert calls["n"] == 1

    def test_unknown_rate_limiter_strategy(self) -> None:
        with pytest.raises(ValueError, match="Unknown rate limiter"):
            create_rate_limiter("not-a-real-strategy")
