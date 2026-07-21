"""Academic Intelligence - Storage Base Module

This module defines the abstract base class and shared interface contract for
all persistence backends in the Academic Intelligence system.

Responsibilities:
    - Define the ``BaseStorage`` abstract interface that concrete backends
      (SQLite, JSON, etc.) must implement.
    - Provide type-annotated method signatures for CRUD operations on
      ``Author``, ``Paper``, and ``Citation`` records.
    - Establish transaction-like semantics (save / update / delete / query) so
      that higher-level collectors and processors can persist results without
      knowing backend details.

Not responsible for:
    - Actual SQL or JSON serialization logic (see ``sqlite_store.py`` and
      ``json_store.py`` for concrete implementations).
    - Connection pooling, migration management, or schema versioning.
    - Caching or in-memory buffering (handled by callers or dedicated cache
      layers).

Input dependencies:
    - ``academic_intelligence.core.models`` for data models.
    - ``academic_intelligence.core.exceptions`` for storage-level errors.

Output / consumers:
    - ``collectors/`` — save collection results.
    - ``processors/`` — update deduplicated / enriched records.
    - ``cli.py`` — query stored data for CLI output.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from academic_intelligence.core.models import Author, Citation, Paper
from academic_intelligence.core.exceptions import StorageError


class BaseStorage(ABC):
    """Abstract base class for all Academic Intelligence storage backends.

    Concrete implementations must provide durable persistence for
    ``Author``, ``Paper``, and ``Citation`` records, and support
    filtering queries used by collectors, processors, and the CLI.

    Attributes:
        backend_name: Human-readable identifier of the backend (e.g. ``"sqlite"``,
            ``"json"``). Used for error reporting and logging.
        connection_string: Optional URI or file path that the backend uses.
    """

    backend_name: str = "abstract"
    connection_string: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Establish the underlying storage connection or open the file handle.

        Raises:
            StorageError: If the backend cannot be initialised.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def close(self) -> None:
        """Gracefully release the connection or file handle.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    # ------------------------------------------------------------------
    # Author CRUD
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_author(self, author: Author) -> str:
        """Persist a single ``Author`` record.

        Args:
            author: The author model to persist.  If ``author.id`` is ``None``
                the backend must assign a new unique identifier.

        Returns:
            The unique identifier of the persisted record.

        Raises:
            StorageError: If the record cannot be saved.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def get_author(self, author_id: str) -> Optional[Author]:
        """Retrieve an ``Author`` by its unique identifier.

        Args:
            author_id: The record ID.

        Returns:
            The author model, or ``None`` if not found.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def update_author(self, author_id: str, author: Author) -> bool:
        """Replace an existing ``Author`` record.

        Args:
            author_id: The ID of the record to update.
            author: New author data (may carry a different ``id``; the
                ``author_id`` parameter takes precedence).

        Returns:
            ``True`` if the record existed and was updated.

        Raises:
            StorageError: If the update operation fails.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def delete_author(self, author_id: str) -> bool:
        """Remove an ``Author`` record.

        Args:
            author_id: The record ID.

        Returns:
            ``True`` if the record existed and was removed.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    # ------------------------------------------------------------------
    # Paper CRUD
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_paper(self, paper: Paper) -> str:
        """Persist a single ``Paper`` record.

        Args:
            paper: The paper model to persist.  If ``paper.id`` is ``None``
                the backend must assign a new unique identifier.

        Returns:
            The unique identifier of the persisted record.

        Raises:
            StorageError: If the record cannot be saved.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def get_paper(self, paper_id: str) -> Optional[Paper]:
        """Retrieve a ``Paper`` by its unique identifier.

        Args:
            paper_id: The record ID.

        Returns:
            The paper model, or ``None`` if not found.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def update_paper(self, paper_id: str, paper: Paper) -> bool:
        """Replace an existing ``Paper`` record.

        Args:
            paper_id: The ID of the record to update.
            paper: New paper data.

        Returns:
            ``True`` if the record existed and was updated.

        Raises:
            StorageError: If the update operation fails.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def delete_paper(self, paper_id: str) -> bool:
        """Remove a ``Paper`` record.

        Args:
            paper_id: The record ID.

        Returns:
            ``True`` if the record existed and was removed.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    # ------------------------------------------------------------------
    # Citation CRUD
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_citation(self, citation: Citation) -> str:
        """Persist a single ``Citation`` relationship.

        Args:
            citation: The citation model to persist.

        Returns:
            A composite or synthetic identifier for the relationship.

        Raises:
            StorageError: If the record cannot be saved.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def get_citations_by_paper(
        self,
        paper_id: str,
        *,
        direction: str = "outgoing",
    ) -> List[Citation]:
        """Retrieve citation relationships for a given paper.

        Args:
            paper_id: The paper whose citations are requested.
            direction: ``"outgoing"`` (this paper cites others) or
                ``"incoming"`` (other papers cite this one).

        Returns:
            List of matching citation records.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    # ------------------------------------------------------------------
    # Batch / bulk operations
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_batch(
        self,
        *,
        authors: Optional[List[Author]] = None,
        papers: Optional[List[Paper]] = None,
        citations: Optional[List[Citation]] = None,
    ) -> Dict[str, List[str]]:
        """Atomically persist multiple records in one call.

        Args:
            authors: Optional list of authors to save.
            papers: Optional list of papers to save.
            citations: Optional list of citations to save.

        Returns:
            Mapping ``{"authors": [...], "papers": [...], "citations": [...]}``
            containing the assigned IDs for each category.

        Raises:
            StorageError: If any record in the batch cannot be saved.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    # ------------------------------------------------------------------
    # Query / search
    # ------------------------------------------------------------------

    @abstractmethod
    async def query_papers(
        self,
        *,
        author: Optional[str] = None,
        year: Optional[int] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        venue: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Paper]:
        """Search persisted papers with optional filters.

        All parameters are combined with AND semantics.

        Args:
            author: Filter by author name (exact or substring match —
                backend-dependent).
            year: Exact publication year.
            year_from: Minimum publication year (inclusive).
            year_to: Maximum publication year (inclusive).
            venue: Filter by journal or conference name.
            keyword: Filter by keyword or tag.
            limit: Maximum number of results to return.
            offset: Number of results to skip (for pagination).

        Returns:
            List of matching ``Paper`` records ordered by relevance or
            recency (backend-dependent).

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def query_authors(
        self,
        *,
        name: Optional[str] = None,
        affiliation: Optional[str] = None,
        interest: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Author]:
        """Search persisted authors with optional filters.

        Args:
            name: Author name (exact or substring match).
            affiliation: Institutional affiliation filter.
            interest: Research interest keyword filter.
            limit: Maximum number of results to return.
            offset: Number of results to skip (for pagination).

        Returns:
            List of matching ``Author`` records.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    # ------------------------------------------------------------------
    # Statistics / diagnostics
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_stats(self) -> Dict[str, Any]:
        """Return high-level statistics about the stored dataset.

        Returns:
            Dictionary with keys such as ``"total_papers"``,
            ``"total_authors"``, ``"total_citations"``, etc.

        TODO: Implement in concrete subclass.
        """
        ...  # type: ignore[empty-body]

    # ------------------------------------------------------------------
    # Incremental update metadata
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_paper_hash(self, paper_id: str) -> Optional[str]:
        """Return the stored content hash for a paper, if any.

        Args:
            paper_id: The paper record ID.

        Returns:
            Hex hash string, or ``None`` if not stored.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def save_paper_hash(self, paper_id: str, hash: str) -> None:
        """Persist a content hash for a paper.

        Args:
            paper_id: The paper record ID.
            hash: Content hash string (e.g. SHA-256 prefix).
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def get_last_update_time(self, source: str) -> Optional[datetime]:
        """Return the last successful incremental update time for a source.

        Args:
            source: Source identifier (e.g. ``"semantic_scholar"``).

        Returns:
            Timestamp of last update, or ``None`` if never updated.
        """
        ...  # type: ignore[empty-body]

    @abstractmethod
    async def save_last_update_time(self, source: str, time: datetime) -> None:
        """Record the last successful incremental update time for a source.

        Args:
            source: Source identifier.
            time: Update timestamp (preferably timezone-aware UTC).
        """
        ...  # type: ignore[empty-body]
