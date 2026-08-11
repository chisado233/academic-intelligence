"""FIX-J tests.

J-1  Google Scholar venue parsing: "Author - 1997 - Publisher" must yield the
     publisher as venue (previously ``parts[1]`` was taken, which is the year).
J-6  HTTP 500 joins the retryable status set (was failing on first attempt).
J-4  Timeouts surface as a typed ``TimeoutError`` (source layer) instead of a
     generic ``SourceUnavailableError``.
J-3  Retry back-off ``base_delay`` is configurable and wired from the
     ``AntiCrawlStrategy``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from academic_intelligence.core.exceptions import (
    RateLimitError,
    SourceError,
    SourceUnavailableError,
    TimeoutError,
)
from academic_intelligence.core.types import AntiCrawlStrategy
from academic_intelligence.sources.google_scholar import GoogleScholarSource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.utils.http import HTTPClient
from academic_intelligence.utils.retry import RetryConfig, RetryHandler

# ---------------------------------------------------------------------------
# J-1: Google Scholar venue parsing
# ---------------------------------------------------------------------------


def _gs_item(summary: str) -> dict:
    return {
        "title": "Some Paper",
        "publication_info": {
            "authors": [{"name": "A Author"}],
            "summary": summary,
        },
        "result_id": "gs-x",
    }


def test_gs_venue_year_only_middle_part_uses_publisher() -> None:
    """'T Mitchell - 1997 - McGraw Hill': parts[1] is the year itself, so the
    venue must come from the trailing publisher segment."""
    src = GoogleScholarSource(serpapi_key="k")
    paper = src._parse_organic(
        _gs_item("T Mitchell - 1997 - McGraw Hill"), "https://scholar.google.com"
    )
    assert paper is not None
    assert paper.venue == "McGraw Hill"


def test_gs_venue_venue_year_publisher_format() -> None:
    """'Conference on X, 2019 - IEEE': no author segment, venue precedes the
    publisher and keeps its year."""
    src = GoogleScholarSource(serpapi_key="k")
    paper = src._parse_organic(
        _gs_item("Conference on X, 2019 - IEEE"), "https://scholar.google.com"
    )
    assert paper is not None
    assert paper.venue == "Conference on X, 2019"


def test_gs_venue_no_separator_returns_none() -> None:
    """A summary without ' - ' separators yields no venue."""
    src = GoogleScholarSource(serpapi_key="k")
    paper = src._parse_organic(_gs_item("NoSeparatorHere"), "https://scholar.google.com")
    assert paper is not None
    assert paper.venue is None


@pytest.mark.asyncio
async def test_gs_venue_cassette_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real cassette: 'T Mitchell - 1997 - McGraw Hill' must parse to
    'McGraw Hill' (previously None); the 'Venue, Year - Publisher' form keeps
    its venue too."""
    from tests.cassette_replay import install_cassette

    install_cassette(monkeypatch, "google_scholar_search")
    src = GoogleScholarSource(serpapi_key="test_key")
    try:
        papers = await src.search_papers("machine learning", limit=5)
    finally:
        await src.close()
    by_title = {p.title: p for p in papers}

    mitchell = by_title.get("Machine Learning")
    assert mitchell is not None
    assert mitchell.venue == "McGraw Hill"

    bishop = by_title.get("Pattern Recognition and Machine Learning")
    assert bishop is not None
    assert bishop.venue == "Springer"

    domingos = by_title.get("A Few Useful Things to Know about Machine Learning")
    assert domingos is not None
    assert domingos.venue == "Communications of the ACM"


# ---------------------------------------------------------------------------
# J-6: HTTP 500 joins the retryable status set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_client_retries_500_then_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent 500 must be retried (max_retries=2 -> 3 attempts) and then
    surface as ``SourceUnavailableError`` (previously 1 attempt)."""
    calls = {"n": 0}

    async def _always_500(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        request = httpx.Request(method, url)
        return httpx.Response(500, request=request)

    strategy = AntiCrawlStrategy(
        max_retries=2, base_delay=0.0, adaptive_delay=False, jitter=False, retry_backoff=1.0
    )
    client = HTTPClient(strategy=strategy, enable_cache=False, timeout=5.0)
    await client.connect()
    try:
        assert client._client is not None
        monkeypatch.setattr(client._client, "request", _always_500)
        monkeypatch.setattr(client, "_apply_rate_limit", AsyncMock())

        src = OpenAlexSource(http_client=client)
        with pytest.raises(SourceUnavailableError) as excinfo:
            await src._get_json("/works/W123")
        assert calls["n"] == 3  # initial try + 2 retries
        assert excinfo.value.source_name == "openalex"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_client_429_retry_behavior_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 retry semantics must not regress: still retried then surfaced as
    ``RateLimitError`` with ``Retry-After`` preserved."""
    calls = {"n": 0}

    async def _always_429(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        request = httpx.Request(method, url)
        return httpx.Response(429, headers={"Retry-After": "5"}, request=request)

    strategy = AntiCrawlStrategy(
        max_retries=2, base_delay=0.0, adaptive_delay=False, jitter=False, retry_backoff=1.0
    )
    client = HTTPClient(strategy=strategy, enable_cache=False, timeout=5.0)
    await client.connect()
    try:
        assert client._client is not None
        monkeypatch.setattr(client._client, "request", _always_429)
        monkeypatch.setattr(client, "_apply_rate_limit", AsyncMock())

        src = OpenAlexSource(http_client=client)
        with pytest.raises(RateLimitError) as excinfo:
            await src._get_json("/works/W123")
        assert calls["n"] == 3
        assert excinfo.value.retry_after == 5
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# J-4: source-layer timeouts are typed as TimeoutError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openalex_read_timeout_raises_typed_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx.ReadTimeout must surface as ``TimeoutError`` (a ``SourceError``),
    not as a generic ``SourceUnavailableError``."""

    async def _timeout_request(*args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout")

    strategy = AntiCrawlStrategy(max_retries=0, base_delay=0.0)
    client = HTTPClient(strategy=strategy, enable_cache=False)
    await client.connect()
    try:
        assert client._client is not None
        monkeypatch.setattr(client._client, "request", _timeout_request)
        monkeypatch.setattr(client, "_apply_rate_limit", AsyncMock())

        src = OpenAlexSource(http_client=client)
        with pytest.raises(TimeoutError) as excinfo:
            await src._get_json("/works/W123")
        assert excinfo.value.source_name == "openalex"
        # TimeoutError is a SourceError, so generic source handling still works
        assert isinstance(excinfo.value, SourceError)
        assert not isinstance(excinfo.value, SourceUnavailableError)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_google_scholar_connect_timeout_raises_typed_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx.ConnectTimeout must surface as ``TimeoutError`` for GS too."""

    async def _timeout_request(*args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated connect timeout")

    strategy = AntiCrawlStrategy(max_retries=0, base_delay=0.0)
    client = HTTPClient(strategy=strategy, enable_cache=False)
    await client.connect()
    try:
        assert client._client is not None
        monkeypatch.setattr(client._client, "request", _timeout_request)
        monkeypatch.setattr(client, "_apply_rate_limit", AsyncMock())

        src = GoogleScholarSource(serpapi_key="k", http_client=client)
        with pytest.raises(TimeoutError) as excinfo:
            await src.search_papers("q")
        assert excinfo.value.source_name == "google_scholar"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# J-3: retry base_delay configurability
# ---------------------------------------------------------------------------


def test_retry_config_custom_base_delay_backoff_sequence() -> None:
    """base_delay=0.1 with backoff=2.0 yields delays 0.1 / 0.2 / 0.4."""
    cfg = RetryConfig(base_delay=0.1, jitter=False, backoff=2.0)
    handler = RetryHandler(cfg)
    assert handler._calculate_delay(0) == 0.1
    assert handler._calculate_delay(1) == 0.2
    assert handler._calculate_delay(2) == 0.4


def test_retry_config_base_delay_default_is_one_second() -> None:
    """Default base_delay stays 1.0s."""
    assert RetryConfig().base_delay == 1.0


def test_http_client_passes_strategy_base_delay_into_retry_config() -> None:
    """HTTPClient must forward AntiCrawlStrategy.base_delay to RetryConfig."""
    strategy = AntiCrawlStrategy(base_delay=0.25)
    client = HTTPClient(strategy=strategy, enable_cache=False)
    assert client._retry_handler.config.base_delay == 0.25
