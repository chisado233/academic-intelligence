"""Rate limiting and adaptive throttling utilities for the academic intelligence crawler.
"""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RateLimitConfig(BaseModel):
    """Serializable configuration for a rate limiter."""

    requests_per_second: float = Field(default=2.0, gt=0)
    adaptive: bool = True
    jitter: bool = True
    max_backoff_seconds: float = Field(default=60.0, gt=0)
    backoff_factor: float = Field(default=2.0, gt=0)

    @field_validator("requests_per_second")
    @classmethod
    def _validate_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("requests_per_second must be positive")
        return v


class AdaptiveDelayConfig(BaseModel):
    """Fine-grained knobs for the adaptive delay algorithm."""

    target_latency_ms: float = Field(default=500.0, gt=0)
    increase_factor: float = Field(default=1.5, gt=1.0)
    decrease_factor: float = Field(default=0.9, gt=0, lt=1.0)
    min_delay_seconds: float = Field(default=0.1, ge=0)
    max_delay_seconds: float = Field(default=30.0, gt=0)


class RateLimiter(ABC):
    """Abstract base class for all rate limiters."""

    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._last_request_time: float = 0.0
        self._current_delay: float = 1.0 / config.requests_per_second

    @property
    def config(self) -> RateLimitConfig:
        """Return the configuration for this limiter."""
        return self._config

    @property
    def current_delay(self) -> float:
        """Current inter-request delay in seconds."""
        return self._current_delay

    @abstractmethod
    async def acquire(self) -> None:
        """Block until the caller is allowed to proceed."""
        ...

    async def release(self) -> None:
        """Release any resources held after a request completes."""
        return None

    async def report_status(self, status_code: int, latency_ms: float) -> None:
        """Inform the limiter about the result of the last request."""
        return None

    @asynccontextmanager
    async def __aenter__(self) -> AsyncIterator[RateLimiter]:
        await self.acquire()
        try:
            yield self
        finally:
            await self.release()

    def _apply_jitter(self, delay: float) -> float:
        """Add random jitter to *delay* if configured to do so."""
        if not self._config.jitter:
            return delay
        return delay * (1.0 + random.random() * 0.25)


class FixedIntervalRateLimiter(RateLimiter):
    """Simple fixed-interval rate limiter."""

    async def acquire(self) -> None:
        """Wait until the fixed interval since the last request has elapsed."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            wait = self._current_delay - elapsed
            if wait > 0:
                await asyncio.sleep(self._apply_jitter(wait))
            self._last_request_time = time.monotonic()


class TokenBucketRateLimiter(RateLimiter):
    """Token-bucket based rate limiter."""

    def __init__(self, config: RateLimitConfig, *, bucket_size: int = 10) -> None:
        super().__init__(config)
        self._bucket_size = max(1, bucket_size)
        self._tokens: float = float(self._bucket_size)
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            float(self._bucket_size),
            self._tokens + elapsed * self._config.requests_per_second,
        )
        self._last_refill = now

    async def acquire(self) -> None:
        """Consume one token, waiting if necessary."""
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._last_request_time = time.monotonic()
                    return
                # Wait for next token
                needed = 1.0 - self._tokens
                wait = needed / self._config.requests_per_second
                await asyncio.sleep(self._apply_jitter(max(wait, 0.001)))


class AdaptiveRateLimiter(RateLimiter):
    """Rate limiter with adaptive delay based on observed latency and errors."""

    def __init__(
        self,
        config: RateLimitConfig,
        *,
        adaptive_config: AdaptiveDelayConfig | None = None,
    ) -> None:
        super().__init__(config)
        self._adaptive_config = adaptive_config or AdaptiveDelayConfig()
        self._consecutive_errors: int = 0

    async def acquire(self) -> None:
        """Wait according to the current adaptive delay."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            wait = self._current_delay - elapsed
            if wait > 0:
                await asyncio.sleep(self._apply_jitter(wait))
            self._last_request_time = time.monotonic()

    async def report_status(self, status_code: int, latency_ms: float) -> None:
        """Adjust the internal delay based on status and latency."""
        async with self._lock:
            self._update_delay(status_code, latency_ms)

    def _update_delay(self, status_code: int, latency_ms: float) -> None:
        """Internal helper to mutate ``self._current_delay``."""
        cfg = self._adaptive_config
        if status_code in (429, 503, 504):
            self._consecutive_errors += 1
            self._current_delay = min(
                self._config.max_backoff_seconds,
                self._current_delay * self._config.backoff_factor,
            )
            return

        if 200 <= status_code < 300:
            self._consecutive_errors = 0
            # (Q2) A slow-but-successful response is NOT an overload signal:
            # a source that answers slowly (e.g. arxiv) must not keep pushing
            # the shared adaptive delay up and penalize every other source on
            # the same HTTPClient.  Only throttling (429/503/504) or errors
            # raise the delay; a slow 200 leaves it unchanged, a fast 200
            # pulls it back toward the 1/rps baseline.
            if latency_ms <= cfg.target_latency_ms:
                self._current_delay = max(
                    cfg.min_delay_seconds,
                    self._current_delay * cfg.decrease_factor,
                )
                # Keep roughly near 1/rps baseline
                baseline = 1.0 / self._config.requests_per_second
                self._current_delay = max(cfg.min_delay_seconds, min(cfg.max_delay_seconds, self._current_delay))
                # Soft pull toward baseline when healthy
                self._current_delay = 0.7 * self._current_delay + 0.3 * baseline
            return

        # Other errors: mild increase
        self._consecutive_errors += 1
        self._current_delay = min(
            self._config.max_backoff_seconds,
            self._current_delay * 1.2,
        )


class RateLimiterRegistry:
    """Global registry that maps source identifiers to their rate limiters."""

    def __init__(self) -> None:
        self._limiters: dict[str, RateLimiter] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        source_id: str,
        factory: Callable[[], RateLimiter],
    ) -> RateLimiter:
        """Return the existing limiter for *source_id*, or create one via *factory*."""
        async with self._lock:
            if source_id not in self._limiters:
                self._limiters[source_id] = factory()
            return self._limiters[source_id]

    async def register(self, source_id: str, limiter: RateLimiter) -> None:
        """Explicitly register a limiter for *source_id*."""
        async with self._lock:
            self._limiters[source_id] = limiter

    async def unregister(self, source_id: str) -> RateLimiter | None:
        """Remove and return the limiter associated with *source_id*."""
        async with self._lock:
            return self._limiters.pop(source_id, None)


def create_rate_limiter(
    strategy: str = "adaptive",
    *,
    requests_per_second: float = 2.0,
    **kwargs: Any,
) -> RateLimiter:
    """Factory function that creates a rate limiter by *strategy* name.

    Args:
        strategy: One of ``"fixed"``, ``"token_bucket"``, or ``"adaptive"``.
        requests_per_second: Base rate limit.
        **kwargs: Extra arguments forwarded to the concrete constructor.

    Returns:
        An instance of the requested limiter.
    """
    config = RateLimitConfig(requests_per_second=requests_per_second)
    if strategy in ("fixed", "fixed_interval"):
        return FixedIntervalRateLimiter(config)
    if strategy in ("token_bucket", "token"):
        bucket_size = int(kwargs.get("bucket_size", 10))
        return TokenBucketRateLimiter(config, bucket_size=bucket_size)
    if strategy == "adaptive":
        return AdaptiveRateLimiter(config)
    raise ValueError(f"Unknown rate limiter strategy: {strategy!r}")
