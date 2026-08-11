"""curl_cffi wrapper with import detection (WP3 webcrawler layer).

``curl_cffi`` is an *optional* dependency (declared in the ``[crawler]``
extra): it provides a browser-grade TLS fingerprint via ``impersonate``,
which is useful for basic anti-bot pages that the site allows crawling.
This module wraps the sync ``curl_cffi.requests`` API behind a tiny,
type-stable facade and exposes ``CURL_CFFI_AVAILABLE`` / :func:`curl_cffi_available`
so the webcrawler layer can degrade to :class:`~academic_intelligence.utils.http.HTTPClient`
(httpx) when the package is not installed.

Anti-detection boundary (red line, see docs/upgrade/technical-design.md §1.2):
curl_cffi is only used on *public* pages that need basic anti-bot handling.
A challenge/captcha/403 anti-crawl interception is always reported as
``blocked`` by the caller — this module never configures automatic
challenge solving.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

try:
    import curl_cffi.requests as _curl_requests
except ImportError:  # pragma: no cover - exercised when the optional dep is absent
    _curl_requests = None  # type: ignore[assignment]

CURL_CFFI_AVAILABLE: bool = _curl_requests is not None
"""Whether the optional ``curl_cffi`` package is importable in this runtime."""


def curl_cffi_available() -> bool:
    """Return ``True`` when ``curl_cffi`` is installed and importable."""
    return CURL_CFFI_AVAILABLE


@dataclass(frozen=True)
class CurlFetchResult:
    """Normalized result of one ``curl_cffi`` GET request.

    Attributes:
        url: Final URL after redirects.
        status_code: HTTP status code.
        headers: Response headers (lowercased names).
        text: Decoded response body.
        reason: HTTP reason phrase.
    """

    url: str
    status_code: int
    headers: dict[str, str]
    text: str
    reason: str = ""


class CurlFetcher:
    """Sync thin wrapper around ``curl_cffi.requests``.

    Not installed → construction raises :class:`ImportError`; callers are
    expected to gate on :attr:`available` / :func:`curl_cffi_available`
    first.  The webcrawler layer invokes :meth:`fetch` via ``asyncio.to_thread``.
    """

    def __init__(
        self,
        *,
        impersonate: str = "chrome",
        timeout: float = 30.0,
        user_agent: str | None = None,
        verify: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize the wrapper.

        Args:
            impersonate: curl_cffi ``impersonate`` target (e.g. ``"chrome"``).
            timeout: Request timeout in seconds.
            user_agent: User-Agent header; overrides the impersonated one.
            verify: Whether to verify TLS certificates.
            headers: Extra headers merged over the defaults.
        """
        if not CURL_CFFI_AVAILABLE:
            raise ImportError(
                "curl_cffi is not installed; install the [crawler] extra "
                "(pip install 'academic-intelligence[crawler]') to enable the "
                "TLS-fingerprint transport, or use HTTPFetcher as fallback"
            )
        self._impersonate = impersonate
        self._timeout = timeout
        self._verify = verify
        self._extra_headers: dict[str, str] = {}
        if user_agent:
            self._extra_headers["User-Agent"] = user_agent
        if headers:
            self._extra_headers.update(headers)

    @property
    def available(self) -> bool:
        """Whether the underlying optional dependency is importable."""
        return CURL_CFFI_AVAILABLE

    def fetch(self, url: str, *, timeout: float | None = None) -> CurlFetchResult:
        """Perform a synchronous GET request.

        Args:
            url: Target URL.
            timeout: Optional per-request timeout override.

        Returns:
            Normalized :class:`CurlFetchResult`.

        Raises:
            ImportError: When ``curl_cffi`` is not installed.
            RuntimeError: When the underlying transport fails.
        """
        if _curl_requests is None:  # pragma: no cover - guarded by __init__
            raise ImportError("curl_cffi is not installed")
        try:
            response: Any = _curl_requests.get(
                url,
                impersonate=self._impersonate,  # type: ignore[arg-type]
                timeout=timeout if timeout is not None else self._timeout,
                verify=self._verify,
                headers=dict(self._extra_headers),
            )
        except Exception as exc:
            raise RuntimeError(f"curl_cffi GET {url} failed: {exc}") from exc
        return CurlFetchResult(
            url=str(getattr(response, "url", url)),
            status_code=int(getattr(response, "status_code", 0)),
            headers=dict(getattr(response, "headers", {}) or {}),
            text=str(getattr(response, "text", "")),
            reason=str(getattr(response, "reason", "") or ""),
        )
