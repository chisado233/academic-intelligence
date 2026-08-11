"""Academic Intelligence - Core Module

This module provides the foundational components for the Academic Intelligence
system, including data models, type definitions, constants, and custom exceptions.
"""

from __future__ import annotations

from academic_intelligence.core.constants import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_RATE_LIMIT,
    SUPPORTED_SOURCES,
)
from academic_intelligence.core.exceptions import (
    AcademicIntelligenceError,
    DataValidationError,
    DeduplicationError,
    RateLimitError,
    SourceUnavailableError,
    StorageError,
)
from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    ChangeDetection,
    ChangeType,
    Citation,
    CollectionResult,
    Evidence,
    IncrementalUpdateResult,
    Paper,
)
from academic_intelligence.core.types import (
    AntiCrawlStrategy,
    Config,
    SourceType,
)

__all__ = [
    # Data Models
    "Author",
    "AuthorRef",
    "Paper",
    "Citation",
    "Evidence",
    "CollectionResult",
    "ChangeType",
    "ChangeDetection",
    "IncrementalUpdateResult",
    # Types
    "SourceType",
    "AntiCrawlStrategy",
    "Config",
    # Constants
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MIN_CONFIDENCE",
    "SUPPORTED_SOURCES",
    # Exceptions
    "AcademicIntelligenceError",
    "SourceUnavailableError",
    "RateLimitError",
    "DataValidationError",
    "DeduplicationError",
    "StorageError",
]
