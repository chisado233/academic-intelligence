"""Processors module for post-processing collected academic data.

Provides deduplication, enrichment, validation, and incremental update
capabilities.
"""

from academic_intelligence.processors.deduplicator import Deduplicator, SimilarityConfig
from academic_intelligence.processors.disambiguator import (
    AuthorDisambiguator,
    DisambiguationConfig,
    DisambiguationScore,
)
from academic_intelligence.processors.enricher import Enricher
from academic_intelligence.processors.incremental import IncrementalProcessor
from academic_intelligence.processors.scorer import (
    SOURCE_BASELINE_CONFIDENCE,
    ConfidenceScorer,
)
from academic_intelligence.processors.validator import Validator

__all__ = [
    "Deduplicator",
    "SimilarityConfig",
    "AuthorDisambiguator",
    "DisambiguationConfig",
    "DisambiguationScore",
    "Enricher",
    "IncrementalProcessor",
    "Validator",
    "ConfidenceScorer",
    "SOURCE_BASELINE_CONFIDENCE",
]
