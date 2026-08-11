"""Retry strategies for resilient academic data collection.

Provides configurable retry policies with exponential backoff, jitter,
and decorator / imperative execution modes.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast, overload

import httpx

T = TypeVar("T")
P = ParamSpec("P")
logger = logging.getLogger(__name__)


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(
        self,
        max_retries: int = 3,
        backoff: float = 2.0,
        jitter: bool = True,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        retry_on: Sequence[type[BaseException]] | None = None,
        retry_on_status: Sequence[int] | None = None,
    ) -> None:
        """Initialize retry configuration.

        Args:
            max_retries: Maximum number of retry attempts after the first try.
            backoff: Exponential backoff multiplier.
            jitter: Whether to add random jitter to delays.
            base_delay: Initial delay in seconds.
            max_delay: Maximum delay cap in seconds.
            retry_on: Exception types to retry on. Defaults to
                ``(httpx.HTTPStatusError, httpx.TransportError)`` — HTTP
                status errors and transport errors (timeouts, connection
                failures).  A generic exception is not retried by default
                (FIX-AA-3).
            retry_on_status: Optional HTTP status codes that should trigger retry
                when raised via an exception carrying ``status_code``.
        """
        self.max_retries = max_retries
        self.backoff = backoff
        self.jitter = jitter
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_on: Sequence[type[BaseException]] = retry_on or (
            httpx.HTTPStatusError,
            httpx.TransportError,
        )
        self.retry_on_status: Sequence[int] = retry_on_status or (429, 500, 503, 504)

    def should_retry(self, exc: BaseException) -> bool:
        """Return True if *exc* is a retriable exception.

        An exception type outside ``retry_on`` is never retried.  When the
        exception carries an HTTP ``status_code``, the decision is purely
        status-based: a status outside ``retry_on_status`` is never retried,
        even if the type itself is retriable (FIX-AA-3 — a 400 response must
        not be retried).  Exceptions without a status (timeouts, connection
        errors) retry by type.
        """
        if not isinstance(exc, tuple(self.retry_on)):
            return False
        status = getattr(exc, "status_code", None)
        if status is None and isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
        if status is not None and self.retry_on_status:
            return int(status) in self.retry_on_status
        return True


def retry_with_backoff(
    config: RetryConfig | None = None,
    *,
    max_retries: int | None = None,
    backoff: float | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that adds retry with exponential backoff.

    Can be used as::

        @retry_with_backoff()
        async def fetch(...): ...

        @retry_with_backoff(RetryConfig(max_retries=5))
        async def fetch(...): ...
    """
    cfg = config or RetryConfig()
    if max_retries is not None:
        cfg.max_retries = max_retries
    if backoff is not None:
        cfg.backoff = backoff

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            handler = RetryHandler(cfg)
            return await handler.execute(func, *args, **kwargs)

        return cast(Callable[..., T], wrapper)

    return decorator


class RetryHandler:
    """Manual retry handler for fine-grained control."""

    def __init__(self, config: RetryConfig | None = None) -> None:
        """Initialize retry handler.

        Args:
            config: Retry configuration.
        """
        self.config = config or RetryConfig()

    @overload
    async def execute(
        self,
        func: Callable[P, Awaitable[T]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T: ...

    @overload
    async def execute(
        self,
        func: Callable[P, T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T: ...

    async def execute(
        self,
        func: Callable[..., T | Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute a function with retry logic.

        Args:
            func: Async (or sync) function to execute.
            *args: Positional arguments for func.
            **kwargs: Keyword arguments for func.

        Returns:
            Result of func.

        Raises:
            Exception: If all retries are exhausted (last exception re-raised).
        """
        last_exc: BaseException | None = None
        attempts = self.config.max_retries + 1

        for attempt in range(attempts):
            try:
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    return await cast(Awaitable[T], result)
                return cast(T, result)
            except BaseException as exc:
                last_exc = exc
                if attempt >= self.config.max_retries or not self.config.should_retry(exc):
                    # Preserve the number of retries already consumed on the
                    # terminal exception.  Source adapters may wrap this
                    # exception, so collectors recover the value by walking
                    # the exception chain (FIX-AG).
                    with suppress(AttributeError, TypeError):
                        exc.retry_count = attempt  # type: ignore[attr-defined]
                    raise
                delay = self._calculate_delay(attempt)
                logger.warning(
                    "Retry %s/%s for %s after %.2fs due to: %s",
                    attempt + 1,
                    self.config.max_retries,
                    getattr(func, "__name__", repr(func)),
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        assert last_exc is not None
        raise last_exc

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay for a given retry attempt (0-indexed)."""
        delay = self.config.base_delay * (self.config.backoff ** attempt)
        if self.config.jitter:
            delay *= random.uniform(0.5, 1.5)
        return min(delay, self.config.max_delay)
