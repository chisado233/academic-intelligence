"""FIX-F tests: HTTP cache must not double-decode brotli responses.

P23 real-network regression: the cache stores the *already decoded* ``text``
alongside the original headers (which include ``content-encoding: br``). When a
later request hits the cache, the Response is rebuilt from those headers and
httpx tries to brotli-decode the plain text again, raising
``httpx.DecodingError: brotli: decoder failed``.

All tests are offline (mock transport serving a genuinely brotli-encoded body).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import brotli  # type: ignore[import-untyped]
import httpx
import pytest

from academic_intelligence.core.types import AntiCrawlStrategy
from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.http import HTTPClient


def _client(monkeypatch: pytest.MonkeyPatch, calls: dict[str, int]) -> HTTPClient:
    strategy = AntiCrawlStrategy(max_retries=0, base_delay=0.0, adaptive_delay=False, jitter=False)
    client = HTTPClient(strategy=strategy, cache=Cache(ttl=60), timeout=5.0)
    return client


@pytest.mark.asyncio
async def test_brotli_response_cache_hit_does_not_double_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached brotli response returns decoded text/json on the cache hit.

    The first GET populates the cache with decoded text + original headers;
    the second (identical) GET is served from cache and must not attempt a
    second brotli decode of the plain-text body.
    """
    calls: dict[str, int] = {"n": 0}
    client = _client(monkeypatch, calls)
    await client.connect()
    body = brotli.compress(b'{"a": 1}')

    async def _req(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        request = httpx.Request(method, url)
        return httpx.Response(
            200,
            content=body,
            headers={"content-encoding": "br", "content-type": "application/json"},
            request=request,
        )

    assert client._client is not None
    monkeypatch.setattr(client._client, "request", _req)
    monkeypatch.setattr(client, "_apply_rate_limit", AsyncMock())

    r1 = await client.get("https://example.com/api")
    r2 = await client.get("https://example.com/api")

    assert calls["n"] == 1  # second GET served entirely from cache
    assert r1.text == r2.text == '{"a": 1}'
    assert r2.json() == {"a": 1}
    await client.close()


@pytest.mark.asyncio
async def test_old_cache_entry_with_br_header_still_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old cached entries (decoded text + ``content-encoding: br``) keep working.

    Entries persisted before the fix carry the original compression headers;
    rebuilding the Response from them must strip those headers so the decoded
    body is not decompressed a second time.
    """
    calls: dict[str, int] = {"n": 0}
    client = _client(monkeypatch, calls)
    await client.connect()

    async def _req(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        raise AssertionError("old-cache entry should be served from cache")

    assert client._client is not None
    monkeypatch.setattr(client._client, "request", _req)
    monkeypatch.setattr(client, "_apply_rate_limit", AsyncMock())

    assert client._cache is not None
    await client._cache.set(
        Cache.make_key("GET", "https://example.com/old", {}),
        {
            "status_code": 200,
            "text": '{"old": true}',
            "headers": {
                "content-encoding": "br",
                "content-type": "application/json",
                "content-length": "12",
            },
        },
    )

    r = await client.get("https://example.com/old")

    assert calls["n"] == 0
    assert r.status_code == 200
    assert r.text == '{"old": true}'
    assert r.json() == {"old": True}
    await client.close()
