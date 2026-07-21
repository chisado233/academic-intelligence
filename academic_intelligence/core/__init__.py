"""Academic Intelligence - Core Module

This module provides the foundational components for the Academic Intelligence
system, including data models, type definitions, constants, and custom exceptions.
"""

from __future__ import annotations

from academic_intelligence.core.models import (
    Author,
    Citation,
    CollectionResult,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import (
    AntiCrawlStrategy,
    Config,
    SourceType,
)
from academic_intelligence.core.constants import (
    DEFAULT_RATE_LIMIT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_CONFIDENCE,
    SUPPORTED_SOURCES,
)
from academic_intelligence.core.exceptions import (
    AcademicIntelligenceError,
    SourceUnavailableError,
    RateLimitError,
    DataValidationError,
    DeduplicationError,
    StorageError,
)

__all__ = [
    # Data Models
    "Author",
    "Paper",
    "Citation",
    "Evidence",
    "CollectionResult",
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
