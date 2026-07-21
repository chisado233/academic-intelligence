"""Academic Intelligence - Type Definitions

This module defines custom types, type aliases, and configuration classes
used across the Academic Intelligence system.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class SourceType(str, Enum):
    """Enumeration of supported academic data sources."""

    GOOGLE_SCHOLAR = "google_scholar"
    ARXIV = "arxiv"
    PUBMED = "pubmed"
    IEEE = "ieee"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    OPENALEX = "openalex"


class AntiCrawlStrategy(BaseModel):
    """Anti-crawl strategy configuration.

    Defines parameters for proxy rotation, rate limiting, and retry
    behavior to avoid detection and blocking by data sources.

    Attributes:
        proxy_pool: List of proxy URLs for rotation.
        proxy_rotation_interval: Number of requests before rotating proxy.
        base_delay: Base delay between requests in seconds.
        adaptive_delay: Whether to adaptively adjust delay based on response.
        jitter: Whether to add random jitter to delays.
        fallback_sources: Whether to fallback to alternative sources on failure.
        fallback_strategies: Whether to fallback to alternative strategies.
        max_retries: Maximum number of retry attempts.
        retry_backoff: Exponential backoff multiplier for retries.
        retry_on_status: HTTP status codes that trigger retries.
    """

    proxy_pool: List[str] = Field(default_factory=list)
    proxy_rotation_interval: int = Field(default=10, ge=1)
    base_delay: float = Field(default=1.0, ge=0.0)
    adaptive_delay: bool = True
    jitter: bool = True
    fallback_sources: bool = True
    fallback_strategies: bool = True
    max_retries: int = Field(default=3, ge=0)
    retry_backoff: float = Field(default=2.0, ge=1.0)
    retry_on_status: List[int] = Field(default_factory=lambda: [429, 503, 504])

    @field_validator("proxy_rotation_interval")
    @classmethod
    def validate_rotation(cls, v: int) -> int:
        if v < 1:
            raise ValueError("proxy_rotation_interval must be >= 1")
        return v


class Config(BaseModel):
    """Global configuration for the Academic Intelligence library.

    Attributes:
        sources: Ordered list of source identifiers to use.
        rate_limit: Requests per second (global default).
        proxy: Optional single proxy URL (merged into anti-crawl pool).
        proxies: Optional list of proxy URLs.
        storage_type: Backend type — ``"sqlite"`` or ``"json"``.
        storage_path: Path to SQLite DB file or JSON data directory.
        min_confidence: Minimum confidence score to accept records.
        deduplication_threshold: Similarity threshold for paper merge (0-1).
        cache_ttl: HTTP response cache TTL in seconds.
        cache_enabled: Whether to enable HTTP response caching.
        timeout: Default HTTP timeout in seconds.
        serpapi_key: Optional SerpAPI key for Google Scholar.
        semantic_scholar_api_key: Optional Semantic Scholar API key.
        openalex_email: Optional polite-pool email for OpenAlex.
        anti_crawl: Nested anti-crawl strategy settings.
        max_concurrent_sources: Max parallel source queries.
    """

    sources: List[str] = Field(
        default_factory=lambda: ["semantic_scholar", "openalex", "google_scholar"]
    )
    rate_limit: float = Field(default=1.0, gt=0)
    proxy: Optional[str] = None
    proxies: List[str] = Field(default_factory=list)
    storage_type: str = Field(default="sqlite")
    storage_path: str = Field(default="./academic_intelligence.db")
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    deduplication_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    cache_ttl: int = Field(default=3600, ge=0)
    cache_enabled: bool = True
    timeout: float = Field(default=30.0, gt=0)
    serpapi_key: Optional[str] = None
    semantic_scholar_api_key: Optional[str] = None
    openalex_email: Optional[str] = None
    anti_crawl: AntiCrawlStrategy = Field(default_factory=AntiCrawlStrategy)
    max_concurrent_sources: int = Field(default=3, ge=1)

    @field_validator("storage_type")
    @classmethod
    def validate_storage_type(cls, v: str) -> str:
        allowed = {"sqlite", "json"}
        if v not in allowed:
            raise ValueError(f"storage_type must be one of {allowed}, got {v!r}")
        return v

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("sources must not be empty")
        return v

    def proxy_list(self) -> List[str]:
        """Return the combined proxy list from config and anti-crawl strategy."""
        result: List[str] = []
        if self.proxy:
            result.append(self.proxy)
        result.extend(self.proxies)
        result.extend(self.anti_crawl.proxy_pool)
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: List[str] = []
        for p in result:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique

    def to_dict(self) -> dict:
        """Serialize configuration to a plain dictionary."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        """Load configuration from a plain dictionary."""
        return cls.model_validate(data)
