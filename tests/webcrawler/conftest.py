"""Shared fixtures for webcrawler tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from academic_intelligence.webcrawler import WebCrawler
from academic_intelligence.webcrawler.fetchers import DEFAULT_USER_AGENT
from academic_intelligence.webcrawler.robots import RobotsChecker

from .fixtures import ALLOW_ALL_ROBOTS, build_transport


@pytest.fixture
def routes_factory() -> Callable[..., tuple[httpx.MockTransport, Callable[[], int]]]:
    return build_transport


@pytest.fixture
def make_crawler() -> Callable[..., WebCrawler]:
    """Factory building an offline WebCrawler.

    Keyword args mirror ``WebCrawler.__init__``; ``routes`` (path → status,
    body) and ``robots_text`` are test conveniences.  A ``robots_text`` of
    ``None`` builds a fixture-mode allow-all checker.
    """

    def _make(
        routes: dict[str, tuple[int, str]] | None = None,
        *,
        robots_text: str | None = ALLOW_ALL_ROBOTS,
        **kwargs: Any,
    ) -> WebCrawler:
        routes = routes or {}
        transport, _ = build_transport(routes)
        text = robots_text if robots_text is not None else ALLOW_ALL_ROBOTS
        checker = RobotsChecker.from_text(text, user_agent=DEFAULT_USER_AGENT)
        kwargs.setdefault("rate_limit", 1000.0)
        # Use httpx+MockTransport by default: curl_cffi cannot be pointed at
        # a mock transport, so the real curl_cffi path is covered separately
        # against a local HTTP server (see test_fetchers.py).
        kwargs.setdefault("prefer_curl", False)
        return WebCrawler(
            transport=transport,
            robots_checker=checker,
            **kwargs,
        )

    return _make
