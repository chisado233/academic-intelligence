"""Fetcher-layer tests (offline, httpx MockTransport / import patching)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from academic_intelligence.utils import curl_fetcher as utils_curl_fetcher
from academic_intelligence.utils.curl_fetcher import (
    CURL_CFFI_AVAILABLE,
)
from academic_intelligence.utils.curl_fetcher import (
    CurlFetcher as SyncCurlFetcher,
)
from academic_intelligence.webcrawler import CrawlStatus, WebCrawler
from academic_intelligence.webcrawler.fetchers import (
    DEFAULT_USER_AGENT,
    BrowserFetcher,
    CurlFetcher,
    FetchTooLargeError,
    FetchTransportError,
    HTTPFetcher,
)
from academic_intelligence.webcrawler.robots import RobotsChecker

from .fixtures import ALLOW_ALL_ROBOTS, ARTICLE_HTML, build_transport


def _serve_locally() -> tuple[ThreadingHTTPServer, int]:
    """Start a local HTTP server serving robots.txt + the article fixture."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib callback name)
            if self.path == "/robots.txt":
                body = ALLOW_ALL_ROBOTS.encode("utf-8")
            else:
                body = ARTICLE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


@pytest.mark.asyncio
async def test_http_fetcher_mock_transport() -> None:
    seen_ua: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_ua.append(request.headers.get("User-Agent", ""))
        return httpx.Response(200, text=ARTICLE_HTML)

    transport = httpx.MockTransport(handler)
    fetcher = HTTPFetcher(transport=transport)
    try:
        result = await fetcher.fetch("https://example.com/article")
    finally:
        await fetcher.close()

    assert result.status_code == 200
    assert result.fetcher == "httpx"
    assert "Transformer" in result.text
    assert result.url == "https://example.com/article"
    assert seen_ua == [DEFAULT_USER_AGENT]


@pytest.mark.asyncio
async def test_http_fetcher_respects_max_bytes() -> None:
    transport, _ = build_transport({"/big": (200, "x" * 10_000)})
    fetcher = HTTPFetcher(transport=transport, max_bytes=100)
    try:
        with pytest.raises(FetchTooLargeError):
            await fetcher.fetch("https://example.com/big")
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_http_fetcher_transport_error_wrapped() -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(failing_handler)
    fetcher = HTTPFetcher(transport=transport)
    try:
        with pytest.raises(FetchTransportError) as excinfo:
            await fetcher.fetch("https://example.com/x")
    finally:
        await fetcher.close()

    assert "failed" in str(excinfo.value)


def test_curl_fetcher_availability_flag() -> None:
    fetcher = CurlFetcher()
    assert fetcher.available is CURL_CFFI_AVAILABLE
    assert utils_curl_fetcher.curl_cffi_available() is CURL_CFFI_AVAILABLE


def test_sync_curl_fetcher_raises_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils_curl_fetcher, "CURL_CFFI_AVAILABLE", False)
    with pytest.raises(ImportError, match="curl_cffi is not installed"):
        SyncCurlFetcher()


@pytest.mark.asyncio
async def test_async_curl_fetcher_raises_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch the flag in the webcrawler fetchers namespace too, since it
    # imported the value at module load.
    import academic_intelligence.webcrawler.fetchers as web_fetchers

    monkeypatch.setattr(web_fetchers, "CURL_CFFI_AVAILABLE", False)
    fetcher = CurlFetcher()
    assert fetcher.available is False
    with pytest.raises(FetchTransportError, match="curl_cffi is not installed"):
        await fetcher.fetch("https://example.com/x")


def test_browser_fetcher_unavailable_in_this_env() -> None:
    fetcher = BrowserFetcher()
    # scrapling is not installed here; availability must be False and fetch
    # must fail with a diagnostic transport error, not a crash.
    assert fetcher.available is False


@pytest.mark.asyncio
async def test_browser_fetcher_fetch_diagnostic_when_absent() -> None:
    fetcher = BrowserFetcher()
    with pytest.raises(FetchTransportError, match="scrapling is not installed"):
        await fetcher.fetch("https://example.com/x")


@pytest.mark.skipif(
    not CURL_CFFI_AVAILABLE,
    reason="curl_cffi optional dependency is not installed",
)
@pytest.mark.asyncio
async def test_curl_fetcher_real_local_server() -> None:
    """Exercise the real curl_cffi transport against a local HTTP server.

    Stays offline (127.0.0.1) while validating the TLS-fingerprint wrapper
    end to end — the MockTransport cannot drive curl_cffi.
    """
    server, port = _serve_locally()
    try:
        fetcher = CurlFetcher()
        result = await fetcher.fetch(f"http://127.0.0.1:{port}/article")
        assert result.status_code == 200
        assert result.fetcher == "curl_cffi"
        assert "Transformer" in result.text
    finally:
        server.shutdown()


@pytest.mark.skipif(
    not CURL_CFFI_AVAILABLE,
    reason="curl_cffi optional dependency is not installed",
)
@pytest.mark.asyncio
async def test_webcrawler_prefers_curl_cffi_on_local_server() -> None:
    """Full WebCrawler pipeline over the real curl_cffi transport."""
    server, port = _serve_locally()
    crawler = WebCrawler(
        rate_limit=1000.0,
        prefer_curl=True,
        robots_checker=RobotsChecker.from_text(ALLOW_ALL_ROBOTS, user_agent=DEFAULT_USER_AGENT),
    )
    try:
        doc = await crawler.crawl(f"http://127.0.0.1:{port}/article")
    finally:
        await crawler.close()
        server.shutdown()

    assert doc.status == CrawlStatus.OK
    assert doc.metadata["fetcher"] == "curl_cffi"
    assert "Transformer" in doc.content
