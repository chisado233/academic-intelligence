"""Tests for utility modules."""

from __future__ import annotations

import pytest

from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.proxy import ProxyPool
from academic_intelligence.utils.rate_limiter import create_rate_limiter
from academic_intelligence.utils.retry import RetryConfig, RetryHandler


@pytest.mark.asyncio
async def test_cache_ttl(tmp_path) -> None:
    cache = Cache(ttl=60, persistent=True, persist_path=tmp_path / "c.json")
    key = Cache.make_key("get", "https://example.com", {})
    assert await cache.get(key) is None
    await cache.set(key, {"ok": True})
    assert await cache.get(key) == {"ok": True}
    assert await cache.size() == 1
    await cache.invalidate(key)
    assert await cache.get(key) is None


def test_proxy_pool_rotation() -> None:
    pool = ProxyPool(["http://a:1", "http://b:1"])
    first = pool.get_next("round_robin")
    second = pool.get_next("round_robin")
    assert first != second
    pool.mark_unhealthy(first)  # type: ignore[arg-type]
    assert pool.healthy_count == 1
    pool.mark_healthy(first)  # type: ignore[arg-type]
    assert pool.healthy_count == 2


@pytest.mark.asyncio
async def test_rate_limiter_acquire() -> None:
    limiter = create_rate_limiter("fixed", requests_per_second=100.0)
    await limiter.acquire()
    await limiter.acquire()


@pytest.mark.asyncio
async def test_retry_handler_success() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    handler = RetryHandler(RetryConfig(max_retries=3, base_delay=0.01, jitter=False))
    result = await handler.execute(flaky)
    assert result == "ok"
    assert calls["n"] == 2
