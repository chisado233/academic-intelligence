"""RobotsChecker tests (offline)."""

from __future__ import annotations

import httpx
import pytest

from academic_intelligence.webcrawler.fetchers import DEFAULT_USER_AGENT
from academic_intelligence.webcrawler.robots import RobotsChecker

from .fixtures import ALLOW_ALL_ROBOTS, DENY_PRIVATE_ROBOTS, build_transport


@pytest.mark.asyncio
async def test_from_text_allow() -> None:
    checker = RobotsChecker.from_text(ALLOW_ALL_ROBOTS, user_agent=DEFAULT_USER_AGENT)
    decision = await checker.check("https://example.com/anything")
    assert decision.allowed is True
    assert decision.source == "https://example.com/robots.txt"


@pytest.mark.asyncio
async def test_from_text_deny_specific_path() -> None:
    checker = RobotsChecker.from_text(DENY_PRIVATE_ROBOTS, user_agent=DEFAULT_USER_AGENT)
    denied = await checker.check("https://example.com/private/page")
    assert denied.allowed is False
    assert "disallow" in denied.reason.lower()

    allowed = await checker.check("https://example.com/public")
    assert allowed.allowed is True


@pytest.mark.asyncio
async def test_real_mode_serves_robots_txt() -> None:
    transport, _ = build_transport({"/robots.txt": (200, DENY_PRIVATE_ROBOTS)})
    checker = RobotsChecker(
        user_agent=DEFAULT_USER_AGENT,
        transport=transport,
    )
    try:
        decision = await checker.check("https://example.com/private/x")
    finally:
        await checker.close()

    assert decision.allowed is False
    assert decision.status == 200
    assert decision.source == "https://example.com/robots.txt"


@pytest.mark.asyncio
async def test_real_mode_missing_robots_fails_open() -> None:
    # No robots.txt route → 404 → no policy → allowed with a note.
    transport, _ = build_transport({})
    checker = RobotsChecker(
        user_agent=DEFAULT_USER_AGENT,
        transport=transport,
    )
    try:
        decision = await checker.check("https://example.com/x")
    finally:
        await checker.close()

    assert decision.allowed is True
    assert "unavailable" in decision.source
    assert "fail-open" in decision.reason


@pytest.mark.asyncio
async def test_real_mode_network_failure_fail_closed() -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(failing_handler)
    checker = RobotsChecker(
        user_agent=DEFAULT_USER_AGENT,
        transport=transport,
        allow_if_unavailable=False,
    )
    try:
        decision = await checker.check("https://example.com/x")
    finally:
        await checker.close()

    assert decision.allowed is False
    assert "fail-closed" in decision.reason


@pytest.mark.asyncio
async def test_real_mode_network_failure_fail_open() -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(failing_handler)
    checker = RobotsChecker(
        user_agent=DEFAULT_USER_AGENT,
        transport=transport,
        allow_if_unavailable=True,
    )
    try:
        decision = await checker.check("https://example.com/x")
    finally:
        await checker.close()

    assert decision.allowed is True
    assert "fail-open" in decision.reason


@pytest.mark.asyncio
async def test_check_is_memoized_per_url() -> None:
    routes = {"/robots.txt": (200, DENY_PRIVATE_ROBOTS)}
    transport, hits = build_transport(routes)
    checker = RobotsChecker(
        user_agent=DEFAULT_USER_AGENT,
        transport=transport,
    )
    try:
        first = await checker.check("https://example.com/private/a")
        second = await checker.check("https://example.com/private/a")
        other = await checker.check("https://example.com/private/b")
    finally:
        await checker.close()

    assert first.allowed is False
    assert second is first  # memoized
    assert other.allowed is False
    # robots.txt fetched once per origin, not per URL.
    assert hits() == 1
