"""Academic Intelligence - Constants and Defaults

This module defines system-wide constants, default values, and configuration
parameters used across the Academic Intelligence system.
"""

from __future__ import annotations

from academic_intelligence.core.types import SourceType


# ---------------------------------------------------------------------------
# Rate Limiting Defaults
# ---------------------------------------------------------------------------
DEFAULT_RATE_LIMIT: float = 1.0
"""Default rate limit in requests per second."""

DEFAULT_MAX_RETRIES: int = 3
"""Default maximum number of retry attempts for failed requests."""

DEFAULT_RETRY_BACKOFF: float = 2.0
"""Default exponential backoff multiplier for retries."""


# ---------------------------------------------------------------------------
# Confidence and Quality Thresholds
# ---------------------------------------------------------------------------
DEFAULT_MIN_CONFIDENCE: float = 0.5
"""Default minimum confidence score for accepting collected data."""

DEFAULT_DEDUPLICATION_THRESHOLD: float = 0.95
"""Default similarity threshold for deduplication (0.0-1.0)."""


# ---------------------------------------------------------------------------
# Supported Sources
# ---------------------------------------------------------------------------
SUPPORTED_SOURCES: list[str] = [
    SourceType.GOOGLE_SCHOLAR.value,
    SourceType.ARXIV.value,
    SourceType.PUBMED.value,
    SourceType.IEEE.value,
    SourceType.SEMANTIC_SCHOLAR.value,
    SourceType.OPENALEX.value,
]
"""List of all supported academic data sources."""

DEFAULT_SOURCES: list[str] = [
    SourceType.GOOGLE_SCHOLAR.value,
    SourceType.SEMANTIC_SCHOLAR.value,
    SourceType.OPENALEX.value,
]
"""Default subset of sources to use for collection."""


# ---------------------------------------------------------------------------
# Storage Defaults
# ---------------------------------------------------------------------------
DEFAULT_STORAGE_TYPE: str = "sqlite"
"""Default storage backend type."""

DEFAULT_STORAGE_PATH: str = "./academic_intelligence.db"
"""Default path for SQLite storage."""

DEFAULT_JSON_STORAGE_PATH: str = "./academic_intelligence_data.json"
"""Default path for JSON file storage."""
