"""Academic Intelligence - Type Definitions

This module defines custom types, type aliases, and configuration classes
used across the Academic Intelligence system.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
)

from academic_intelligence.budget.models import BudgetSpec


def mask_proxy_userinfo(proxy_url: str) -> str:
    """Replace the ``user:pass@`` userinfo of a proxy URL with ``***@``.

    ``http://user:pass@host:8080`` → ``http://***@host:8080`` (FIX-AF F3 /
    AF-3): proxy credentials must never leave the process through
    ``Config.to_dict()`` / ``model_dump()`` or ``ProxyPool`` health logs.
    The host/port/path stay visible so the entry stays diagnosable.  A URL
    without userinfo, or anything that does not parse as a URL, is returned
    unchanged — a masking helper never raises.
    """
    try:
        parts = urlsplit(proxy_url)
    except ValueError:
        return proxy_url
    netloc = parts.netloc
    if "@" not in netloc:
        return proxy_url
    host_part = netloc.rsplit("@", 1)[1]
    return urlunsplit(
        (parts.scheme, f"***@{host_part}", parts.path, parts.query, parts.fragment)
    )


class SourceType(StrEnum):
    """Enumeration of supported academic data sources."""

    GOOGLE_SCHOLAR = "google_scholar"
    ARXIV = "arxiv"
    PUBMED = "pubmed"
    IEEE = "ieee"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    OPENALEX = "openalex"
    EUROPE_PMC = "europe_pmc"
    CORE = "core"
    UNPAYWALL = "unpaywall"
    OPEN_CITATIONS = "opencitations"
    CROSSREF = "crossref"


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

    proxy_pool: list[str] = Field(default_factory=list)
    proxy_rotation_interval: int = Field(default=10, ge=1)
    base_delay: float = Field(default=1.0, ge=0.0)
    adaptive_delay: bool = True
    jitter: bool = True
    fallback_sources: bool = True
    fallback_strategies: bool = True
    max_retries: int = Field(default=3, ge=0)
    retry_backoff: float = Field(default=2.0, ge=1.0)
    retry_on_status: list[int] = Field(
        default_factory=lambda: [429, 500, 503, 504]
    )

    @field_validator("proxy_rotation_interval")
    @classmethod
    def validate_rotation(cls, v: int) -> int:
        if v < 1:
            raise ValueError("proxy_rotation_interval must be >= 1")
        return v

    @field_serializer("proxy_pool")
    def _serialize_proxy_pool(self, value: list[str]) -> list[str]:
        """Mask proxy credentials on serialization (FIX-AF F3 / AF-3)."""
        return [mask_proxy_userinfo(p) for p in value]


class WebCrawlerConfig(BaseModel):
    """Configuration for the polite web crawler (upgrade technical-design §8).

    Mirrors the tunable knobs of
    :class:`~academic_intelligence.webcrawler.crawler.WebCrawler` plus the
    robots pre-check switch and the ``fetch_mode`` transport policy.
    ``to_crawler_kwargs()`` maps the model onto ``WebCrawler.__init__``
    keyword arguments.

    Attributes:
        fetch_mode: Transport policy — ``"auto"`` (prefer curl_cffi when
            installed, upgrade to the browser fetcher for JS-required pages
            subject to ``enable_browser``), ``"http"`` (httpx only, no
            browser upgrade) or ``"browser"`` (browser fetcher enabled).
        user_agent: User-Agent header (defaults to the polite-mode banner).
        timeout: Request timeout in seconds.
        rate_limit: Global request ceiling (requests/second).
        max_concurrency: Cap on concurrent in-flight fetches.
        enable_cache: Master switch for the crawl document cache.
        cache_ttl: TTL (seconds) for the in-memory crawl cache.
        enable_browser: Allow the optional Scrapling browser fetcher
            (heavy optional dependency, import-detected).
        prefer_curl: Prefer ``curl_cffi`` over httpx for static pages when
            the optional dependency is installed (only meaningful in
            ``fetch_mode="auto"``).
        enable_robots: Whether to pre-check ``robots.txt`` before fetching
            (denial → ``blocked``).  Disabling it is NOT recommended — the
            polite-mode default is to respect robots.
        max_page_bytes: Body-size cap per page.
    """

    fetch_mode: Literal["auto", "http", "browser"] = "auto"
    user_agent: str = "paper-research-crawler/0.2 (learning use)"
    timeout: float = Field(default=30.0, gt=0)
    rate_limit: float = Field(default=1.0, gt=0)
    max_concurrency: int = Field(default=4, ge=1)
    enable_cache: bool = True
    cache_ttl: int = Field(default=3600, ge=0)
    enable_browser: bool = False
    prefer_curl: bool = True
    enable_robots: bool = True
    max_page_bytes: int | None = None

    def to_crawler_kwargs(self) -> dict[str, Any]:
        """Map this config to ``WebCrawler.__init__`` keyword arguments."""
        kwargs: dict[str, Any] = {
            "user_agent": self.user_agent,
            "timeout": self.timeout,
            "rate_limit": self.rate_limit,
            "max_concurrency": self.max_concurrency,
            "enable_cache": self.enable_cache,
            "cache_ttl": self.cache_ttl,
            "enable_robots": self.enable_robots,
            "max_page_bytes": self.max_page_bytes,
        }
        if self.fetch_mode == "http":
            kwargs["prefer_curl"] = False
            kwargs["enable_browser"] = False
        elif self.fetch_mode == "browser":
            kwargs["prefer_curl"] = True
            kwargs["enable_browser"] = True
        else:  # "auto" — keep the independent booleans.
            kwargs["prefer_curl"] = self.prefer_curl
            kwargs["enable_browser"] = self.enable_browser
        return kwargs


class Config(BaseModel):
    """Global configuration for the Academic Intelligence library.

    API keys and the polite-pool email are stored as :class:`SecretStr` so
    they never leak through ``to_dict()`` / ``str()`` / ``repr()`` (I-7);
    adapters read them via ``get_secret_value()``. ``validate_assignment``
    keeps the value typed when environment-variable fallbacks assign into
    the model after construction.

    Attributes:
        sources: Ordered list of source identifiers to use.
        rate_limit: Requests per second (global default).
        proxy: Optional single proxy URL (merged into anti-crawl pool).
        proxies: Optional list of proxy URLs.
        storage_type: Backend type — ``"sqlite"`` or ``"json"``.
        storage_path: Path to SQLite DB file or JSON data directory.  Write
            paths are validated (C-2 / FIX-AA): a relative path that escapes
            the working directory (leading ``..`` after normalization) is
            rejected; relative-to-cwd paths and deliberate absolute paths
            are preserved unchanged.
        sqlite_busy_timeout: Seconds a SQLite connection waits on a busy
            database before raising ``database is locked`` (WAL
            single-writer contention, P50 round-32).  Passed to
            ``SQLiteStorage(busy_timeout=...)``; the write paths retry
            transient lock contention on top of it (FIX-AE F1).  Lower for
            fail-fast low-latency pipelines, raise for heavy multi-writer
            contention.  Default 10s.
        min_confidence: Minimum confidence score to accept records.
        deduplication_threshold: Similarity threshold for paper merge (0-1).
        cache_ttl: HTTP response cache TTL in seconds.
        cache_enabled: Whether to enable HTTP response caching.
        cache_persistent: Whether the HTTP cache persists to disk across
            sessions (Y-2). Disabled by default, keeping the current
            in-memory behaviour.
        cache_path: JSON cache file path used when ``cache_persistent`` is
            enabled; defaults to ``./.ai_cache.json``.  Same write-path
            validation as ``storage_path`` (C-2 / FIX-AA).
        timeout: Default HTTP timeout in seconds.
        serpapi_key: Optional SerpAPI key for Google Scholar.
        semantic_scholar_api_key: Optional Semantic Scholar API key.
        openalex_email: Optional polite-pool email for OpenAlex.
        ieee_api_key: Optional IEEE Xplore Metadata API key (env: ``IEEE_API_KEY``).
        anti_crawl: Nested anti-crawl strategy settings.
        max_concurrent_sources: Max parallel source queries.
        enable_google_scholar: Whether Google Scholar collection is enabled.
        download_delay: Default delay between downloads in seconds.
        max_concurrent_requests: Max parallel HTTP requests.
        max_expand_depth: Max depth for graph expansion traversal.
        max_expand_nodes: Max nodes to expand in the knowledge graph.
        graph_cache_size: Knowledge graph cache capacity (nodes).
        auto_merge_threshold: Similarity above which authors auto-merge.
        ambiguous_threshold: Similarity below which authors stay ambiguous.
        paper_refresh_days: Days before a paper is refreshed incrementally.
        author_refresh_days: Days before an author profile is refreshed.
        budget: Per-source quota overrides keyed by source name (empty uses
            the budget layer's defaults; limit 0 disables the source
            fail-soft).
        crawler: Polite web crawler configuration (``WebCrawlerConfig``).
    """

    model_config = ConfigDict(validate_assignment=True)

    sources: list[str] = Field(
        default_factory=lambda: ["semantic_scholar", "openalex", "google_scholar"]
    )
    rate_limit: float = Field(default=1.0, gt=0)
    proxy: str | None = None
    proxies: list[str] = Field(default_factory=list)
    storage_type: str = Field(default="sqlite")
    storage_path: str = Field(default="./academic_intelligence.db")
    sqlite_busy_timeout: float = Field(default=10.0, gt=0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    deduplication_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    cache_ttl: int = Field(default=3600, ge=0)
    cache_enabled: bool = True
    cache_persistent: bool = False
    cache_path: str | None = None
    timeout: float = Field(default=30.0, gt=0)
    serpapi_key: SecretStr | None = None
    semantic_scholar_api_key: SecretStr | None = None
    openalex_email: SecretStr | None = None
    ieee_api_key: SecretStr | None = None
    # --- crawler upgrade 2026-08: new free sources (M9) ---
    crossref_mailto: SecretStr | None = None  # Crossref polite pool (env: CROSSREF_MAILTO)
    unpaywall_email: SecretStr | None = None  # Unpaywall API (env: UNPAYWALL_EMAIL)
    core_api_key: SecretStr | None = None     # CORE API v3 (env: CORE_API_KEY)
    # --- crawler upgrade 2026-08: budget + crawler sections (technical-design §8) ---
    # Per-source quotas keyed by source name.  Empty (default) means "use the
    # budget layer's DEFAULT_BUDGETS"; a key present here overrides the
    # default for that source.  A source pinned to limit 0 is skipped
    # fail-soft (config-level kill switch).  Keys are normalized to each
    # spec's ``source`` field on validation.
    budget: dict[str, BudgetSpec] = Field(default_factory=dict)
    # Polite web crawler configuration (fetch_mode / ua / robots switch ...).
    crawler: WebCrawlerConfig = Field(default_factory=WebCrawlerConfig)
    anti_crawl: AntiCrawlStrategy = Field(default_factory=AntiCrawlStrategy)
    max_concurrent_sources: int = Field(default=3, ge=1)
    # --- v2 extension fields ---
    enable_google_scholar: bool = False
    download_delay: float = Field(default=1.0, ge=0.0)
    max_concurrent_requests: int = Field(default=4, ge=1)
    max_expand_depth: int = Field(default=3, ge=1)
    max_expand_nodes: int = Field(default=50, ge=1)
    graph_cache_size: int = Field(default=5000, ge=1)
    auto_merge_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    ambiguous_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    paper_refresh_days: int = Field(default=7, ge=1)
    author_refresh_days: int = Field(default=30, ge=1)

    @field_validator("storage_type")
    @classmethod
    def validate_storage_type(cls, v: str) -> str:
        allowed = {"sqlite", "json"}
        if v not in allowed:
            raise ValueError(f"storage_type must be one of {allowed}, got {v!r}")
        return v

    @field_serializer("proxy", "proxies")
    def _serialize_proxies(self, value: Any) -> Any:
        """Mask proxy credentials on serialization (FIX-AF F3 / AF-3).

        ``proxy`` / ``proxies`` (and ``anti_crawl.proxy_pool`` via
        :meth:`AntiCrawlStrategy._serialize_proxy_pool`) carry optional
        ``user:pass@`` credentials; ``to_dict()`` / ``model_dump()`` / JSON
        export must never expose them.  The live attribute values are
        untouched, so adapters and ``HTTPClient`` still use the real URL.
        """
        if isinstance(value, list):
            return [mask_proxy_userinfo(p) for p in value]
        if value is None:
            return None
        return mask_proxy_userinfo(value)

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("sources must not be empty")
        return v

    @field_validator("budget")
    @classmethod
    def normalize_budget_keys(cls, v: dict[str, BudgetSpec]) -> dict[str, BudgetSpec]:
        """Normalize budget-dict keys to each spec's ``source`` field.

        ``{"openalex": BudgetSpec(source="openalex", ...)}`` is the canonical
        shape; a caller that keys the dict differently (or omits the key
        match) is re-keyed by ``spec.source`` so the manager wiring can rely
        on ``dict.values()``.
        """
        if not v:
            return v
        normalized: dict[str, BudgetSpec] = {}
        for key, spec in v.items():
            if spec.source != key and spec.source in normalized:
                raise ValueError(
                    f"budget config key {key!r} duplicates spec.source "
                    f"{spec.source!r}"
                )
            normalized[spec.source] = spec
        return normalized

    @field_validator("storage_path", "cache_path")
    @classmethod
    def validate_write_path(cls, v: str | None) -> str | None:
        """Reject write paths that escape the working directory (C-2).

        ``storage_path`` (SQLite/JSON backend) and ``cache_path`` (persistent
        HTTP cache) are file-write locations derived from configuration; a
        relative path starting with ``..`` would resolve outside the process
        working directory at open time, letting a poisoned config write
        SQLite/JSON data (or cache files) to arbitrary writable locations.
        The check runs on the normalized form (so ``sub/../../evil.db`` is
        caught), while the original value is returned unchanged — legitimate
        relative-to-cwd paths and explicit absolute paths (a deliberate user
        choice, e.g. ``C:\\data\\store.db``) are preserved byte-for-byte.
        """
        if v is None:
            return None
        if not v.strip():
            raise ValueError("write path must not be empty")
        if os.path.normpath(v).startswith(".."):
            raise ValueError(
                "write path must not escape the working directory "
                f"(got {v!r}); use an absolute path for external locations"
            )
        return v

    def proxy_list(self) -> list[str]:
        """Return the combined proxy list from config and anti-crawl strategy."""
        result: list[str] = []
        if self.proxy:
            result.append(self.proxy)
        result.extend(self.proxies)
        result.extend(self.anti_crawl.proxy_pool)
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for p in result:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique

    def to_dict(self) -> dict[str, Any]:
        """Serialize configuration to a plain dictionary."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """Load configuration from a plain dictionary."""
        return cls.model_validate(data)
