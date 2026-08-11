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
from typing import ClassVar

from academic_intelligence.core.exceptions import NotSupportedError
from academic_intelligence.core.models import Author, Citation, Paper
from academic_intelligence.core.types import SourceType


def is_rate_limit_status(exc: BaseException) -> bool:
    """Return True when *exc* represents an HTTP 429 rate-limit response.

    The HTTP client raises ``httpx.HTTPStatusError`` (with ``status_code``
    attached) after its retry budget for 429/503/504 is exhausted; sources map
    that back to :class:`~academic_intelligence.core.exceptions.RateLimitError`
    instead of wrapping it as an unreachable-source failure (FIX-D-2).
    """
    status = getattr(exc, "status_code", None)
    if status is not None:
        return int(status) == 429
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 429


def retry_after_from_error(exc: BaseException) -> int | None:
    """Extract ``Retry-After`` seconds from an HTTP error's response header.

    Returns ``None`` when the exception carries no response or the header is
    absent / not an integer.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


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
    capabilities: ClassVar[dict[str, bool]] = {
        # C1 CLI operation keys (technical-design.md §1.1.1): the CLI source
        # registry drives off these; metadata operations are required of
        # every source, the rest are opt-in.
        "search": True,
        "get": True,
        "citations": False,
        "fulltext": False,
        # Long-form method keys (collector dispatch).  Author-class and
        # citation operations are declared unsupported by default (C1
        # decision 1): the base class downgrades them to concrete methods
        # raising ``NotSupportedError``, and sources that implement them
        # must declare the key ``True`` explicitly (the arXiv/IEEE
        # ``get_citations=False`` declaration convention).  The by-id
        # lookups are source-specific (OpenAlex W-id / arXiv id) — absent
        # from the C1 operation set, so they too default to False and are
        # opted in by the sources that expose them.
        "search_papers": True,
        "get_paper_by_doi": True,
        "get_paper_by_arxiv_id": False,
        "get_paper_by_id": False,
        "get_author_papers": False,
        "get_author_profile": False,
        "get_citations": False,
        "get_citing_papers": False,
    }

    def supports(self, operation: str) -> bool:
        """Return whether this adapter declares support for *operation*.

        Capability declarations are the single source of truth (fail-closed,
        technical-design.md §1.1.1 decision 2): an operation absent from
        ``capabilities`` or declared ``False`` is unsupported even when a
        method of that name happens to exist.
        """
        return bool(self.capabilities.get(operation, False))

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
    async def get_paper_by_doi(self, doi: str) -> Paper | None:
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
    async def get_author_papers(self, author_name: str) -> list[Paper]:
        """Retrieve all papers authored by *author_name*.

        Default implementation (C1 decision 1, technical-design.md
        §1.1.1): author-class operations are not required of every source,
        so the base class raises :class:`NotSupportedError` instead of
        declaring this an abstract method.  Sources that implement the
        operation override this method and declare
        ``capabilities["get_author_papers"] = True``.

        Raises:
            NotSupportedError: This source does not support author paper
                lookups.
        """
        raise NotSupportedError(
            f"source {self.name!r} does not support author paper lookups",
            source_name=self.name,
        )

    async def get_author_profile(self, author_name: str) -> Author | None:
        """Retrieve profile metadata for an author.

        Default implementation (C1 decision 1, technical-design.md
        §1.1.1): author-class operations are not required of every source,
        so the base class raises :class:`NotSupportedError` instead of
        declaring this an abstract method.  Sources that implement the
        operation override this method and declare
        ``capabilities["get_author_profile"] = True``.

        Raises:
            NotSupportedError: This source does not support author profile
                lookups.
        """
        raise NotSupportedError(
            f"source {self.name!r} does not support author profile lookups",
            source_name=self.name,
        )

    # ------------------------------------------------------------------
    # Citation queries
    # ------------------------------------------------------------------
    async def get_citations(self, paper_id: str) -> list[Citation]:
        """Retrieve citation relationships for a given paper.

        Default implementation (C1 decision 1, technical-design.md
        §1.1.1): citation operations are not required of every source,
        so the base class raises :class:`NotSupportedError` instead of
        declaring this an abstract method.  Sources that implement the
        operation override this method and declare
        ``capabilities["get_citations"] = True``.

        Raises:
            NotSupportedError: This source does not support citation
                graph lookups.
        """
        raise NotSupportedError(
            f"source {self.name!r} does not support citation graph lookups",
            source_name=self.name,
        )
