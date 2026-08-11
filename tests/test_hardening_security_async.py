"""Regression tests for security and async-lifecycle hardening."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import academic_intelligence as ai_module
from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.exceptions import SourceFailure
from academic_intelligence.core.types import AntiCrawlStrategy, Config
from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.http import HTTPClient


@pytest.mark.asyncio
async def test_http_status_error_redacts_request_url_and_headers() -> None:
    """A rejected request must not retain credentials in exception attributes."""
    secret = "sk-ATTRIBUTE-LEAK-123"

    async def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"}, request=request)

    client = HTTPClient(
        strategy=AntiCrawlStrategy(max_retries=0, base_delay=0.0),
        enable_cache=False,
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    try:
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client.get_json(
                "https://example.test/papers",
                params={"api_key": secret, "q": "graphs"},
                headers={"Authorization": f"Bearer {secret}"},
            )
    finally:
        await client.close()

    exc = excinfo.value
    surfaces = [
        str(exc),
        str(exc.request.url),
        repr(dict(exc.request.headers)),
        str(exc.response.request.url),
        repr(dict(exc.response.request.headers)),
    ]
    assert all(secret not in surface for surface in surfaces)
    assert "api_key=***" in str(exc.request.url)
    assert exc.request.headers["authorization"] == "***"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
        TimeoutError("operation timed out"),
        TimeoutError("async operation timed out"),
    ],
)
def test_native_transport_and_timeout_failures_are_transient(exc: BaseException) -> None:
    failure = SourceFailure.from_exception(
        source="test-source",
        operation="search_papers",
        exc=exc,
    )

    assert failure.transient is True
    assert failure.permanent is False


@pytest.mark.asyncio
async def test_cache_waiter_cancellation_does_not_cancel_shared_factory() -> None:
    cache = Cache(ttl=60)
    started = asyncio.Event()
    release = asyncio.Event()
    factory_calls = 0

    async def factory() -> str:
        nonlocal factory_calls
        factory_calls += 1
        started.set()
        await release.wait()
        return "ready"

    cancelled_waiter = asyncio.create_task(cache.get_or_set("shared", factory))
    surviving_waiter = asyncio.create_task(cache.get_or_set("shared", factory))
    await started.wait()

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    release.set()
    assert await surviving_waiter == "ready"
    assert factory_calls == 1
    assert await cache.get("shared") == "ready"


@pytest.mark.asyncio
async def test_facade_concurrent_connect_initializes_one_resource_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_instances: list[object] = []
    storage_instances: list[object] = []

    class TrackingHTTP:
        def __init__(self, *args: object, **kwargs: object) -> None:
            http_instances.append(self)

        async def connect(self) -> None:
            await asyncio.sleep(0.01)

        async def close(self) -> None:
            return None

    class TrackingStorage:
        def __init__(self) -> None:
            storage_instances.append(self)

        async def connect(self) -> None:
            await asyncio.sleep(0.01)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(ai_module, "HTTPClient", TrackingHTTP)
    ai = AcademicIntelligence(Config(sources=["arxiv"], cache_enabled=False))
    monkeypatch.setattr(ai, "_build_sources", lambda sources: {})
    monkeypatch.setattr(ai, "_build_storage", lambda: TrackingStorage())

    await asyncio.gather(ai.connect(), ai.connect(), ai.connect())
    try:
        assert len(http_instances) == 1
        assert len(storage_instances) == 1
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_facade_cancelled_connect_rolls_back_partial_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_started = asyncio.Event()
    storage_release = asyncio.Event()
    closed: list[str] = []

    class TrackingHTTP:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            closed.append("http")

    class HangingStorage:
        async def connect(self) -> None:
            storage_started.set()
            await storage_release.wait()

        async def close(self) -> None:
            closed.append("storage")

    monkeypatch.setattr(ai_module, "HTTPClient", TrackingHTTP)
    ai = AcademicIntelligence(Config(sources=["arxiv"], cache_enabled=False))
    monkeypatch.setattr(ai, "_build_sources", lambda sources: {})
    monkeypatch.setattr(ai, "_build_storage", HangingStorage)

    task = asyncio.create_task(ai.connect())
    await storage_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(closed) == ["http", "storage"]
    assert ai._http is None
    assert ai._storage is None
    assert ai._connected is False
