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
from typing import Any

from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import Author, Citation, Evidence, Paper

__all__ = ["BaseStorage", "StorageError"]


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
    connection_string: str | None = None

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
        ...

    @abstractmethod
    async def close(self) -> None:
        """Gracefully release the connection or file handle.

        TODO: Implement in concrete subclass.
        """
        ...

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
        ...

    @abstractmethod
    async def get_author(self, author_id: str) -> Author | None:
        """Retrieve an ``Author`` by its unique identifier.

        Args:
            author_id: The record ID.

        Returns:
            The author model, or ``None`` if not found.

        TODO: Implement in concrete subclass.
        """
        ...

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
        ...

    @abstractmethod
    async def delete_author(self, author_id: str) -> bool:
        """Remove an ``Author`` record.

        Args:
            author_id: The record ID.

        Returns:
            ``True`` if the record existed and was removed.

        TODO: Implement in concrete subclass.
        """
        ...

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
        ...

    @abstractmethod
    async def get_paper(self, paper_id: str) -> Paper | None:
        """Retrieve a ``Paper`` by its unique identifier.

        Args:
            paper_id: The record ID.

        Returns:
            The paper model, or ``None`` if not found.

        TODO: Implement in concrete subclass.
        """
        ...

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
        ...

    @abstractmethod
    async def delete_paper(self, paper_id: str) -> bool:
        """Remove a ``Paper`` record.

        Args:
            paper_id: The record ID.

        Returns:
            ``True`` if the record existed and was removed.

        TODO: Implement in concrete subclass.
        """
        ...

    # ------------------------------------------------------------------
    # Citation CRUD
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_citation(self, citation: Citation) -> str:
        """Persist a single ``Citation`` relationship.

        The directed ``(citing_paper_id, cited_paper_id)`` pair is the domain
        identity. Re-saving the pair must update it and return the existing
        relationship ID rather than create a duplicate edge.

        Args:
            citation: The citation model to persist.

        Returns:
            A composite or synthetic identifier for the relationship.

        Raises:
            StorageError: If the record cannot be saved.

        TODO: Implement in concrete subclass.
        """
        ...

    @abstractmethod
    async def get_citations_by_paper(
        self,
        paper_id: str,
        *,
        direction: str = "outgoing",
    ) -> list[Citation]:
        """Retrieve citation relationships for a given paper.

        Args:
            paper_id: The paper whose citations are requested.
            direction: ``"outgoing"`` (this paper cites others) or
                ``"incoming"`` (other papers cite this one).

        Returns:
            List of matching citation records.

        TODO: Implement in concrete subclass.
        """
        ...

    # ------------------------------------------------------------------
    # Graph / relationship edges (3A v2 §8)
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_references(self, paper_id: str) -> list[str]:
        """Return the IDs of papers cited by *paper_id* (outgoing edges).

        Args:
            paper_id: The paper whose references are requested.

        Returns:
            List of cited paper IDs (deduplicated).

        TODO: Implement in concrete subclass.
        """
        ...

    @abstractmethod
    async def get_citations(self, paper_id: str) -> list[str]:
        """Return the IDs of papers that cite *paper_id* (incoming edges).

        Note the semantic distinction from :meth:`get_citations_by_paper`
        (which returns full ``Citation`` records): this helper returns plain
        citing-paper IDs for graph traversal.

        Args:
            paper_id: The paper whose citing papers are requested.

        Returns:
            List of citing paper IDs (deduplicated).

        TODO: Implement in concrete subclass.
        """
        ...

    @abstractmethod
    async def get_coauthors(self, author_id: str) -> list[str]:
        """Return the IDs of authors that co-authored papers with *author_id*.

        Args:
            author_id: The author whose co-authors are requested.

        Returns:
            List of co-author IDs (deduplicated).

        TODO: Implement in concrete subclass.
        """
        ...

    @abstractmethod
    async def get_author_papers(self, author_id: str) -> list[str]:
        """Return the IDs of papers authored by *author_id*.

        Args:
            author_id: The author whose papers are requested.

        Returns:
            List of paper IDs.

        TODO: Implement in concrete subclass.
        """
        ...

    @abstractmethod
    async def save_evidence(
        self,
        entity_type: str,
        entity_id: str,
        evidence_list: list[Evidence],
    ) -> None:
        """Persist an evidence list for a record (``"paper"`` / ``"author"``).

        Args:
            entity_type: ``"paper"`` or ``"author"``.
            entity_id: The record ID the evidence belongs to.
            evidence_list: Evidence entries to store (replaces previous rows).

        TODO: Implement in concrete subclass.
        """
        ...

    @abstractmethod
    async def get_evidence(
        self,
        entity_type: str,
        entity_id: str,
    ) -> list[Evidence]:
        """Return the persisted evidence list for a record.

        Args:
            entity_type: ``"paper"`` or ``"author"``.
            entity_id: The record ID.

        Returns:
            List of evidence entries (empty if none stored).

        TODO: Implement in concrete subclass.
        """
        ...

    # ------------------------------------------------------------------
    # Batch / bulk operations
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_batch(
        self,
        *,
        authors: list[Author] | None = None,
        papers: list[Paper] | None = None,
        citations: list[Citation] | None = None,
    ) -> dict[str, list[str]]:
        """Atomically persist multiple records in one call.

        This is an idempotent upsert contract: explicit author/paper IDs keep
        their local entity identity, citation pairs are unique, and derived
        authorship/coauthorship state must reflect the latest paper byline
        rather than the number of times the batch was replayed.

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
        ...

    # ------------------------------------------------------------------
    # Query / search
    # ------------------------------------------------------------------

    @abstractmethod
    async def query_papers(
        self,
        *,
        author: str | None = None,
        year: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        venue: str | None = None,
        keyword: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "id",
        after: str | None = None,
        cursor: str | None = None,
    ) -> list[Paper]:
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
            order_by: Stable sort key (``"id"``, ``"title"``, or ``"year"``).
            after: Last paper id from the previous keyset page.
            cursor: Alias of ``after``; specifying both is invalid.

        Returns:
            List of matching ``Paper`` records ordered by relevance or
            recency (backend-dependent).

        TODO: Implement in concrete subclass.
        """
        ...

    @abstractmethod
    async def query_authors(
        self,
        *,
        name: str | None = None,
        affiliation: str | None = None,
        interest: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "id",
        after: str | None = None,
        cursor: str | None = None,
    ) -> list[Author]:
        """Search persisted authors with optional filters.

        Args:
            name: Author name (exact or substring match).
            affiliation: Institutional affiliation filter.
            interest: Research interest keyword filter.
            limit: Maximum number of results to return.
            offset: Number of results to skip (for pagination).
            order_by: Stable sort key (``"id"`` or ``"name"``).
            after: Last author id from the previous keyset page.
            cursor: Alias of ``after``; specifying both is invalid.

        Returns:
            List of matching ``Author`` records.

        TODO: Implement in concrete subclass.
        """
        ...

    # ------------------------------------------------------------------
    # Statistics / diagnostics
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """Return high-level statistics about the stored dataset.

        Returns:
            Dictionary with keys such as ``"total_papers"``,
            ``"total_authors"``, ``"total_citations"``, etc.

        TODO: Implement in concrete subclass.
        """
        ...

    # ------------------------------------------------------------------
    # Incremental update metadata
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_paper_hash(self, paper_id: str) -> str | None:
        """Return the stored content hash for a paper, if any.

        Args:
            paper_id: The paper record ID.

        Returns:
            Hex hash string, or ``None`` if not stored.
        """
        ...

    @abstractmethod
    async def save_paper_hash(self, paper_id: str, hash: str) -> None:
        """Persist a content hash for a paper.

        Args:
            paper_id: The paper record ID.
            hash: Content hash string (e.g. SHA-256 prefix).
        """
        ...

    @abstractmethod
    async def get_last_update_time(self, source: str) -> datetime | None:
        """Return the last successful incremental update time for a source.

        Args:
            source: Source identifier (e.g. ``"semantic_scholar"``).

        Returns:
            Timestamp of last update, or ``None`` if never updated.
        """
        ...

    @abstractmethod
    async def save_last_update_time(self, source: str, time: datetime) -> None:
        """Record the last successful incremental update time for a source.

        Args:
            source: Source identifier.
            time: Update timestamp (preferably timezone-aware UTC).
        """
        ...

    @abstractmethod
    async def get_entity_sync(
        self,
        entity_type: str,
        entity_id: str,
        source: str,
    ) -> datetime | None:
        """Return the last successful sync time of an ``(entity, source)`` pair.

        Args:
            entity_type: ``"author"`` or ``"paper"``.
            entity_id: Entity key (normalized author name for authors, the
                paper record id for papers).
            source: Source identifier (e.g. ``"semantic_scholar"``).

        Returns:
            Timestamp of last update, or ``None`` if the pair was never synced.
        """
        ...

    @abstractmethod
    async def save_entity_sync(
        self,
        entity_type: str,
        entity_id: str,
        source: str,
        time: datetime,
    ) -> None:
        """Record the last successful sync time of an ``(entity, source)`` pair.

        Args:
            entity_type: ``"author"`` or ``"paper"``.
            entity_id: Entity key (normalized author name for authors, the
                paper record id for papers).
            source: Source identifier.
            time: Update timestamp (preferably timezone-aware UTC).
        """
        ...
