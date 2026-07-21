"""Base source module for the Academic Intelligence system.

This module defines the abstract base class :class:`BaseSource` that all
academic data source plugins must implement. It establishes the contract
between the collection orchestration layer and individual source adapters
(e.g. Google Scholar, arXiv, Semantic Scholar, OpenAlex, PubMed, IEEE Xplore).

Responsibilities
----------------
- Define the common interface for querying papers, authors, and citations.
- Provide shared utilities for rate limiting, retry logic, and response parsing.
- Enforce evidence tracking so that every returned data point carries provenance
  and confidence metadata.

Typical usage::

    from academic_intelligence.sources.base import BaseSource

    class MySource(BaseSource):
        name = "my_source"

        async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
            ...

"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from academic_intelligence.core.models import Author, Paper, Citation
from academic_intelligence.core.types import SourceType


class BaseSource(ABC):
    """Abstract base class for all academic data source adapters.

    Each concrete source (Google Scholar, arXiv, etc.) must subclass this
    class and implement the three primary query methods. The orchestration
    layer in :mod:`academic_intelligence.collectors` will instantiate and
    drive these sources concurrently.

    Attributes:
        name: Short, URL-safe identifier for the source (e.g. ``"arxiv"``).
        source_type: Member of :class:`~academic_intelligence.core.types.SourceType`
            that corresponds to this adapter.
    """

    name: str = ""
    source_type: SourceType

    # ------------------------------------------------------------------
    # Paper queries
    # ------------------------------------------------------------------
    @abstractmethod
    async def search_papers(self, query: str, limit: int = 10) -> list[Paper]:
        """Search for papers matching *query*.

        Args:
            query: Free-text search string (title, keywords, etc.).
            limit: Maximum number of results to return.

        Returns:
            A list of :class:`~academic_intelligence.core.models.Paper`
            objects, each carrying :class:`~academic_intelligence.core.models.Evidence`.

        Raises:
            SourceUnavailableError: If the source is unreachable.
            RateLimitError: If the request exceeds the source's rate limit.
            ParseError: If the response cannot be parsed.
        """
        # TODO: implement search_papers
        ...

    @abstractmethod
    async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
        """Retrieve a single paper by its DOI.

        Args:
            doi: Digital Object Identifier (e.g. ``"10.1038/nature14539"``).

        Returns:
            The matching :class:`~academic_intelligence.core.models.Paper`
            or ``None`` if not found.

        Raises:
            SourceUnavailableError: If the source is unreachable.
            RateLimitError: If the request exceeds the source's rate limit.
            ParseError: If the response cannot be parsed.
        """
        # TODO: implement get_paper_by_doi
        ...

    # ------------------------------------------------------------------
    # Author queries
    # ------------------------------------------------------------------
    @abstractmethod
    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Retrieve all papers authored by *author_name*.

        Args:
            author_name: Full or partial author name.

        Returns:
            A list of :class:`~academic_intelligence.core.models.Paper`
            objects associated with the author.

        Raises:
            SourceUnavailableError: If the source is unreachable.
            RateLimitError: If the request exceeds the source's rate limit.
            ParseError: If the response cannot be parsed.
        """
        # TODO: implement get_author_papers
        ...

    @abstractmethod
    async def get_author_profile(self, author_name: str) -> Optional[Author]:
        """Retrieve profile metadata for an author.

        Args:
            author_name: Full or partial author name.

        Returns:
            An :class:`~academic_intelligence.core.models.Author` object
            containing affiliation, h-index, interests, etc., or ``None``
            if the source has no profile for this name.

        Raises:
            SourceUnavailableError: If the source is unreachable.
            RateLimitError: If the request exceeds the source's rate limit.
            ParseError: If the response cannot be parsed.
        """
        # TODO: implement get_author_profile
        ...

    # ------------------------------------------------------------------
    # Citation queries
    # ------------------------------------------------------------------
    @abstractmethod
    async def get_citations(self, paper_id: str) -> list[Citation]:
        """Retrieve citation relationships for a given paper.

        Args:
            paper_id: Unique identifier of the paper within this source.
                (Note: this is the source-local ID, not necessarily a DOI.)

        Returns:
            A list of :class:`~academic_intelligence.core.models.Citation`
            objects where *cited_paper_id* equals *paper_id*.

        Raises:
            SourceUnavailableError: If the source is unreachable.
            RateLimitError: If the request exceeds the source's rate limit.
            ParseError: If the response cannot be parsed.
        """
        # TODO: implement get_citations
        ...
