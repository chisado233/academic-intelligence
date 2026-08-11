"""Proxy pool management for distributed academic data collection.

Manages a pool of proxy servers with health checking, rotation, and fallback.
Supports HTTP, HTTPS, and SOCKS proxies.
"""

from __future__ import annotations

import logging
import random
from urllib.parse import urlsplit

from academic_intelligence.core.types import mask_proxy_userinfo

logger = logging.getLogger(__name__)


def _proxy_log_label(proxy_url: str) -> str:
    """``host:port`` (host only without a port) label for proxy logs.

    Never includes the ``user:pass@`` userinfo (FIX-AF F3 / AF-3): a proxy
    carrying credentials must not leak them through health/rotation log
    lines.  Falls back to the masked URL when the input cannot be parsed.
    """
    try:
        parts = urlsplit(proxy_url)
    except ValueError:
        return mask_proxy_userinfo(proxy_url)
    host = parts.hostname
    if not host:
        return mask_proxy_userinfo(proxy_url)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port is not None else host


class ProxyPool:
    """Manages a pool of proxy servers for rotation and health checking.

    Features:
    - Round-robin or random rotation
    - Health checking with automatic removal
    - Proxy validation before use
    """

    def __init__(self, proxies: list[str] | None = None) -> None:
        """Initialize proxy pool.

        Args:
            proxies: List of proxy URLs (e.g., "http://host:port").
        """
        proxies = proxies or []
        self._proxies = list(proxies)
        self._healthy: list[str] = list(proxies)
        self._unhealthy: list[str] = []
        self._index = 0

    def get_next(self, strategy: str = "round_robin") -> str | None:
        """Get next available proxy.

        Args:
            strategy: Rotation strategy ("round_robin" or "random").

        Returns:
            Proxy URL or None if no healthy proxies available.
        """
        if not self._healthy:
            return None

        if strategy == "random":
            return random.choice(self._healthy)

        # Default: round-robin
        proxy = self._healthy[self._index % len(self._healthy)]
        self._index = (self._index + 1) % len(self._healthy)
        return proxy

    def mark_unhealthy(self, proxy: str) -> None:
        """Mark a proxy as unhealthy and remove from rotation.

        Args:
            proxy: Proxy URL to mark.
        """
        if proxy in self._healthy:
            self._healthy.remove(proxy)
            if proxy not in self._unhealthy:
                self._unhealthy.append(proxy)
            logger.warning("Proxy marked unhealthy: %s", _proxy_log_label(proxy))
            # Keep index in range
            if self._healthy:
                self._index %= len(self._healthy)
            else:
                self._index = 0

    def mark_healthy(self, proxy: str) -> None:
        """Mark a proxy as healthy and add back to rotation.

        Args:
            proxy: Proxy URL to mark.
        """
        if proxy in self._unhealthy:
            self._unhealthy.remove(proxy)
        if proxy not in self._healthy:
            self._healthy.append(proxy)
        if proxy not in self._proxies:
            self._proxies.append(proxy)
        logger.info("Proxy marked healthy: %s", _proxy_log_label(proxy))

    def add(self, proxy: str) -> None:
        """Add a new proxy to the pool as healthy."""
        if proxy not in self._proxies:
            self._proxies.append(proxy)
        if proxy not in self._healthy and proxy not in self._unhealthy:
            self._healthy.append(proxy)

    def remove(self, proxy: str) -> None:
        """Permanently remove a proxy from the pool."""
        if proxy in self._proxies:
            self._proxies.remove(proxy)
        if proxy in self._healthy:
            self._healthy.remove(proxy)
        if proxy in self._unhealthy:
            self._unhealthy.remove(proxy)

    async def health_check(self, proxy: str, timeout: float = 5.0) -> bool:
        """Check if a proxy is responsive.

        Args:
            proxy: Proxy URL to check.
            timeout: Request timeout in seconds.

        Returns:
            True if proxy is healthy.
        """
        try:
            import httpx

            async with httpx.AsyncClient(
                proxy=proxy,
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                # Lightweight connectivity check
                response = await client.get("https://httpbin.org/ip")
                healthy = response.status_code == 200
                if healthy:
                    self.mark_healthy(proxy)
                else:
                    self.mark_unhealthy(proxy)
                return healthy
        except Exception as exc:
            logger.debug(
                "Proxy health check failed for %s: %s",
                _proxy_log_label(proxy),
                exc,
            )
            self.mark_unhealthy(proxy)
            return False

    async def health_check_all(self, timeout: float = 5.0) -> dict[str, bool]:
        """Run health checks on all known proxies.

        Returns:
            Mapping of proxy URL to health status.
        """
        results: dict[str, bool] = {}
        for proxy in list(self._proxies):
            results[proxy] = await self.health_check(proxy, timeout=timeout)
        return results

    @property
    def healthy_count(self) -> int:
        """Number of healthy proxies in pool."""
        return len(self._healthy)

    @property
    def total_count(self) -> int:
        """Total number of proxies (healthy + unhealthy)."""
        return len(self._proxies)

    @property
    def healthy_proxies(self) -> list[str]:
        """Copy of healthy proxy list."""
        return list(self._healthy)
