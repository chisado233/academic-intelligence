"""Sources module for academic data source plugins.

Provides abstract base class and concrete implementations for various
academic data sources.
"""

from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.sources.core_ import CoreSource
from academic_intelligence.sources.europe_pmc import EuropePmcSource
from academic_intelligence.sources.google_scholar import GoogleScholarSource
from academic_intelligence.sources.ieee import IEEESource
from academic_intelligence.sources.openalex import OpenAlexSource
from academic_intelligence.sources.pubmed import PubMedSource
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource
from academic_intelligence.sources.unpaywall import UnpaywallSource

__all__ = [
    "BaseSource",
    "CoreSource",
    "EuropePmcSource",
    "GoogleScholarSource",
    "SemanticScholarSource",
    "OpenAlexSource",
    "ArxivSource",
    "PubMedSource",
    "IEEESource",
    "UnpaywallSource",
]
