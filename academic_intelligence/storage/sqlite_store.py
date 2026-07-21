"""SQLite storage backend for Academic Intelligence.

Provides persistent storage for Author, Paper, and Citation records
using SQLite with SQLAlchemy 2.0 async ORM.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.storage.base import BaseStorage


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


class PaperRow(Base):
    __tablename__ = "papers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    authors: Mapped[Any] = mapped_column(JSON, default=list)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    venue: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    abstract: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    doi: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    citations: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    keywords: Mapped[Any] = mapped_column(JSON, default=list)
    evidence: Mapped[Any] = mapped_column(JSON, nullable=False)


class AuthorRow(Base):
    __tablename__ = "authors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    affiliation: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    homepage: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    h_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    citations: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    interests: Mapped[Any] = mapped_column(JSON, default=list)
    profile_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence: Mapped[Any] = mapped_column(JSON, nullable=False)


class CitationRow(Base):
    __tablename__ = "citations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    citing_paper_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cited_paper_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evidence: Mapped[Any] = mapped_column(JSON, nullable=False)


class PaperHashRow(Base):
    """Content hash cache for incremental change detection."""

    __tablename__ = "paper_hashes"

    paper_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SourceUpdateRow(Base):
    """Last successful incremental update timestamp per source."""

    __tablename__ = "source_updates"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_update: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _new_id() -> str:
    return uuid.uuid4().hex


def _evidence_to_dict(evidence: Evidence) -> Dict[str, Any]:
    return evidence.model_dump(mode="json")


def _evidence_from_dict(data: Dict[str, Any]) -> Evidence:
    return Evidence.model_validate(data)


def _paper_to_row(paper: Paper, paper_id: str) -> PaperRow:
    return PaperRow(
        id=paper_id,
        title=paper.title,
        authors=list(paper.authors),
        year=paper.year,
        venue=paper.venue,
        abstract=paper.abstract,
        doi=paper.doi,
        url=paper.url,
        pdf_url=paper.pdf_url,
        citations=paper.citations,
        keywords=list(paper.keywords),
        evidence=_evidence_to_dict(paper.evidence),
    )


def _row_to_paper(row: PaperRow) -> Paper:
    return Paper(
        id=row.id,
        title=row.title,
        authors=list(row.authors or []),
        year=row.year,
        venue=row.venue,
        abstract=row.abstract,
        doi=row.doi,
        url=row.url,
        pdf_url=row.pdf_url,
        citations=row.citations,
        keywords=list(row.keywords or []),
        evidence=_evidence_from_dict(row.evidence),
    )


def _author_to_row(author: Author, author_id: str) -> AuthorRow:
    return AuthorRow(
        id=author_id,
        name=author.name,
        affiliation=author.affiliation,
        email=author.email,
        homepage=author.homepage,
        h_index=author.h_index,
        citations=author.citations,
        interests=list(author.interests),
        profile_url=author.profile_url,
        evidence=_evidence_to_dict(author.evidence),
    )


def _row_to_author(row: AuthorRow) -> Author:
    return Author(
        id=row.id,
        name=row.name,
        affiliation=row.affiliation,
        email=row.email,
        homepage=row.homepage,
        h_index=row.h_index,
        citations=row.citations,
        interests=list(row.interests or []),
        profile_url=row.profile_url,
        evidence=_evidence_from_dict(row.evidence),
    )


def _citation_to_row(citation: Citation, citation_id: str) -> CitationRow:
    return CitationRow(
        id=citation_id,
        citing_paper_id=citation.citing_paper_id,
        cited_paper_id=citation.cited_paper_id,
        evidence=_evidence_to_dict(citation.evidence),
    )


def _row_to_citation(row: CitationRow) -> Citation:
    return Citation(
        citing_paper_id=row.citing_paper_id,
        cited_paper_id=row.cited_paper_id,
        evidence=_evidence_from_dict(row.evidence),
    )


class SQLiteStorage(BaseStorage):
    """SQLite-backed storage implementation using SQLAlchemy 2.0 async ORM."""

    backend_name: str = "sqlite"

    def __init__(self, db_path: str = "./academic_data.db") -> None:
        """Initialize SQLite storage.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = db_path
        self.connection_string = f"sqlite+aiosqlite:///{db_path}"
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None

    def _session(self) -> AsyncSession:
        if self._session_factory is None:
            raise StorageError("Storage not connected", backend=self.backend_name)
        return self._session_factory()

    async def connect(self) -> None:
        """Establish database connection and create tables."""
        try:
            self._engine = create_async_engine(
                self.connection_string,
                echo=False,
            )
            self._session_factory = async_sessionmaker(
                self._engine,
                expire_on_commit=False,
            )
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        except Exception as exc:
            raise StorageError(
                f"Failed to connect SQLite storage: {exc}",
                backend=self.backend_name,
                context={"db_path": self.db_path},
            ) from exc

    async def close(self) -> None:
        """Close database connection and dispose engine."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    async def save_paper(self, paper: Paper) -> str:
        paper_id = paper.id or _new_id()
        try:
            async with self._session() as session:
                existing = await session.get(PaperRow, paper_id)
                if existing is not None:
                    existing.title = paper.title
                    existing.authors = list(paper.authors)
                    existing.year = paper.year
                    existing.venue = paper.venue
                    existing.abstract = paper.abstract
                    existing.doi = paper.doi
                    existing.url = paper.url
                    existing.pdf_url = paper.pdf_url
                    existing.citations = paper.citations
                    existing.keywords = list(paper.keywords)
                    existing.evidence = _evidence_to_dict(paper.evidence)
                else:
                    session.add(_paper_to_row(paper, paper_id))
                await session.commit()
            return paper_id
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Failed to save paper: {exc}",
                backend=self.backend_name,
            ) from exc

    async def get_paper(self, paper_id: str) -> Optional[Paper]:
        async with self._session() as session:
            row = await session.get(PaperRow, paper_id)
            return _row_to_paper(row) if row else None

    async def update_paper(self, paper_id: str, paper: Paper) -> bool:
        async with self._session() as session:
            row = await session.get(PaperRow, paper_id)
            if row is None:
                return False
            row.title = paper.title
            row.authors = list(paper.authors)
            row.year = paper.year
            row.venue = paper.venue
            row.abstract = paper.abstract
            row.doi = paper.doi
            row.url = paper.url
            row.pdf_url = paper.pdf_url
            row.citations = paper.citations
            row.keywords = list(paper.keywords)
            row.evidence = _evidence_to_dict(paper.evidence)
            await session.commit()
            return True

    async def delete_paper(self, paper_id: str) -> bool:
        async with self._session() as session:
            row = await session.get(PaperRow, paper_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def save_author(self, author: Author) -> str:
        author_id = author.id or _new_id()
        try:
            async with self._session() as session:
                existing = await session.get(AuthorRow, author_id)
                if existing is not None:
                    existing.name = author.name
                    existing.affiliation = author.affiliation
                    existing.email = author.email
                    existing.homepage = author.homepage
                    existing.h_index = author.h_index
                    existing.citations = author.citations
                    existing.interests = list(author.interests)
                    existing.profile_url = author.profile_url
                    existing.evidence = _evidence_to_dict(author.evidence)
                else:
                    session.add(_author_to_row(author, author_id))
                await session.commit()
            return author_id
        except Exception as exc:
            raise StorageError(
                f"Failed to save author: {exc}",
                backend=self.backend_name,
            ) from exc

    async def get_author(self, author_id: str) -> Optional[Author]:
        async with self._session() as session:
            row = await session.get(AuthorRow, author_id)
            return _row_to_author(row) if row else None

    async def update_author(self, author_id: str, author: Author) -> bool:
        async with self._session() as session:
            row = await session.get(AuthorRow, author_id)
            if row is None:
                return False
            row.name = author.name
            row.affiliation = author.affiliation
            row.email = author.email
            row.homepage = author.homepage
            row.h_index = author.h_index
            row.citations = author.citations
            row.interests = list(author.interests)
            row.profile_url = author.profile_url
            row.evidence = _evidence_to_dict(author.evidence)
            await session.commit()
            return True

    async def delete_author(self, author_id: str) -> bool:
        async with self._session() as session:
            row = await session.get(AuthorRow, author_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def save_citation(self, citation: Citation) -> str:
        citation_id = _new_id()
        try:
            async with self._session() as session:
                session.add(_citation_to_row(citation, citation_id))
                await session.commit()
            return citation_id
        except Exception as exc:
            raise StorageError(
                f"Failed to save citation: {exc}",
                backend=self.backend_name,
            ) from exc

    async def get_citations_by_paper(
        self,
        paper_id: str,
        *,
        direction: str = "outgoing",
    ) -> List[Citation]:
        async with self._session() as session:
            if direction == "incoming":
                stmt = select(CitationRow).where(CitationRow.cited_paper_id == paper_id)
            else:
                stmt = select(CitationRow).where(CitationRow.citing_paper_id == paper_id)
            result = await session.execute(stmt)
            return [_row_to_citation(r) for r in result.scalars().all()]

    async def save_batch(
        self,
        *,
        authors: Optional[List[Author]] = None,
        papers: Optional[List[Paper]] = None,
        citations: Optional[List[Citation]] = None,
    ) -> Dict[str, List[str]]:
        ids: Dict[str, List[str]] = {"authors": [], "papers": [], "citations": []}
        try:
            async with self._session() as session:
                for author in authors or []:
                    author_id = author.id or _new_id()
                    session.add(_author_to_row(author, author_id))
                    ids["authors"].append(author_id)
                for paper in papers or []:
                    paper_id = paper.id or _new_id()
                    session.add(_paper_to_row(paper, paper_id))
                    ids["papers"].append(paper_id)
                for citation in citations or []:
                    citation_id = _new_id()
                    session.add(_citation_to_row(citation, citation_id))
                    ids["citations"].append(citation_id)
                await session.commit()
            return ids
        except Exception as exc:
            raise StorageError(
                f"Failed to save batch: {exc}",
                backend=self.backend_name,
            ) from exc

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
        async with self._session() as session:
            stmt = select(PaperRow)
            if year is not None:
                stmt = stmt.where(PaperRow.year == year)
            if year_from is not None:
                stmt = stmt.where(PaperRow.year >= year_from)
            if year_to is not None:
                stmt = stmt.where(PaperRow.year <= year_to)
            if venue is not None:
                stmt = stmt.where(PaperRow.venue.ilike(f"%{venue}%"))
            if keyword is not None:
                pattern = f"%{keyword}%"
                stmt = stmt.where(
                    or_(
                        PaperRow.title.ilike(pattern),
                        PaperRow.abstract.ilike(pattern),
                    )
                )
            stmt = stmt.offset(offset).limit(limit)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

            # Filter by author in Python (JSON list)
            papers = [_row_to_paper(r) for r in rows]
            if author:
                author_lower = author.lower()
                papers = [
                    p
                    for p in papers
                    if any(author_lower in a.lower() for a in p.authors)
                ]
            return papers

    async def query_authors(
        self,
        *,
        name: Optional[str] = None,
        affiliation: Optional[str] = None,
        interest: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Author]:
        async with self._session() as session:
            stmt = select(AuthorRow)
            if name is not None:
                stmt = stmt.where(AuthorRow.name.ilike(f"%{name}%"))
            if affiliation is not None:
                stmt = stmt.where(AuthorRow.affiliation.ilike(f"%{affiliation}%"))
            stmt = stmt.offset(offset).limit(limit)
            result = await session.execute(stmt)
            authors = [_row_to_author(r) for r in result.scalars().all()]
            if interest:
                interest_lower = interest.lower()
                authors = [
                    a
                    for a in authors
                    if any(interest_lower in i.lower() for i in a.interests)
                ]
            return authors

    async def get_stats(self) -> Dict[str, Any]:
        async with self._session() as session:
            papers = await session.scalar(select(func.count()).select_from(PaperRow))
            authors = await session.scalar(select(func.count()).select_from(AuthorRow))
            citations = await session.scalar(select(func.count()).select_from(CitationRow))
            return {
                "total_papers": int(papers or 0),
                "total_authors": int(authors or 0),
                "total_citations": int(citations or 0),
                "backend": self.backend_name,
                "db_path": self.db_path,
            }

    # ------------------------------------------------------------------
    # Incremental update metadata
    # ------------------------------------------------------------------

    async def get_paper_hash(self, paper_id: str) -> Optional[str]:
        async with self._session() as session:
            row = await session.get(PaperHashRow, paper_id)
            return row.content_hash if row else None

    async def save_paper_hash(self, paper_id: str, hash: str) -> None:
        try:
            async with self._session() as session:
                existing = await session.get(PaperHashRow, paper_id)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                if existing is not None:
                    existing.content_hash = hash
                    existing.updated_at = now
                else:
                    session.add(
                        PaperHashRow(
                            paper_id=paper_id,
                            content_hash=hash,
                            updated_at=now,
                        )
                    )
                await session.commit()
        except Exception as exc:
            raise StorageError(
                f"Failed to save paper hash: {exc}",
                backend=self.backend_name,
            ) from exc

    async def get_last_update_time(self, source: str) -> Optional[datetime]:
        async with self._session() as session:
            row = await session.get(SourceUpdateRow, source)
            return row.last_update if row else None

    async def save_last_update_time(self, source: str, time: datetime) -> None:
        try:
            async with self._session() as session:
                existing = await session.get(SourceUpdateRow, source)
                # Store as naive UTC for SQLite DateTime compatibility
                stored = time.replace(tzinfo=None) if time.tzinfo else time
                if existing is not None:
                    existing.last_update = stored
                else:
                    session.add(SourceUpdateRow(source=source, last_update=stored))
                await session.commit()
        except Exception as exc:
            raise StorageError(
                f"Failed to save last update time: {exc}",
                backend=self.backend_name,
            ) from exc
