"""Processors module for post-processing collected academic data.

Provides deduplication, enrichment, and validation capabilities.
"""

from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.processors.enricher import Enricher
from academic_intelligence.processors.validator import Validator

__all__ = ["Deduplicator", "Enricher", "Validator"]
