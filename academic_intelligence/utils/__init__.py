"""Utilities module for academic data collection.

Provides HTTP client, proxy management, rate limiting, retry, and caching utilities.
"""

from academic_intelligence.utils.cache import Cache
from academic_intelligence.utils.http import HTTPClient
from academic_intelligence.utils.proxy import ProxyPool
from academic_intelligence.utils.rate_limiter import (
    AdaptiveRateLimiter,
    FixedIntervalRateLimiter,
    RateLimitConfig,
    RateLimiter,
    TokenBucketRateLimiter,
    create_rate_limiter,
)
from academic_intelligence.utils.retry import RetryConfig, RetryHandler, retry_with_backoff

__all__ = [
    "Cache",
    "HTTPClient",
    "ProxyPool",
    "RateLimiter",
    "FixedIntervalRateLimiter",
    "TokenBucketRateLimiter",
    "AdaptiveRateLimiter",
    "RateLimitConfig",
    "create_rate_limiter",
    "RetryConfig",
    "RetryHandler",
    "retry_with_backoff",
]
