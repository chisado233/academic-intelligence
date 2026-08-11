"""SQLite storage backend for Academic Intelligence.

Provides persistent storage for Author, Paper, and Citation records
using SQLite with SQLAlchemy 2.0 async ORM.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    and_,
    bindparam,
    delete,
    func,
    insert,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError as SqlAlchemyOperationalError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    Citation,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import SourceType
from academic_intelligence.fulltext.models import FullText, Segment
from academic_intelligence.processors.scorer import ConfidenceScorer
from academic_intelligence.storage.base import BaseStorage
from academic_intelligence.utils.names import (
    author_name_matches,
    normalize_author_tokens,
)
from academic_intelligence.utils.normalize import normalize_nfc
from academic_intelligence.webcrawler.models import CrawlCacheRecord

logger = logging.getLogger(__name__)

# (FIX-I F3) How many times connect() retries the WAL switch / create_all
# when it races another process on a brand-new database (I-3): the loser can
# hit "database is locked" (WAL lock) or "table ... already exists"
# (create_all collision); both are transient and converge once the winner
# finishes.
_CONNECT_RETRIES = 5

# (B7-P43 V2) FTS5 trigram table backing the substring branch of the
# author-name query prefilter (``query_papers(author=...)``).  SQLite's
# ``LIKE '%...%'`` on a trigram-indexed FTS5 table uses the index, so a
# name substring such as ``"田中"`` in ``"田中太郎"`` or ``"ffrey"`` in
# ``"Geoffrey"`` is found in ms instead of a ``papers.authors LIKE``
# full-table scan.  The table is a plain FTS5 (content stored internally);
# the token-equality branch lives in the B-tree table
# :class:`PaperAuthorTokenRow`.
_AUTHOR_FTS_TABLE = "paper_author_names_fts"

# (FIX-AB-4) FTS5 trigram table backing the keyword-query path
# (``query_papers(keyword=...)``).  SQLite's ``LIKE '%...%'`` on a
# trigram-indexed FTS5 table uses the index, so a title/abstract substring
# is found in ms instead of a ``papers.title LIKE`` / ``papers.abstract
# LIKE`` full-table scan — the same mechanism as the author-name trigram
# table above.  The table is a plain FTS5 (content stored internally);
# rows are kept in lockstep with ``papers`` on every write path via
# :func:`_replace_paper_text_index` and backfilled by
# :meth:`SQLiteStorage._migrate_paper_text_index`.
_PAPER_TEXT_FTS_TABLE = "paper_text_fts"


async def _run_with_transient_retry(op: Callable[[], Awaitable[Any]]) -> None:
    """Run *op* retrying transient concurrent-first-connect errors (I-3).

    Aiosqlite executes ``run_sync`` / ``execute`` on the event-loop thread,
    so two processes racing to initialise a brand-new database can transiently
    collide: the loser either hits ``database is locked`` while the winner
    holds the WAL-switch lock, or ``table ... already exists`` while the
    winner is mid-``create_all``.  Both resolve once the winner finishes, so
    the operation is retried a bounded number of times.
    """
    for attempt in range(_CONNECT_RETRIES):
        try:
            await op()
            return
        except SqlAlchemyOperationalError as exc:
            message = str(exc)
            if not (
                "already exists" in message or "database is locked" in message
            ) or attempt == _CONNECT_RETRIES - 1:
                raise
            await asyncio.sleep(0.05)


# (FIX-AE F1 / AE-1) Write-path lock-contention retry budget.  A write
# transaction that loses SQLite's WAL single-writer race raises
# ``database is locked`` after the connection's busy_timeout; the batch /
# single-record upserts are idempotent (C-1), so the whole transaction is
# retried up to ``_WRITE_RETRIES`` times with a fixed 50ms delay.  P50
# round-32 V1.4: at 32-way concurrent writers 6/32 processes hard-failed
# because the 10s busy_timeout alone was exhausted — a bounded retry absorbs
# the same contention the connect path already handles (I-3).
_WRITE_RETRIES = 5
_WRITE_RETRY_DELAY = 0.05

# Bound TypeVar so :func:`_retry_busy` preserves the decorated method's exact
# signature (override checks against BaseStorage stay intact), mirroring the
# ``_ReadMethod`` pattern of :func:`_typed_read`.
_WriteMethod = TypeVar("_WriteMethod", bound=Callable[..., Awaitable[Any]])


def _is_lock_contention(exc: Exception) -> bool:
    """True when *exc* is a SQLite lock-contention error worth retrying (AE-1).

    ``SQLITE_BUSY`` (``database is locked``) and ``SQLITE_LOCKED``
    (``database table is locked``) are both transient under WAL
    single-writer contention; every other error is permanent and must
    propagate immediately.
    """
    return isinstance(
        exc, (SqlAlchemyOperationalError, sqlite3.OperationalError)
    ) and (
        "database is locked" in str(exc) or "database table is locked" in str(exc)
    )


def _retry_busy(method: _WriteMethod) -> _WriteMethod:
    """Retry a write method on transient SQLite lock contention (AE-1).

    Each write method converts its raw DBAPI/ORM failure into a
    :class:`StorageError` via ``raise _write_storage_error(...) from exc``;
    the decorator inspects that ``__cause__`` (the original exception) and
    re-runs the whole method — a fresh session, a fresh transaction — when
    it was lock contention.  Re-running is safe because every write path is
    an idempotent upsert and each transaction commits atomically (C-1).  A
    lock that outlives the budget surfaces the last ``StorageError``
    unchanged, exactly as before the fix.
    """

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(_WRITE_RETRIES):
            try:
                return await method(self, *args, **kwargs)
            except StorageError as exc:
                cause = exc.__cause__
                if (
                    attempt == _WRITE_RETRIES - 1
                    or not isinstance(
                        cause,
                        (SqlAlchemyOperationalError, sqlite3.OperationalError),
                    )
                    or not _is_lock_contention(cause)
                ):
                    raise
                await asyncio.sleep(_WRITE_RETRY_DELAY)
        raise AssertionError("unreachable")

    return cast(_WriteMethod, wrapper)


# (FIX-AA-1 / FIX-AA-2) Error-message hygiene: SQLAlchemy appends the full
# SQL statement and parameters to statement failures (``[SQL: ...]`` /
# ``[parameters: ...]``) and DBAPI errors carry the raw message; error
# messages must keep only the diagnostic category, and path labels must
# never expose absolute paths.
_SQL_ERROR_SUFFIXES = ("[SQL:", "[parameters:")
_MAX_ERROR_DETAIL = 500


def _trim_error_detail(detail: str) -> str:
    """Trim DBAPI/SQLAlchemy error text to its category (FIX-AA-1).

    Strips the ``[SQL: ...]`` / ``[parameters: ...]`` suffixes SQLAlchemy
    appends to statement failures (so a "LIKE or GLOB pattern too complex"
    stays diagnosable without leaking the full ``SELECT``) and caps the
    length so messages stay bounded.
    """
    for marker in _SQL_ERROR_SUFFIXES:
        if marker in detail:
            detail = detail.split(marker, 1)[0].strip()
    return detail[:_MAX_ERROR_DETAIL]


def _safe_path_label(path: str) -> str:
    """Return a readable, non-leaking label for a storage path (FIX-AA-2).

    The basename only — absolute paths are never exposed through
    ``get_stats`` or ``StorageError`` context.
    """
    return os.path.basename(os.path.normpath(path)) or path


def _write_storage_error(action: str, exc: Exception, db_path: str) -> StorageError:
    """Hygienic StorageError for a write failure (FIX-AD F1 / AD-1).

    Write methods previously embedded the raw exception verbatim, leaking the
    full ``[SQL: ...]`` / ``[parameters: ...]`` suffixes SQLAlchemy appends
    to statement failures (P49 round-31 V2.1: ENOSPC / read-only saves carried
    the complete INSERT).  The detail is trimmed to its diagnostic category
    and capped (FIX-AA-1), and the db path is exposed as a basename-only
    label (FIX-AA-2) — the same contract the read path already follows.
    """
    detail = _trim_error_detail(str(exc))
    return StorageError(
        f"Failed to {action}: {detail}",
        backend="sqlite",
        context={"db_path": _safe_path_label(db_path), "detail": detail},
    )


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


class PaperRow(Base):
    __tablename__ = "papers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # (FIX-Z Z-2) NOT NULL list/status columns carry a server default so
    # writes that bypass the ORM (old-code bare INSERT) do not hard-fail
    # with ``IntegrityError NOT NULL constraint failed``.
    authors: Mapped[Any] = mapped_column(JSON, default=list, server_default="[]")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    venue: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    venue_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    pmid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reference_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keywords: Mapped[Any] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    fields_of_study: Mapped[Any] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    references: Mapped[Any] = mapped_column(JSON, nullable=True)
    citations_list: Mapped[Any] = mapped_column(JSON, nullable=True)
    evidence: Mapped[Any] = mapped_column(
        JSON, nullable=False, server_default="[]"
    )


class AuthorRow(Base):
    __tablename__ = "authors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    orcid: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    semantic_scholar_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    openalex_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    aliases: Mapped[Any] = mapped_column(JSON, default=list, server_default="[]")
    disambiguation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="auto", server_default="auto"
    )
    coauthors: Mapped[Any] = mapped_column(JSON, default=list, server_default="[]")
    venues: Mapped[Any] = mapped_column(JSON, default=list, server_default="[]")
    active_years: Mapped[Any] = mapped_column(JSON, nullable=True)
    affiliation: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    homepage: Mapped[str | None] = mapped_column(Text, nullable=True)
    h_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interests: Mapped[Any] = mapped_column(JSON, default=list, server_default="[]")
    profile_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[Any] = mapped_column(
        JSON, nullable=False, server_default="[]"
    )


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
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PaperAuthorTokenRow(Base):
    """Materialized author-name token index (B7-P43 V2 / U1).

    One row per (paper, byline name, index key); the ``token`` B-tree turns
    ``query_papers(author=...)``'s coarse prefilter into an indexed lookup
    instead of a ``papers.authors LIKE '%token%'`` full-table scan (the only
    super-subsecond query path at 100k, 718-829ms in P39).  ``name_idx``
    scopes the key-equality prefilter to a *single* byline name, so a paper
    whose "Author 3" and "Surname3" appear on different bylines is not a
    false candidate for the token rule.  Keys are produced by
    :func:`_author_index_keys`: the significant tokens of :func:`author_name_matches`
    plus 2-char windows of 3-4-char tokens (CJK/Cyrillic fragments such as
    ``田中`` ⊂ ``田中太郎`` that FTS5's trigram index cannot serve); the
    substring branch is served by the FTS5 trigram table
    ``paper_author_names_fts`` (see
    :meth:`SQLiteStorage._migrate_author_name_index`).
    """

    __tablename__ = "paper_author_tokens"
    __table_args__ = (
        Index("ix_paper_author_tokens_token", "token"),
        Index("ix_paper_author_tokens_paper", "paper_id"),
    )

    paper_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(Text, primary_key=True)


class SourceUpdateRow(Base):
    """Last successful incremental update timestamp per source."""

    __tablename__ = "source_updates"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_update: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EntitySyncRow(Base):
    """Last successful incremental update timestamp per (entity, source).

    Per-entity gating (I-5): a re-pull is decided on the entity dimension, so
    syncing author A never blocks a never-synced author B. ``source_updates``
    is kept untouched for backward compatibility.
    """

    __tablename__ = "entity_sync"

    entity_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AuthorshipRow(Base):
    """Paper ↔ author relationship edge (3A v2 §8.1)."""

    __tablename__ = "authorships"

    paper_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    author_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_corresponding: Mapped[bool] = mapped_column(default=False)
    raw_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    affiliation: Mapped[str | None] = mapped_column(Text, nullable=True)


class CoauthorshipRow(Base):
    """Author ↔ author co-authorship edge (3A v2 §8.1)."""

    __tablename__ = "coauthorships"

    author_a_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    author_b_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paper_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_year: Mapped[int | None] = mapped_column(Integer, nullable=True)


class EvidenceRow(Base):
    """Evidence chain rows — one row per evidence entry (3A v2 §8.1)."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    raw_data: Mapped[Any] = mapped_column(JSON, nullable=True)

class FullTextRow(Base):
    """Full-text paragraphs for a paper (upgrade technical-design §2)."""

    __tablename__ = "full_text"

    paper_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oa_license: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    paragraph_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    segments: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON [{heading,text,page}]
    collected_at: Mapped[str | None] = mapped_column(Text, nullable=True)  # ISO-8601 UTC



class BudgetUsageRow(Base):
    """Per-source budget usage for one period bucket (WP5).

    One row per ``(source, period)`` — the ``period`` string identifies the
    bucket (a UTC day for USD-class budgets such as OpenAlex, or an aligned
    natural window such as ``300s`` for req-class budgets).  ``used`` is the
    locally-estimated accumulated cost/request count in that bucket; rows for
    older periods are kept as history and the current bucket is upserted on
    every consume.  The table is created incrementally by ``create_all`` on
    connect (design §2 / §8: new tables added, existing schema untouched).
    """

    __tablename__ = "budget_usage"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    period: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)


class CrawlCacheRow(Base):
    """One web crawl outcome row (upgrade technical-design.md §2).

    The table replaces the pure in-memory crawl cache as the
    cross-session record: every crawl outcome (``ok`` / ``blocked`` /
    ``failed``) is upserted by :meth:`SQLiteStorage.save_crawl_cache` and
    readable through :meth:`SQLiteStorage.get_crawl_cache`, so web crawl
    status is queryable (design §7 observability).  ``web_doc`` holds the
    serialized :class:`~academic_intelligence.webcrawler.models.WebDocument`
    JSON; ``etag`` / ``body_hash`` are transport-level revalidation
    metadata (may be empty).  The table is created incrementally by
    ``create_all`` on connect, like the other upgrade tables.
    """

    __tablename__ = "crawl_cache"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    fetched_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    web_doc: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuthorIdentityGlobalRow(Base):
    """Global author identity row (upgrade technical-design.md §2, I8).

    The cross-paper identity table: one row per confirmed / auto /
    ambiguous external authority id for a byline name.  ``confirm`` writes
    ``status='confirmed'`` here (plus ``confirmed_by``); a later
    :meth:`Resolver.resolve` for the same name returns the confirmed
    identity directly (cross-paper reuse).  The composite primary key
    ``(author_name, author_id, source)`` keeps the same real-world author
    reachable under each authority system it has an id in.  The table is
    created incrementally by ``create_all`` on connect, like the other
    upgrade tables (lightweight migration — pre-existing tables untouched).
    """

    __tablename__ = "author_identity_global"

    author_name: Mapped[str] = mapped_column(Text, primary_key=True)
    author_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuthorIdentityRow(Base):
    """Paper-level author identity evidence link (technical-design §2, I8).

    One row per ``(paper_id, author_name)`` mapping a byline name in a
    specific paper to the global identity it was confirmed as — the
    traceable paper-level evidence for the global table.  The foreign key
    targets :class:`AuthorIdentityGlobalRow` so a paper-level link can only
    reference an identity that exists globally; the child column order
    matches the parent composite key order (SQLite enforces order-sensitive
    composite foreign keys — the design DDL's ``(author_id, author_name,
    source)`` column order was corrected here so the constraint is actually
    enforceable).  Created incrementally by ``create_all`` on connect.
    """

    __tablename__ = "author_identity"
    __table_args__ = (
        ForeignKeyConstraint(
            ["author_name", "author_id", "source"],
            [
                "author_identity_global.author_name",
                "author_identity_global.author_id",
                "author_identity_global.source",
            ],
            name="fk_author_identity_global",
        ),
    )

    paper_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    author_name: Mapped[str] = mapped_column(Text, primary_key=True)
    author_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)


def _new_id() -> str:
    return uuid.uuid4().hex


def _evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    return _storage_evidence(evidence).model_dump(mode="json")


def _evidence_from_dict(data: dict[str, Any]) -> Evidence:
    return Evidence.model_validate(data)


def _evidence_to_json_list(evidence_list: list[Evidence]) -> list[dict[str, Any]]:
    return [_storage_evidence(e).model_dump(mode="json") for e in evidence_list]


def _evidence_column_from_row(raw: Any) -> list[Evidence]:
    """Parse the legacy ``evidence`` JSON column.

    New databases store a JSON list of evidence dicts; old databases stored
    a single evidence dict. Both shapes are accepted for backwards
    compatibility (3A v2 §8.2 migration note).
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [Evidence.model_validate(d) for d in raw if isinstance(d, dict)]
    if isinstance(raw, dict):
        return [Evidence.model_validate(raw)]
    return []


# (FIX-P F4 / P3) pydantic-core refuses to serialize dict/list nesting deeper
# than 99 levels: 100+ raises ``ValueError "Circular reference detected
# (depth exceeded)"`` — a genuine nesting-depth limit misreported as a cycle
# (P34 V2.1).  ``raw_data`` beyond the cap is truncated before any
# serialization so saving, batch writes and exports keep working.
RAW_DATA_MAX_DEPTH = 99
_RAW_DATA_TRUNCATION_MARKER = "<truncated: raw_data nesting depth exceeds 99>"


def _raw_data_exceeds_depth(value: Any, max_depth: int) -> bool:
    """Return True when any container in *value* nests deeper than *max_depth*.

    Iterative walk (never recurses into caller data, so a pathologically deep
    payload cannot blow the Python stack).
    """
    if not isinstance(value, (dict, list)):
        return False
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            return True
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return False


def _cap_raw_data_depth(value: Any, max_depth: int = RAW_DATA_MAX_DEPTH) -> Any:
    """Return *value* with containers deeper than *max_depth* replaced by a
    truncation marker; the first *max_depth* levels are preserved as-is.

    The result is a fresh copy — the caller's structure is never mutated.
    Cycles along the current path (which pydantic reports as "Circular
    reference detected (id repeated)") are broken the same way instead of
    looping forever.
    """
    if not isinstance(value, (dict, list)):
        return value
    out: Any = [] if isinstance(value, list) else {}
    path_ids: set[int] = set()
    # stack entries: (source container, output container, depth, entering)
    stack: list[tuple[Any, Any, int, bool]] = [(value, out, 1, True)]
    while stack:
        src, dst, depth, entering = stack.pop()
        if not entering:
            path_ids.discard(id(src))
            continue
        path_ids.add(id(src))
        stack.append((src, dst, depth, False))
        items: Any = (
            list(src.items()) if isinstance(src, dict) else list(enumerate(src))
        )
        for key, item in items:
            if not isinstance(item, (dict, list)):
                if isinstance(dst, list):
                    dst.append(item)
                else:
                    dst[key] = item
                continue
            if depth >= max_depth or id(item) in path_ids:
                if isinstance(dst, list):
                    dst.append(_RAW_DATA_TRUNCATION_MARKER)
                else:
                    dst[key] = _RAW_DATA_TRUNCATION_MARKER
                continue
            child: Any = [] if isinstance(item, list) else {}
            if isinstance(dst, list):
                dst.append(child)
            else:
                dst[key] = child
            stack.append((item, child, depth + 1, True))
    return out


def _storage_evidence(evidence: Evidence) -> Evidence:
    """Return an *evidence* whose ``raw_data`` is safe to serialize.

    (FIX-P F4 / P3) pydantic-core refuses to serialize dict/list nesting
    deeper than ``RAW_DATA_MAX_DEPTH`` levels and misreports it as "Circular
    reference detected" (P34 V2.1); over-deep ``raw_data`` — mostly abnormal
    or hostile payloads — is truncated to the cap (first levels kept, tail
    replaced by a marker) before any ``model_dump`` / JSON-column write, and
    the truncation is surfaced through the log.  Shallow data is returned
    unchanged without copying.
    """
    raw = evidence.raw_data
    if raw is None or not _raw_data_exceeds_depth(raw, RAW_DATA_MAX_DEPTH):
        return evidence
    logger.warning(
        "evidence raw_data (source=%s, source_id=%r) truncated: nesting "
        "deeper than %d levels (pydantic serialization limit)",
        evidence.source.value,
        evidence.source_id,
        RAW_DATA_MAX_DEPTH,
    )
    return evidence.model_copy(update={"raw_data": _cap_raw_data_depth(raw)})


def _paper_to_values(paper: Paper, paper_id: str) -> dict[str, Any]:
    """Column-value dict for a paper row (single-record and bulk paths)."""
    return {
        "id": paper_id,
        "title": paper.title,
        "authors": [a.model_dump(mode="json") for a in paper.authors],
        "year": paper.year,
        "venue": paper.venue,
        "venue_type": paper.venue_type,
        "abstract": paper.abstract,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "pmid": paper.pmid,
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "citations": paper.citations,
        "reference_count": paper.reference_count,
        "keywords": list(paper.keywords),
        "fields_of_study": list(paper.fields_of_study),
        "references": paper.references,
        "citations_list": paper.citations_list,
        "evidence": _evidence_to_json_list(paper.evidence_list),
    }


def _paper_to_row(paper: Paper, paper_id: str) -> PaperRow:
    return PaperRow(**_paper_to_values(paper, paper_id))


def _update_paper_row(row: PaperRow, paper: Paper) -> None:
    """Overwrite a persisted paper row's fields in place (upsert path).

    Shared by ``save_paper`` and ``save_batch`` so batch inserts keep the
    exact same update semantics as single-record saves (C-1).
    """
    row.title = paper.title
    row.authors = [a.model_dump(mode="json") for a in paper.authors]
    row.year = paper.year
    row.venue = paper.venue
    row.venue_type = paper.venue_type
    row.abstract = paper.abstract
    row.doi = paper.doi
    row.arxiv_id = paper.arxiv_id
    row.pmid = paper.pmid
    row.url = paper.url
    row.pdf_url = paper.pdf_url
    row.citations = paper.citations
    row.reference_count = paper.reference_count
    row.keywords = list(paper.keywords)
    row.fields_of_study = list(paper.fields_of_study)
    row.references = paper.references
    row.citations_list = paper.citations_list
    row.evidence = _evidence_to_json_list(paper.evidence_list)


def _update_author_row(row: AuthorRow, author: Author) -> None:
    """Overwrite a persisted author row's fields in place (upsert path)."""
    row.name = author.name
    row.orcid = author.orcid
    row.semantic_scholar_id = author.semantic_scholar_id
    row.openalex_id = author.openalex_id
    row.aliases = list(author.aliases)
    row.disambiguation_status = author.disambiguation_status
    row.coauthors = list(author.coauthors)
    row.venues = list(author.venues)
    row.active_years = list(author.active_years) if author.active_years else None
    row.affiliation = author.affiliation
    row.email = author.email
    row.homepage = author.homepage
    row.h_index = author.h_index
    row.citations = author.citations
    row.interests = list(author.interests)
    row.profile_url = author.profile_url
    row.evidence = _evidence_to_json_list(author.evidence_list)


def _row_to_paper(
    row: PaperRow,
    evidence_list: list[Evidence] | None = None,
) -> Paper:
    evidences = (
        evidence_list
        if evidence_list is not None
        else _evidence_column_from_row(row.evidence)
    )
    return Paper(
        id=row.id,
        title=row.title,
        authors=list(row.authors or []),
        year=row.year,
        venue=row.venue,
        venue_type=row.venue_type,
        abstract=row.abstract,
        doi=row.doi,
        arxiv_id=row.arxiv_id,
        pmid=row.pmid,
        url=row.url,
        pdf_url=row.pdf_url,
        citations=row.citations,
        reference_count=row.reference_count,
        keywords=list(row.keywords or []),
        fields_of_study=list(row.fields_of_study or []),
        references=row.references,
        citations_list=row.citations_list,
        evidence_list=evidences,
    )


def _rebuild_synthetic_paper(paper: Paper) -> Paper:
    """Recompute the composite confidence of a loaded paper (I1).

    The composite confidence (multi-source bonus + field-level adjustments)
    is a derived quantity: the deprecated ``evidence`` alias is excluded from
    serialization and the storage layer only persists per-source
    ``evidence_list`` entries. :meth:`ConfidenceScorer.score_paper` is an
    idempotent pure function, so rebuilding it on the read path keeps
    ``primary_evidence`` identical to the value computed at write time.
    """
    if not paper.evidence_list:
        return paper
    return ConfidenceScorer().score_paper(paper)


def _rebuild_synthetic_author(author: Author) -> Author:
    """Recompute the composite confidence of a loaded author (I1)."""
    if not author.evidence_list:
        return author
    return ConfidenceScorer().score_author(author)


def _author_to_values(author: Author, author_id: str) -> dict[str, Any]:
    """Column-value dict for an author row (single-record and bulk paths)."""
    return {
        "id": author_id,
        "name": author.name,
        "orcid": author.orcid,
        "semantic_scholar_id": author.semantic_scholar_id,
        "openalex_id": author.openalex_id,
        "aliases": list(author.aliases),
        "disambiguation_status": author.disambiguation_status,
        "coauthors": list(author.coauthors),
        "venues": list(author.venues),
        "active_years": list(author.active_years) if author.active_years else None,
        "affiliation": author.affiliation,
        "email": author.email,
        "homepage": author.homepage,
        "h_index": author.h_index,
        "citations": author.citations,
        "interests": list(author.interests),
        "profile_url": author.profile_url,
        "evidence": _evidence_to_json_list(author.evidence_list),
    }


def _author_to_row(author: Author, author_id: str) -> AuthorRow:
    return AuthorRow(**_author_to_values(author, author_id))


def _row_to_author(
    row: AuthorRow,
    evidence_list: list[Evidence] | None = None,
) -> Author:
    evidences = (
        evidence_list
        if evidence_list is not None
        else _evidence_column_from_row(row.evidence)
    )
    return Author(
        id=row.id,
        name=row.name,
        orcid=row.orcid,
        semantic_scholar_id=row.semantic_scholar_id,
        openalex_id=row.openalex_id,
        aliases=list(row.aliases or []),
        disambiguation_status=row.disambiguation_status or "auto",
        coauthors=list(row.coauthors or []),
        venues=list(row.venues or []),
        active_years=list(row.active_years) if row.active_years else None,
        affiliation=row.affiliation,
        email=row.email,
        homepage=row.homepage,
        h_index=row.h_index,
        citations=row.citations,
        interests=list(row.interests or []),
        profile_url=row.profile_url,
        evidence_list=evidences,
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


def _author_identity_global_row_to_dict(
    row: AuthorIdentityGlobalRow,
) -> dict[str, Any]:
    """Serialize one global identity row to its storage-facing dict shape."""
    return {
        "author_name": row.author_name,
        "author_id": row.author_id,
        "source": row.source,
        "status": row.status,
        "confidence": row.confidence,
        "confirmed_by": row.confirmed_by,
    }


def _evidence_row_to_model(row: EvidenceRow) -> Evidence:
    collected = row.collected_at or datetime.now(UTC).replace(tzinfo=None)
    # (FIX-V F2) SQLite DateTime columns carry no timezone, so a stored
    # ``collected_at`` reads back naive while freshly collected evidence is
    # UTC-aware.  Lifting the stored value to UTC keeps merged lists
    # homogeneous — mixing naive and aware datetimes crashed
    # ``ConfidenceScorer``'s ``max(collected_at)`` with a TypeError on the
    # real incremental update path (P40 V-C).
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=UTC)
    return Evidence(
        source=SourceType(row.source),
        source_id=row.source_id,
        source_url=row.source_url or "",
        collected_at=collected,
        confidence=row.confidence,
        raw_data=row.raw_data,
    )


def _evidence_to_row(
    entity_type: str,
    entity_id: str,
    evidence: Evidence,
) -> EvidenceRow:
    return EvidenceRow(
        entity_type=entity_type,
        entity_id=entity_id,
        source=evidence.source.value,
        source_id=evidence.source_id,
        source_url=evidence.source_url,
        collected_at=evidence.collected_at,
        confidence=evidence.confidence,
        raw_data=_storage_evidence(evidence).raw_data,
    )


def _authorship_key(ref: AuthorRef) -> str:
    """Stable author key for an authorship row.

    Resolved authors use their ``author_id``; unresolved references fall back
    to the raw byline name (prefixed) so the row stays unique per paper.
    """
    if ref.author_id:
        return ref.author_id
    return f"~{ref.name}"


async def _replace_evidence_rows(
    session: AsyncSession,
    entity_type: str,
    entity_id: str,
    evidence_list: list[Evidence],
) -> None:
    """Replace the evidence rows of a record inside a session."""
    await session.execute(
        delete(EvidenceRow).where(
            EvidenceRow.entity_type == entity_type,
            EvidenceRow.entity_id == entity_id,
        )
    )
    for evidence in evidence_list:
        session.add(_evidence_to_row(entity_type, entity_id, evidence))


async def _replace_authorships(
    session: AsyncSession,
    paper_id: str,
    authors: list[AuthorRef],
) -> list[AuthorRef]:
    """Rewrite authorship edges for a paper inside a session.

    Args:
        session: Active database session.
        paper_id: Paper record ID.
        authors: Author references (in byline order).

    Returns:
        The resolved, deduplicated author references (in byline order) so the
        caller can reuse the exact refs for coauthorship counting — the
        single-record save path counts pairs against the same resolved ids as
        the edges it writes (FIX-M F1 consistency, FIX-P F1).

    Coauthorship counters are NOT touched here (the old per-pair
    ``session.get`` loop, O(n²) DB round trips — 200 authors measured 66s,
    P34 V2.3).  Callers that need them for brand-new papers apply
    :func:`_apply_coauthorship_deltas` (Python aggregation + one upsert)
    instead.
    """
    await session.execute(
        delete(AuthorshipRow).where(AuthorshipRow.paper_id == paper_id)
    )
    # (FIX-M F1 / M1) Re-key unresolved byline names to their Author record
    # ids so name-only sources (pubmed / arxiv) build real author→paper edges.
    resolved_refs = await _resolve_name_author_refs(session, authors)
    unique_authors = _dedupe_author_refs(resolved_refs)
    for ref in unique_authors:
        session.add(
            AuthorshipRow(
                paper_id=paper_id,
                author_id=_authorship_key(ref),
                position=ref.position,
                is_corresponding=ref.is_corresponding,
                raw_name=ref.name,
                affiliation=ref.affiliation,
            )
        )
    return unique_authors


def _dedupe_author_refs(authors: list[AuthorRef]) -> list[AuthorRef]:
    """Dedupe byline refs by authorship key, keeping the first occurrence.

    (FIX-C F1) Real OpenAlex payloads can repeat the same author id in one
    work's byline, and unresolved bylines can repeat a same-name ref; the
    authorship edge table enforces UNIQUE(paper_id, author_id), so dedupe by
    authorship key keeping the first occurrence (byline order preserved).
    """
    seen_keys: set[str] = set()
    unique: list[AuthorRef] = []
    for ref in authors:
        key = _authorship_key(ref)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(ref)
    return unique


def _author_name_key(name: str) -> str:
    """Case-insensitive key used to match byline names to Author records."""
    return name.strip().lower()


async def _resolve_name_author_refs(
    session: AsyncSession,
    refs: Sequence[AuthorRef],
    name_to_id: dict[str, str] | None = None,
    negative: set[str] | None = None,
) -> list[AuthorRef]:
    """Re-key unresolved (``author_id=None``) byline refs to Author ids.

    (FIX-M F1 / M1) Sources without a stable author id (pubmed, arxiv
    bylines) produce ``AuthorRef`` entries with ``author_id=None``; their
    authorship edge falls back to the ``~name`` pseudo-key, so the ``Author``
    record persisted from ``get_author_profile`` never connects to the papers
    that name wrote — ``expand(author, ["papers"])`` reads storage and finds
    no edges.  Each unresolved ref is matched by exact name (case-insensitive)
    against the in-batch ``name_to_id`` map first and the persisted
    ``authors`` table second; a match re-keys the ref to that Author id.
    Unmatched names keep the ``~name`` fallback.  Same-name-different-person
    conflations are the documented limitation (name-based matching only,
    FIX-M "不做": stronger disambiguation is out of scope).

    (FIX-N F1 / N1) ``negative`` is a caller-owned negative cache: names that
    have already been queried and matched nothing are skipped on later calls,
    so repeated names never re-trigger the DB round trip.  After a query the
    queried-but-missing names are added to the set, which ``save_batch``
    pre-seeds from a single batch-wide query (see
    :func:`_resolve_name_author_refs_batch`).
    """
    name_to_id = {} if name_to_id is None else name_to_id
    negative = set() if negative is None else negative
    unresolved = [ref for ref in refs if not ref.author_id]
    if not unresolved:
        return list(refs)
    missing = {
        _author_name_key(ref.name)
        for ref in unresolved
        if _author_name_key(ref.name) not in name_to_id
        and _author_name_key(ref.name) not in negative
    }
    if missing:
        result = await session.execute(
            select(AuthorRow.id, AuthorRow.name).where(
                func.lower(AuthorRow.name).in_(sorted(missing))
            )
        )
        found: dict[str, str] = {}
        for row_id, row_name in result.all():
            # First match wins when several stored authors share a name.
            found[_author_name_key(str(row_name))] = str(row_id)
            name_to_id.setdefault(_author_name_key(str(row_name)), str(row_id))
        # Names that were queried and matched nothing are remembered so a
        # later call with the same name skips the DB round trip (N1).
        negative.update(missing - found.keys())
    resolved: list[AuthorRef] = []
    for ref in refs:
        if ref.author_id:
            resolved.append(ref)
            continue
        author_id = name_to_id.get(_author_name_key(ref.name))
        if author_id:
            resolved.append(ref.model_copy(update={"author_id": author_id}))
        else:
            resolved.append(ref)
    return resolved


async def _resolve_name_author_refs_batch(
    session: AsyncSession,
    refs_by_paper: dict[str, Sequence[AuthorRef]],
    name_to_id: dict[str, str],
) -> dict[str, list[AuthorRef]]:
    """Resolve name-only bylines for a whole paper batch with one DB query (N1).

    The pre-fix ``save_batch`` loop called :func:`_resolve_name_author_refs`
    once per paper, each call firing an author ``select`` whenever the paper's
    byline had no in-batch match — papers-only name-only batches degraded to
    one round trip per paper (10k papers measured 5.05s on the P31 box / 16s
    on a slower host).  Here every unresolved name across the batch is
    collected up front and matched with a single ``IN`` query; names that
    match nothing are recorded in the negative cache so the per-paper
    resolution below never re-queries.
    """
    unresolved = {
        _author_name_key(ref.name)
        for refs in refs_by_paper.values()
        for ref in refs
        if not ref.author_id and _author_name_key(ref.name) not in name_to_id
    }
    negative: set[str] = set()
    if unresolved:
        result = await session.execute(
            select(AuthorRow.id, AuthorRow.name).where(
                func.lower(AuthorRow.name).in_(sorted(unresolved))
            )
        )
        found: set[str] = set()
        for row_id, row_name in result.all():
            key = _author_name_key(str(row_name))
            found.add(key)
            # First match wins when several stored authors share a name.
            name_to_id.setdefault(key, str(row_id))
        negative = unresolved - found
    return {
        paper_id: await _resolve_name_author_refs(
            session, refs, name_to_id, negative=negative
        )
        for paper_id, refs in refs_by_paper.items()
    }


def _build_upsert(model: Any) -> Any:
    """SQLite ``INSERT ... ON CONFLICT DO UPDATE`` over a mapped table.

    All non-PK columns are overwritten from the incoming row on conflict,
    matching the single-record ORM upsert semantics (C-1).  Reusable across
    sessions: the compiled statement is immutable.
    """
    insert_stmt = sqlite_insert(model)
    pk = [c.name for c in model.__table__.primary_key.columns]
    others = [c.name for c in model.__table__.columns if c.name not in pk]
    return insert_stmt.on_conflict_do_update(
        index_elements=pk,
        set_={name: getattr(insert_stmt.excluded, name) for name in others},
    )


_PAPER_UPSERT = _build_upsert(PaperRow)
_AUTHOR_UPSERT = _build_upsert(AuthorRow)
_COAUTHORSHIP_UPSERT = _build_upsert(CoauthorshipRow)

# (FIX-V F1) Citation upsert keyed on the (citing, cited) pair instead of the
# row PK: a citation edge is a fact about the pair, so re-persisting the same
# pair must update the existing row (evidence refreshed) rather than insert a
# duplicate.  Backed by the ``uq_citations_citing_cited`` unique index created
# in :meth:`SQLiteStorage._migrate_citation_index` (P40 V-A: repeat persist
# grew citations 3→6 and 2→10).
_CITATION_PAIR_UPSERT = sqlite_insert(CitationRow).on_conflict_do_update(
    index_elements=["citing_paper_id", "cited_paper_id"],
    set_={"evidence": sqlite_insert(CitationRow).excluded.evidence},
)


_FULLTEXT_UPSERT = _build_upsert(FullTextRow)


def _evidence_to_row_values(
    entity_type: str,
    entity_id: str,
    evidence: Evidence,
) -> dict[str, Any]:
    """Column-value dict for a bulk evidence insert (FIX-G F1)."""
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source": evidence.source.value,
        "source_id": evidence.source_id,
        "source_url": evidence.source_url,
        "collected_at": evidence.collected_at,
        "confidence": evidence.confidence,
        "raw_data": _storage_evidence(evidence).raw_data,
    }


def _authorship_to_values(paper_id: str, ref: AuthorRef) -> dict[str, Any]:
    """Column-value dict for a bulk authorship insert (FIX-G F1)."""
    return {
        "paper_id": paper_id,
        "author_id": _authorship_key(ref),
        "position": ref.position,
        "is_corresponding": ref.is_corresponding,
        "raw_name": ref.name,
        "affiliation": ref.affiliation,
    }


def _author_index_keys(name: str) -> set[str]:
    """Index keys for one author byline name (B7-P43 V2).

    Returns the significant tokens (serving the token-equality branch of
    :func:`author_name_matches`) plus the 2-char windows of 3-4-char tokens.
    The windows serve the substring branch for the CJK/Cyrillic fragments
    that FTS5's trigram index cannot serve ("田中" ⊂ "田中太郎", "Ив" ⊂
    "Иван" — a 2-char trigram LIKE is a full scan), while longer substrings
    are handled by the indexed trigram LIKE.  The same function builds the
    query-side key set (applied to each query token), so the prefilter stays
    a superset of :func:`author_name_matches` on both branches.
    """
    keys: set[str] = set()
    for token in normalize_author_tokens(name):
        keys.add(token)
        if 3 <= len(token) <= 4:
            for i in range(len(token) - 1):
                keys.add(token[i : i + 2])
    return keys


def _paper_author_tokens_values(
    paper_id: str, authors: Sequence[AuthorRef]
) -> list[dict[str, Any]]:
    """Author-name index rows for a paper (B7-P43 V2).

    One row per (byline position, index key) — see :func:`_author_index_keys`
    — deduplicated on the composite primary key so a paper carrying two
    "Bob Alice" bylines still yields a single (paper_id, name_idx, key) row
    per key.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for name_idx, ref in enumerate(authors):
        name = ref.name
        if not name:
            continue
        for key in _author_index_keys(name):
            pair = (paper_id, name_idx, key)
            if pair in seen:
                continue
            seen.add(pair)
            rows.append(
                {"paper_id": paper_id, "name_idx": name_idx, "token": key}
            )
    return rows


def _paper_author_names_values(
    paper_id: str, authors: Sequence[AuthorRef]
) -> list[dict[str, str]]:
    """Folded byline names for the FTS substring index (B7-P43 V2).

    Names are NFC-normalized and lowercased so the trigram LIKE prefilter is
    a superset of :func:`author_name_matches`' substring branch
    (``query in stored``) for every casing / decomposition variant — the
    query side is folded identically in ``query_papers``.
    """
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in authors:
        name = ref.name
        if not name:
            continue
        folded = normalize_nfc(str(name)).lower().strip()
        if not folded:
            continue
        key = (paper_id, folded)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"paper_id": paper_id, "name": folded})
    return rows


def _paper_row_columns() -> tuple[str, ...]:
    """Paper column names derived from the ORM model (FIX-AB-4).

    The FTS keyword join is expressed in raw SQL (SQLAlchemy cannot
    ORM-join a virtual table), so the paper column list is derived from
    ``PaperRow`` metadata instead of being hardcoded — a schema change
    updates the join automatically.
    """
    return tuple(c.name for c in PaperRow.__table__.columns)


_PAPER_ROW_COLUMNS = _paper_row_columns()
_PAPER_JSON_COLUMNS = frozenset(
    c.name for c in PaperRow.__table__.columns if isinstance(c.type, JSON)
)


def _paper_row_from_mapping(mapping: Any) -> PaperRow:
    """Build a :class:`PaperRow` from a raw-SQL result mapping (FIX-AB-4).

    Raw ``text()`` queries return JSON columns as JSON *text strings* (only
    the ORM parses them), so the JSON columns are decoded here; everything
    else maps directly.
    """
    values: dict[str, Any] = dict(mapping)
    for name in _PAPER_JSON_COLUMNS:
        raw = values.get(name)
        if isinstance(raw, str):
            values[name] = json.loads(raw)
    return PaperRow(**values)


async def _replace_paper_author_index(
    session: AsyncSession,
    paper_id: str,
    authors: Sequence[AuthorRef],
    *,
    fts5_available: bool = True,
) -> None:
    """Rewrite a paper's author-name index rows (token + FTS) in a session.

    Shared by ``save_paper`` / ``update_paper`` so both keep the materialized
    index in lockstep with the persisted ``papers.authors`` JSON (B7-P43 V2).
    The FTS half is skipped on SQLite builds without FTS5 (the query path
    then falls back to the raw-JSON LIKE prefilter).
    """
    await session.execute(
        delete(PaperAuthorTokenRow).where(PaperAuthorTokenRow.paper_id == paper_id)
    )
    token_rows = _paper_author_tokens_values(paper_id, authors)
    if token_rows:
        await session.execute(insert(PaperAuthorTokenRow), token_rows)
    if fts5_available:
        await session.execute(
            text(f"DELETE FROM {_AUTHOR_FTS_TABLE} WHERE paper_id = :paper_id"),
            {"paper_id": paper_id},
        )
        name_rows = _paper_author_names_values(paper_id, authors)
        if name_rows:
            await session.execute(
                text(
                    f"INSERT INTO {_AUTHOR_FTS_TABLE} (paper_id, name) "
                    "VALUES (:paper_id, :name)"
                ),
                name_rows,
            )


def _paper_text_values(
    paper_id: str,
    title: str | None,
    abstract: str | None,
) -> dict[str, str] | None:
    """Folded title/abstract row for the paper-text FTS table (FIX-AB-4).

    Text is NFC-normalized and lowercased so the trigram LIKE prefilter is a
    superset of the SQL ``LIKE`` match for every casing / decomposition
    variant — the query side folds identically in ``query_papers`` (the
    final SQL ``ILIKE`` filters still run, so the prefilter only narrows).
    Returns ``None`` when both fields are empty (no row to index).
    """
    folded_title = normalize_nfc(title or "").lower().strip()
    folded_abstract = normalize_nfc(abstract or "").lower().strip()
    if not folded_title and not folded_abstract:
        return None
    return {
        "paper_id": paper_id,
        "title": folded_title,
        "abstract": folded_abstract,
    }


async def _replace_paper_text_index(
    session: AsyncSession,
    paper_id: str,
    title: str | None,
    abstract: str | None,
    *,
    fts5_available: bool = True,
) -> None:
    """Rewrite one paper's title/abstract FTS rows inside a session (FIX-AB-4).

    Shared by ``save_paper`` / ``update_paper`` / ``delete_paper`` so every
    write path keeps ``paper_text_fts`` in lockstep with the persisted
    ``papers`` row.  Skipped on SQLite builds without FTS5 (the keyword
    query path then keeps the plain LIKE scan).
    """
    if not fts5_available:
        return
    await session.execute(
        text(f"DELETE FROM {_PAPER_TEXT_FTS_TABLE} WHERE paper_id = :paper_id"),
        {"paper_id": paper_id},
    )
    row = _paper_text_values(paper_id, title, abstract)
    if row is not None:
        await session.execute(
            text(
                f"INSERT INTO {_PAPER_TEXT_FTS_TABLE} (paper_id, title, abstract) "
                "VALUES (:paper_id, :title, :abstract)"
            ),
            row,
        )


def _citation_to_values(citation: Citation, citation_id: str) -> dict[str, Any]:
    """Column-value dict for a bulk citation insert (FIX-G F1)."""
    return {
        "id": citation_id,
        "citing_paper_id": citation.citing_paper_id,
        "cited_paper_id": citation.cited_paper_id,
        "evidence": _evidence_to_dict(citation.evidence),
    }


def _coauthorship_to_values(
    author_a_id: str,
    author_b_id: str,
    paper_count: int,
    first_year: int | None,
    last_year: int | None,
) -> dict[str, Any]:
    """Column-value dict for a coauthorship row (FIX-G F1)."""
    return {
        "author_a_id": author_a_id,
        "author_b_id": author_b_id,
        "paper_count": paper_count,
        "first_year": first_year,
        "last_year": last_year,
    }


async def _replace_evidence_batch(
    session: AsyncSession,
    entity_type: str,
    entries: list[tuple[str, list[Evidence]]],
) -> None:
    """Replace evidence rows for a set of records in one DELETE + INSERT."""
    if not entries:
        return
    entity_ids = [entity_id for entity_id, _ in entries]
    await session.execute(
        delete(EvidenceRow).where(
            EvidenceRow.entity_type == entity_type,
            EvidenceRow.entity_id.in_(entity_ids),
        )
    )
    rows = [
        _evidence_to_row_values(entity_type, entity_id, evidence)
        for entity_id, evidence_list in entries
        for evidence in evidence_list
    ]
    if rows:
        await session.execute(insert(EvidenceRow), rows)


def _merge_year_window(
    existing: int | None,
    new: int | None,
) -> tuple[int | None, int | None]:
    """Merge a publication year into a coauthorship year window."""
    if new is None:
        return existing, existing
    if existing is None:
        return new, new
    return min(existing, new), max(existing, new)


async def _apply_coauthorship_deltas(
    session: AsyncSession,
    final_papers: dict[str, tuple[Paper, bool]],
    resolved_refs: dict[str, list[AuthorRef]] | None = None,
) -> None:
    """Apply coauthorship counters for brand-new papers (FIX-G F1).

    Mirrors the per-paper ORM logic in :func:`_replace_authorships` with
    ``count_coauthorships=True``: only papers new to storage contribute, and
    each unordered author pair counts once per paper.  Deltas are aggregated
    in Python, existing rows are read in one query, and the merged counters
    are written with a single upsert.

    ``resolved_refs`` (FIX-M F1 / M1) optionally maps a paper id to its
    name-resolved byline (see :func:`_resolve_name_author_refs`); when given,
    it is used instead of the raw ``paper.authors`` so coauthorship edges stay
    consistent with the resolved authorship edges of the same batch.
    """
    deltas: dict[tuple[str, str], tuple[int, int | None, int | None]] = {}
    for paper_id, (paper, count_coauth) in final_papers.items():
        if not count_coauth:
            continue
        refs = (
            resolved_refs[paper_id]
            if resolved_refs is not None and paper_id in resolved_refs
            else paper.authors
        )
        resolved = [
            ref.author_id
            for ref in _dedupe_author_refs(refs)
            if ref.author_id
        ]
        if len(resolved) < 2:
            continue
        for i in range(len(resolved)):
            for j in range(i + 1, len(resolved)):
                author_a, author_b = sorted([str(resolved[i]), str(resolved[j])])
                key = (author_a, author_b)
                count, first, last = deltas.get(key, (0, None, None))
                new_first, new_last = _merge_year_window(first, paper.year)
                deltas[key] = (count + 1, new_first, new_last)
    if not deltas:
        return
    author_as = {key[0] for key in deltas}
    author_bs = {key[1] for key in deltas}
    existing: dict[tuple[str, str], CoauthorshipRow] = {}
    if author_as and author_bs:
        result = await session.execute(
            select(CoauthorshipRow).where(
                CoauthorshipRow.author_a_id.in_(author_as),
                CoauthorshipRow.author_b_id.in_(author_bs),
            )
        )
        existing = {
            (row.author_a_id, row.author_b_id): row
            for row in result.scalars().all()
        }
    rows: list[dict[str, Any]] = []
    for (author_a, author_b), (count, first, last) in deltas.items():
        prev = existing.get((author_a, author_b))
        new_count = (prev.paper_count if prev is not None else 0) + count
        new_first, _ = _merge_year_window(
            prev.first_year if prev is not None else None, first
        )
        _, new_last = _merge_year_window(
            prev.last_year if prev is not None else None, last
        )
        rows.append(
            _coauthorship_to_values(author_a, author_b, new_count, new_first, new_last)
        )
    if rows:
        await session.execute(_COAUTHORSHIP_UPSERT, rows)


async def _paper_authorship_ids(
    session: AsyncSession,
    paper_ids: Sequence[str],
) -> set[str]:
    """Return resolved and unresolved authorship keys for selected papers."""
    if not paper_ids:
        return set()
    result = await session.execute(
        select(AuthorshipRow.author_id).where(AuthorshipRow.paper_id.in_(paper_ids))
    )
    return {str(author_id) for author_id in result.scalars()}


async def _rebuild_coauthorships_for_authors(
    session: AsyncSession,
    affected_author_ids: set[str],
) -> None:
    """Recompute every coauthorship pair touching an affected author.

    Paper updates and deletions can remove an edge, which an additive delta
    cannot represent.  Rebuilding only pairs that touch the old/new byline
    keeps the operation bounded while making the materialized table converge
    exactly to the authoritative ``authorships`` rows.
    """
    affected = {
        author_id
        for author_id in affected_author_ids
        if author_id and not author_id.startswith("~")
    }
    if not affected:
        return

    await session.flush()
    await session.execute(
        delete(CoauthorshipRow).where(
            or_(
                CoauthorshipRow.author_a_id.in_(affected),
                CoauthorshipRow.author_b_id.in_(affected),
            )
        )
    )
    paper_result = await session.execute(
        select(AuthorshipRow.paper_id)
        .where(AuthorshipRow.author_id.in_(affected))
        .distinct()
    )
    paper_ids = [str(paper_id) for paper_id in paper_result.scalars()]
    if not paper_ids:
        return

    edge_result = await session.execute(
        select(PaperRow.id, PaperRow.year, AuthorshipRow.author_id)
        .join(AuthorshipRow, AuthorshipRow.paper_id == PaperRow.id)
        .where(PaperRow.id.in_(paper_ids))
    )
    by_paper: dict[str, tuple[int | None, set[str]]] = {}
    for paper_id, year, author_id in edge_result:
        current_year, authors = by_paper.setdefault(str(paper_id), (year, set()))
        if author_id and not str(author_id).startswith("~"):
            authors.add(str(author_id))
        by_paper[str(paper_id)] = (current_year, authors)

    aggregates: dict[tuple[str, str], tuple[int, int | None, int | None]] = {}
    for year, authors in by_paper.values():
        ordered = sorted(authors)
        for index, author_a in enumerate(ordered):
            for author_b in ordered[index + 1 :]:
                if author_a not in affected and author_b not in affected:
                    continue
                key = (author_a, author_b)
                count, first, last = aggregates.get(key, (0, None, None))
                if year is not None:
                    first = year if first is None else min(first, year)
                    last = year if last is None else max(last, year)
                aggregates[key] = (count + 1, first, last)

    if aggregates:
        await session.execute(
            _COAUTHORSHIP_UPSERT,
            [
                _coauthorship_to_values(author_a, author_b, count, first, last)
                for (author_a, author_b), (count, first, last) in aggregates.items()
            ],
        )


async def _load_evidence_rows(
    session: AsyncSession,
    entity_type: str,
    entity_id: str,
    fallback_raw: Any,
) -> list[Evidence]:
    """Load evidence for a record: evidence table first, legacy column next."""
    stmt = (
        select(EvidenceRow)
        .where(
            EvidenceRow.entity_type == entity_type,
            EvidenceRow.entity_id == entity_id,
        )
        .order_by(EvidenceRow.id)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    if rows:
        return [_evidence_row_to_model(r) for r in rows]
    return _evidence_column_from_row(fallback_raw)


_ReadMethod = TypeVar("_ReadMethod", bound=Callable[..., Awaitable[Any]])


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards in user input (FIX-I F1 / I-1).
    ``%`` and ``_`` in the caller's value are converted to escaped literals
    (``\\%`` / ``\\_``) so the query matches them literally instead of
    treating them as wildcards, e.g. ``100%`` no longer also matches
    ``100x Speedup Report``.  The matching ``ilike(..., escape="\\")`` calls
    declare the backslash escape character.  Backslashes in the input are
    doubled so they are not interpreted as escape characters themselves.

    (FIX-P F2 / P1) NUL (``\\x00``) is rejected outright: SQLite ``LIKE``
    follows C-string semantics and truncates the pattern at the first NUL,
    so ``keyword="\\x00"`` silently degraded to a full-table wildcard
    (P34 V1.4b: 11/11 rows matched).  The other control characters
    (``\\x07``, ``\\x1f``, ...) are ordinary characters to LIKE and keep
    their literal semantics.
    """
    if "\x00" in value:
        raise ValueError("LIKE input must not contain NUL (\\x00) characters")
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _can_encode_utf8(value: str) -> bool:
    """Whether *value* can be bound to SQLite (B7-P43 V2).

    A lone surrogate (``"\\ud800"``) cannot be encoded to UTF-8, so it is
    never pushed into an SQL ``LIKE`` pattern; the author query's token
    branch still runs and the Python exact match keeps the pre-index
    semantics (the surrogate is dropped by token normalization, so a
    ``"Bob\\ud800"`` query still finds "Bob" — matching the pre-index
    behavior).
    """
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _prepare_folded_query(value: str) -> str:
    """Normalize and validate a non-ASCII query string for Python-side folding.

    (FIX-W W2/W3) SQLite LIKE is only ASCII-case-insensitive, so non-ASCII
    queries (Cyrillic/Greek uppercase, decomposed spellings, …) are matched in
    Python instead.  The string is NFC-normalized (W3) and validated with the
    same rules as the SQL LIKE inputs: NUL is rejected outright (P1) and lone
    surrogates surface as a typed :class:`StorageError` via ``_typed_read``
    (P2) instead of silently matching nothing.
    """
    value = normalize_nfc(value)
    if "\x00" in value:
        raise ValueError("LIKE input must not contain NUL (\\x00) characters")
    value.encode("utf-8")
    return value


def _fold_papers(
    papers: Sequence[Paper],
    *,
    keyword: str | None,
    venue: str | None,
) -> list[Paper]:
    """Apply deferred non-ASCII venue/keyword filters in Python (FIX-W W2).

    Mirrors the JSON backend's pure-substring, case-insensitive (``.lower()``)
    matching so both backends agree on non-ASCII queries, which SQLite LIKE
    cannot fold (ASCII-only case insensitivity).
    """
    if venue is not None:
        vl = venue.lower()
        papers = [p for p in papers if p.venue and vl in p.venue.lower()]
    if keyword is not None:
        kl = keyword.lower()
        papers = [
            p
            for p in papers
            if kl in p.title.lower()
            or (p.abstract and kl in p.abstract.lower())
            or any(kl in k.lower() for k in p.keywords)
        ]
    return list(papers)


def _typed_read(method: _ReadMethod) -> _ReadMethod:
    """Wrap a SQLite read in :class:`StorageError` (FIX-H F5 / H6).

    A dropped table (``sqlite3.OperationalError`` / SQLAlchemy's
    ``OperationalError``), a corrupt JSON cell (``json.JSONDecodeError``),
    or a query string that cannot be encoded to UTF-8
    (``UnicodeEncodeError`` — a lone-surrogate keyword such as
    ``"\\ud800"``, FIX-P F3 / P2) must surface as a typed storage error
    carrying a sanitized (basename) db path label in context, not as a raw
    DBAPI/decode exception that callers cannot catch.  The message keeps
    only the diagnostic category — never the full SQL statement
    (FIX-AA-1) — and never the absolute db path (FIX-AA-2).
    """

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await method(self, *args, **kwargs)
        except StorageError:
            raise
        except (
            SqlAlchemyOperationalError,
            sqlite3.OperationalError,
            json.JSONDecodeError,
            UnicodeEncodeError,
        ) as exc:
            raise StorageError(
                f"Failed to read from SQLite storage: {_trim_error_detail(str(exc))}",
                backend="sqlite",
                context={
                    "db_path": _safe_path_label(self.db_path),
                    "detail": _trim_error_detail(str(exc)),
                },
            ) from exc

    return cast(_ReadMethod, wrapper)


class SQLiteStorage(BaseStorage):
    """SQLite-backed storage implementation using SQLAlchemy 2.0 async ORM.

    Concurrency contract (FIX-AE-3 / P50 round-32): the engine is a
    NullPool — every session opens a fresh aiosqlite connection bound to
    the calling event loop and closes it when the session ends — so the
    supported model is *one storage instance per thread / event loop*;
    concurrent processes each run their own instance against the same
    database file, serialized by SQLite's WAL single-writer plus the
    per-connection ``busy_timeout``.  Sharing a single instance across
    threads / loops currently happens to work only because NullPool never
    reuses a connection across sessions; a future pooled engine would break
    it with loop-attachment errors, so it is NOT a contract and must not be
    relied on.
    """

    backend_name: str = "sqlite"

    def __init__(
        self, db_path: str = "./academic_data.db", busy_timeout: float = 10.0
    ) -> None:
        """Initialize SQLite storage.

        Args:
            db_path: Path to SQLite database file.
            busy_timeout: Seconds each new connection waits on a busy
                database (SQLite ``busy_timeout``, passed as aiosqlite's
                ``timeout``) before raising ``database is locked``; the
                write paths then retry transient lock contention (see
                :func:`_retry_busy`).  Lower it for fail-fast,
                low-latency pipelines, raise it for heavy multi-writer
                contention (default 10s, unchanged).
        """
        self.db_path = db_path
        self.busy_timeout = busy_timeout
        self.connection_string = f"sqlite+aiosqlite:///{db_path}"
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        # (B7-P43 V2) Set by :meth:`connect` after probing the SQLite build;
        # False degrades the author-name substring prefilter to the raw-JSON
        # LIKE scan.
        self._fts5_available = True

    def _session(self) -> AsyncSession:
        if self._session_factory is None:
            raise StorageError("Storage not connected", backend=self.backend_name)
        return self._session_factory()

    async def connect(self) -> None:
        """Establish database connection and create tables.

        The engine is a NullPool (forced by the ``sqlite+aiosqlite``
        dialect): every session gets its own connection on the calling
        event loop, which is what makes one-instance-per-thread the
        supported concurrency model (see the class docstring, FIX-AE-3).
        Each connection waits ``busy_timeout`` seconds on a busy database
        before raising ``database is locked``.
        """
        try:
            # (FIX-H F5 / H6) Remember whether the db file pre-existed: a
            # pre-existing file that lost its core tables should be flagged
            # instead of silently recreated as empty tables.
            db_file_existed = os.path.exists(self.db_path)
            # (FIX-AD F2 / AD-2) A leftover ``-wal`` means the previous session
            # did not shut down cleanly (crash / hard kill): commits after the
            # last checkpoint live only in that file, and SQLite silently
            # discards corrupted frames on recovery (P49 round-31 V3.2 lost
            # 21->20 rows with no error).  Warn BEFORE the first connection
            # opens so the on-disk state is what is reported; the connect-time
            # checkpoint below folds the frames into the main database.  A
            # zero-byte ``-wal`` (e.g. left by a TRUNCATE checkpoint) holds no
            # frames, so it is not flagged.
            wal_path = f"{self.db_path}-wal"
            if os.path.exists(wal_path):
                try:
                    wal_size = os.path.getsize(wal_path)
                except OSError:
                    wal_size = -1
                if wal_size > 0:
                    logger.warning(
                        "SQLite WAL journal %s exists: the previous session "
                        "did not shut down cleanly, so commits after the last "
                        "checkpoint may be at risk on crash; connect-time "
                        "checkpoint will fold them into the database",
                        _safe_path_label(wal_path),
                    )
            connection_string = self.connection_string
            if connection_string is None:
                raise StorageError(
                    "SQLite connection string is not configured",
                    backend=self.backend_name,
                )
            self._engine = create_async_engine(
                connection_string,
                echo=False,
                # (FIX-AE F2 / AE-2) SQLite busy_timeout (seconds) on every
                # new connection: aiosqlite passes ``timeout`` to
                # sqlite3.connect.  Configurable via ``busy_timeout`` (and
                # ``Config.sqlite_busy_timeout``), default 10s — unchanged.
                connect_args={"timeout": self.busy_timeout},
            )
            self._session_factory = async_sessionmaker(
                self._engine,
                expire_on_commit=False,
            )
            async with self._engine.begin() as conn:
                # WAL journal mode (I-12): concurrent readers with a single
                # writer without "database is locked" errors. The setting is
                # persistent per database file.  On a brand-new database two
                # processes can race the WAL switch (I-3); the retry below
                # treats the transient "database is locked" as benign.
                await _run_with_transient_retry(
                    lambda: conn.execute(text("PRAGMA journal_mode=WAL"))
                )
                if db_file_existed:
                    table_result = await conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                    existing_tables = {row[0] for row in table_result.fetchall()}
                    if existing_tables and "papers" not in existing_tables:
                        logger.warning(
                            "Existing SQLite database %s is missing core "
                            "tables (found: %s); recreate will rebuild empty "
                            "tables and the previous data is not recoverable",
                            self.db_path,
                            sorted(existing_tables),
                        )
                # (FIX-I F3) Concurrent first-connect race (I-3): two
                # processes can both pass create_all's existence check on a
                # brand-new database and then collide on CREATE TABLE
                # ("table ... already exists").  The loser re-runs create_all
                # (default checkfirst) until it converges with the winner's
                # schema instead of proceeding early — proceeding while the
                # winner is still creating tables would make the migration
                # helpers below race a not-yet-existing table.  Once
                # create_all succeeds, the schema is complete and the
                # migrations are no-ops.
                await _run_with_transient_retry(
                    lambda: conn.run_sync(Base.metadata.create_all)
                )
                await self._migrate_paper_columns(conn)
                await self._migrate_author_columns(conn)
                await self._migrate_citation_index(conn)
                # (B7-P43 V2) Probe FTS5 before the author-name index
                # migration tries to create its trigram virtual table: on a
                # build without FTS5 the substring prefilter degrades to the
                # original ``papers.authors LIKE`` scan (correct, slower) —
                # never an error.
                try:
                    fts5_enabled = bool(
                        (
                            await conn.execute(
                                text(
                                    "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
                                )
                            )
                        ).scalar()
                    )
                    self._fts5_available = fts5_enabled
                except Exception:
                    self._fts5_available = False
                await self._migrate_author_name_index(
                    conn, fts5_available=self._fts5_available
                )
                # (FIX-AB-4) Paper title/abstract FTS table for the keyword
                # query prefilter; created + backfilled exactly like the
                # author-name index above (skipped without FTS5).
                await self._migrate_paper_text_index(
                    conn, fts5_available=self._fts5_available
                )
            # (FIX-AD F2 / AD-2) Fold committed WAL frames into the main
            # database after the schema transaction commits.  A crash then
            # loses at most the frames committed *after* this connect, and a
            # corrupted ``-wal`` (the P49 V3.2 silent-loss case) can no longer
            # swallow pre-existing commits.  Best-effort: a busy/locked
            # checkpoint warns instead of failing connect.
            await self._checkpoint_wal()
        except Exception as exc:
            raise StorageError(
                f"Failed to connect SQLite storage: {_trim_error_detail(str(exc))}",
                backend=self.backend_name,
                context={
                    "db_path": _safe_path_label(self.db_path),
                    "detail": _trim_error_detail(str(exc)),
                },
            ) from exc

    async def _checkpoint_wal(self) -> None:
        """Fold committed WAL frames into the main database (FIX-AD F2 / AD-2).

        Runs on connect so the crash-loss window shrinks to the frames written
        *after* connect: a subsequent crash can discard at most the newest
        frames instead of every un-checkpointed commit since the process
        started (P49 round-31 V3.2: 21->20 rows silently lost).

        ``PRAGMA wal_checkpoint(TRUNCATE)`` must run outside any transaction —
        SQLite refuses a checkpoint from inside a write transaction with
        "database table is locked" — so it executes on a fresh connection as
        its first statement (the ``commit()`` clears the autobegun read
        transaction and is a no-op when none is open).  The checkpoint is
        best-effort: a busy/locked database (another writer holding the WAL)
        warns instead of failing connect; the busy_timeout on new connections
        already absorbs most contention.
        """
        # Only reachable from :meth:`connect`, which assigns ``_engine`` just
        # before calling; guard for the Optional type.
        if self._engine is None:
            return
        try:
            async with self._engine.connect() as conn:
                await conn.commit()
                result = await conn.execute(
                    text("PRAGMA wal_checkpoint(TRUNCATE)")
                )
                row = result.first()
                if row is not None:
                    logger.debug(
                        "WAL checkpoint after connect: busy=%s log=%s "
                        "checkpointed=%s",
                        row[0],
                        row[1],
                        row[2],
                    )
        except Exception as exc:
            logger.warning(
                "WAL checkpoint after connect failed (recent commits may be "
                "at risk on crash): %s",
                _trim_error_detail(str(exc)),
            )

    @staticmethod
    async def _migrate_paper_columns(conn: AsyncConnection) -> None:
        """Bring a pre-existing papers table to the full model column set.

        ``create_all`` only creates missing tables; databases created before
        the current model keep their old schema. Every missing model column
        — base (v1) columns such as ``keywords`` / ``url`` / ``pdf_url`` /
        ``evidence`` as well as the v2 columns (arxiv_id / pmid /
        venue_type / reference_count / fields_of_study / references /
        citations_list) — is added here with ``ALTER TABLE ADD COLUMN``
        (idempotent, I3).  NOT NULL list/status columns are added with a
        server default (FIX-Z Z-2) so ORM-bypassing bare INSERTs do not
        hard-fail with ``IntegrityError``.
        """
        await SQLiteStorage._ensure_table_columns(
            conn,
            "papers",
            required=("id", "title"),
            addable={
                "authors": "JSON NOT NULL DEFAULT '[]'",
                "year": "INTEGER",
                "venue": "TEXT",
                "venue_type": "VARCHAR(32)",
                "abstract": "TEXT",
                "doi": "VARCHAR(255)",
                "arxiv_id": "VARCHAR(255)",
                "pmid": "VARCHAR(64)",
                "url": "TEXT",
                "pdf_url": "TEXT",
                "citations": "INTEGER",
                "reference_count": "INTEGER",
                "keywords": "JSON NOT NULL DEFAULT '[]'",
                "fields_of_study": "JSON NOT NULL DEFAULT '[]'",
                "references": "JSON",
                "citations_list": "JSON",
                "evidence": "JSON NOT NULL DEFAULT '[]'",
            },
        )

    @staticmethod
    async def _migrate_author_columns(conn: AsyncConnection) -> None:
        """Bring a pre-existing authors table to the full model column set.

        Databases created before the current model keep their old schema;
        every missing model column — base (v1) columns (aliases / interests /
        affiliation / email / ...) as well as the v2 identity fields
        (orcid / openalex_id / semantic_scholar_id / disambiguation_status)
        and the disambiguation context fields (coauthors / venues /
        active_years) — is added here with ``ALTER TABLE ADD COLUMN``
        (idempotent).  NOT NULL list/status columns are added with a server
        default (FIX-Z Z-2) so ORM-bypassing bare INSERTs do not hard-fail.
        """
        await SQLiteStorage._ensure_table_columns(
            conn,
            "authors",
            required=("id", "name"),
            addable={
                "orcid": "VARCHAR(255)",
                "semantic_scholar_id": "VARCHAR(255)",
                "openalex_id": "VARCHAR(255)",
                "aliases": "JSON NOT NULL DEFAULT '[]'",
                "disambiguation_status": "VARCHAR(32) NOT NULL DEFAULT 'auto'",
                "coauthors": "JSON NOT NULL DEFAULT '[]'",
                "venues": "JSON NOT NULL DEFAULT '[]'",
                "active_years": "JSON",
                "affiliation": "TEXT",
                "email": "VARCHAR(255)",
                "homepage": "TEXT",
                "h_index": "INTEGER",
                "citations": "INTEGER",
                "interests": "JSON NOT NULL DEFAULT '[]'",
                "profile_url": "TEXT",
                "evidence": "JSON NOT NULL DEFAULT '[]'",
            },
        )

    @staticmethod
    async def _ensure_table_columns(
        conn: AsyncConnection,
        table: str,
        required: Sequence[str],
        addable: dict[str, str],
    ) -> None:
        """Add every missing model column to *table* and validate the schema.

        ``required`` columns are NOT NULL with no meaningful server default
        (``papers.title`` / ``authors.name``), so SQLite cannot synthesize
        them with ``ALTER TABLE ADD COLUMN``.  A table missing one predates
        the base schema (FIX-Z Z-1): connect fails fast with a diagnosable
        "schema too old" error instead of succeeding and then confusing the
        caller with ``no such column`` on the first read.  All other missing
        model columns are added with ``ALTER TABLE ADD COLUMN`` (idempotent).
        """
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        existing = {row[1] for row in result.fetchall()}
        for column in required:
            if column not in existing:
                raise StorageError(
                    f"SQLite schema is too old: the '{table}' table is "
                    f"missing the required column '{column}'. The database "
                    "predates the base schema; recreate it (previous data is "
                    "not recoverable) or migrate it manually.",
                    backend="sqlite",
                )
        for name, ddl_type in addable.items():
            if name not in existing:
                await conn.execute(
                    text(f'ALTER TABLE {table} ADD COLUMN "{name}" {ddl_type}')
                )

    @staticmethod
    async def _migrate_citation_index(conn: AsyncConnection) -> None:
        """Install the ``(citing_paper_id, cited_paper_id)`` unique index.

        (FIX-V F1) Pre-fix databases have no unique constraint on the citation
        pair, so every ``save_batch``/``save_citation`` inserted a fresh row
        and repeat persists grew the table without bound (P40 V-A: a real
        ``collect_citations(..., persist=True)`` ×2 grew 3→6).  The migration
        collapses existing duplicate pairs (keeping the lowest id) and then
        creates the index; ``save_citation``/``save_batch`` upsert on the pair
        so later persists are idempotent.  A concurrent first-connect race
        (I-3) may see ``already exists`` — that is the winner's index, so it
        is treated as success.

        (FIX-Z2 F1) The dedup DELETE keys on ``citations.id``; a table that
        predates even the id column would hit a raw ``no such column: id``
        here.  Like the papers/authors required-column check in
        :meth:`_ensure_table_columns`, that database is flagged "too old"
        instead (its data is not reliable and SQLite cannot add a PRIMARY KEY
        column via ``ALTER TABLE``, so there is no sound auto-repair).
        """
        result = await conn.execute(text("PRAGMA table_info(citations)"))
        existing = {row[1] for row in result.fetchall()}
        if "id" not in existing:
            raise StorageError(
                "SQLite schema is too old: the 'citations' table is "
                "missing the required column 'id'. The database "
                "predates the base schema; recreate it (previous data is "
                "not recoverable) or migrate it manually.",
                backend="sqlite",
            )
        result = await conn.execute(text("PRAGMA index_list('citations')"))
        names = {row[1] for row in result.fetchall()}
        if "uq_citations_citing_cited" in names:
            return
        await conn.execute(
            text(
                "DELETE FROM citations WHERE id NOT IN ("
                "SELECT MIN(id) FROM citations "
                "GROUP BY citing_paper_id, cited_paper_id)"
            )
        )
        try:
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_citations_citing_cited "
                    "ON citations (citing_paper_id, cited_paper_id)"
                )
            )
        except SqlAlchemyOperationalError as exc:
            if "already exists" not in str(exc):
                raise

    @staticmethod
    async def _migrate_author_name_index(
        conn: AsyncConnection,
        *,
        fts5_available: bool = True,
    ) -> None:
        """Create + backfill the author-name query index (B7-P43 V2 / U1).

        Old databases (created before the index existed) get the two index
        artifacts automatically on connect:

        - ``paper_author_tokens`` — created by ``create_all`` via the
          :class:`PaperAuthorTokenRow` model; backfilled from existing
          ``papers.authors`` JSON here.
        - ``paper_author_names_fts`` — an FTS5 trigram virtual table (not a
          declarative model), created with raw DDL and backfilled the same
          way.  Skipped entirely on SQLite builds without FTS5, where
          ``query_papers(author=...)`` keeps the raw-JSON LIKE prefilter.

        Backfill runs only when the FTS table is empty and papers exist, so a
        fresh empty database or an already-migrated one pays nothing.
        """
        fts_backfilled = False
        if fts5_available:
            try:
                await conn.execute(
                    text(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_AUTHOR_FTS_TABLE} "
                        "USING fts5(paper_id UNINDEXED, name, tokenize='trigram')"
                    )
                )
                count = await conn.scalar(
                    text(f"SELECT COUNT(*) FROM {_AUTHOR_FTS_TABLE}")
                )
                fts_backfilled = bool(count)
            except SqlAlchemyOperationalError as exc:
                if "already exists" not in str(exc):
                    raise
        paper_count = await conn.scalar(text("SELECT COUNT(*) FROM papers"))
        if not paper_count:
            return
        # (B7-P43) The index table's schema gained a ``name_idx`` column
        # (per-name key scoping) after the first draft; a database built with
        # the 2-column draft schema is rebuilt — it is a pure index, so a
        # drop + recreate + backfill is safe.
        token_cols = await conn.execute(
            text("PRAGMA table_info(paper_author_tokens)")
        )
        token_col_names = {row[1] for row in token_cols.fetchall()}
        if "name_idx" not in token_col_names:
            await conn.execute(text("DROP TABLE paper_author_tokens"))
            await conn.execute(
                text(
                    "CREATE TABLE paper_author_tokens ("
                    "paper_id VARCHAR(64) NOT NULL, "
                    "name_idx INTEGER NOT NULL, "
                    "token TEXT NOT NULL, "
                    "PRIMARY KEY (paper_id, name_idx, token))"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX ix_paper_author_tokens_token "
                    "ON paper_author_tokens (token)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX ix_paper_author_tokens_paper "
                    "ON paper_author_tokens (paper_id)"
                )
            )
        token_count = await conn.scalar(
            text("SELECT COUNT(*) FROM paper_author_tokens")
        )
        if token_count and (fts_backfilled or not fts5_available):
            return  # already migrated / populated
        # Backfill from existing papers.  ``authors`` comes back from raw SQL
        # as a JSON *text string* (only the ORM parses the JSON column), so it
        # is decoded here; names are folded the same way the write paths fold
        # them so the prefilter stays a superset of author_name_matches.
        fts_rows: list[dict[str, str]] = []
        token_rows: list[dict[str, Any]] = []
        batch_size = 5000
        last_id: str | None = None
        while True:
            stmt = "SELECT id, authors FROM papers"
            params: dict[str, Any] = {}
            if last_id is not None:
                stmt += " WHERE id > :last_id"
                params["last_id"] = last_id
            stmt += " ORDER BY id LIMIT :limit"
            params["limit"] = batch_size
            result = await conn.execute(text(stmt), params)
            rows = result.fetchall()
            if not rows:
                break
            for row_id, authors in rows:
                pid = str(row_id)
                if isinstance(authors, str):
                    try:
                        authors = json.loads(authors)
                    except (json.JSONDecodeError, TypeError):
                        authors = []
                if not isinstance(authors, list):
                    authors = []
                names = [
                    str(a.get("name"))
                    for a in authors
                    if isinstance(a, dict) and a.get("name")
                ]
                for name_idx, name in enumerate(names):
                    # FTS rows are only collected when the FTS table was empty
                    # at the start — when only the token table was rebuilt
                    # (schema change) the existing FTS rows stay valid and
                    # must NOT be re-inserted (that would duplicate them).
                    if fts5_available and not fts_backfilled:
                        folded = normalize_nfc(name).lower().strip()
                        if folded:
                            fts_rows.append({"paper_id": pid, "name": folded})
                    for key in _author_index_keys(name):
                        token_rows.append(
                            {"paper_id": pid, "name_idx": name_idx, "token": key}
                        )
                last_id = pid
            if len(rows) < batch_size:
                break
        if fts_rows:
            await conn.execute(
                text(
                    f"INSERT INTO {_AUTHOR_FTS_TABLE} (paper_id, name) "
                    "VALUES (:paper_id, :name)"
                ),
                fts_rows,
            )
        if token_rows:
            await conn.execute(insert(PaperAuthorTokenRow), token_rows)

    @staticmethod
    async def _migrate_paper_text_index(
        conn: AsyncConnection,
        *,
        fts5_available: bool = True,
    ) -> None:
        """Create + backfill the paper title/abstract FTS table (FIX-AB-4).

        Mirrors :meth:`_migrate_author_name_index`: ``paper_text_fts`` is
        created on connect (SQLite builds with FTS5 only) and backfilled
        from existing ``papers`` rows when the table is empty and papers
        exist, so a fresh empty database or an already-migrated one pays
        nothing.
        """
        fts_backfilled = False
        if fts5_available:
            try:
                await conn.execute(
                    text(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_PAPER_TEXT_FTS_TABLE} "
                        "USING fts5(paper_id UNINDEXED, title, abstract, "
                        "tokenize='trigram')"
                    )
                )
                count = await conn.scalar(
                    text(f"SELECT COUNT(*) FROM {_PAPER_TEXT_FTS_TABLE}")
                )
                fts_backfilled = bool(count)
            except SqlAlchemyOperationalError as exc:
                if "already exists" not in str(exc):
                    raise
        paper_count = await conn.scalar(text("SELECT COUNT(*) FROM papers"))
        if not paper_count or fts_backfilled:
            return
        rows: list[dict[str, str]] = []
        batch_size = 5000
        last_id: str | None = None
        while True:
            stmt = "SELECT id, title, abstract FROM papers"
            params: dict[str, Any] = {}
            if last_id is not None:
                stmt += " WHERE id > :last_id"
                params["last_id"] = last_id
            stmt += " ORDER BY id LIMIT :limit"
            params["limit"] = batch_size
            result = await conn.execute(text(stmt), params)
            batch = result.fetchall()
            if not batch:
                break
            for row_id, title, abstract in batch:
                row = _paper_text_values(
                    str(row_id), str(title or ""), str(abstract or "")
                )
                if row is not None:
                    rows.append(row)
            last_id = str(row_id)
            if len(batch) < batch_size:
                break
        if rows:
            await conn.execute(
                text(
                    f"INSERT INTO {_PAPER_TEXT_FTS_TABLE} (paper_id, title, abstract) "
                    "VALUES (:paper_id, :title, :abstract)"
                ),
                rows,
            )

    async def close(self) -> None:
        """Close database connection and dispose engine."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    async def save_full_text(self, fulltext: FullText) -> None:
        """Upsert a full-text record into the ``full_text`` table.

        Idempotent (C-1): re-saving the same ``paper_id`` updates the row.
        ``segments`` is stored as a JSON text column and ``collected_at`` as
        an ISO-8601 UTC string (upgrade technical-design §2 schema).
        """
        try:
            async with self._session() as session:
                await session.execute(
                    _FULLTEXT_UPSERT,
                    {
                        "paper_id": fulltext.paper_id,
                        "source": fulltext.source,
                        "oa_license": fulltext.oa_license,
                        "file_path": fulltext.file_path,
                        "paragraph_count": fulltext.paragraph_count,
                        "segments": json.dumps(
                            [segment.model_dump(mode="json") for segment in fulltext.segments],
                            ensure_ascii=False,
                        ),
                        "collected_at": fulltext.collected_at.isoformat(),
                    },
                )
                await session.commit()
        except StorageError:
            raise
        except Exception as exc:
            raise _write_storage_error("save full text", exc, self.db_path) from exc

    @_typed_read
    async def get_full_text(self, paper_id: str) -> FullText | None:
        """Return the stored full-text record for *paper_id*, or ``None``."""
        async with self._session() as session:
            row = await session.get(FullTextRow, paper_id)
            if row is None:
                return None
            segments = json.loads(row.segments or "[]")
            return FullText(
                paper_id=row.paper_id,
                source=row.source or "",
                oa_license=row.oa_license,
                file_path=row.file_path,
                paragraph_count=row.paragraph_count or 0,
                segments=[
                    Segment.model_validate(item)
                    for item in segments
                    if isinstance(item, dict)
                ],
                collected_at=(
                    datetime.fromisoformat(row.collected_at)
                    if row.collected_at
                    else datetime.now(UTC)
                ),
            )


    @_retry_busy
    async def save_paper(self, paper: Paper) -> str:
        paper_id = paper.id or _new_id()
        try:
            async with self._session() as session:
                existing = await session.get(PaperRow, paper_id)
                if existing is not None:
                    affected_authors = await _paper_authorship_ids(session, [paper_id])
                    _update_paper_row(existing, paper)
                    resolved = await _replace_authorships(
                        session, paper_id, paper.authors
                    )
                    affected_authors.update(_authorship_key(ref) for ref in resolved)
                    await _rebuild_coauthorships_for_authors(
                        session, affected_authors
                    )
                else:
                    session.add(_paper_to_row(paper, paper_id))
                    resolved = await _replace_authorships(
                        session, paper_id, paper.authors
                    )
                    # (FIX-P F1 / P4) Count coauthorship pairs with the batch
                    # path's Python aggregation (one SELECT + one upsert)
                    # instead of one ``session.get`` per pair: a 200-author
                    # single save dropped from ~66s to ~1s; 1000-author
                    # papers (ATLAS/CMS) no longer take ~25min.
                    await _apply_coauthorship_deltas(
                        session,
                        {paper_id: (paper, True)},
                        resolved_refs={paper_id: resolved},
                    )
                # (B7-P43 V2) Keep the author-name query index in lockstep with
                # the persisted byline (both insert and update branches).
                await _replace_paper_author_index(
                    session,
                    paper_id,
                    paper.authors,
                    fts5_available=self._fts5_available,
                )
                # (FIX-AB-4) Keep the paper title/abstract FTS index in
                # lockstep with the persisted row (both branches).
                await _replace_paper_text_index(
                    session,
                    paper_id,
                    paper.title,
                    paper.abstract,
                    fts5_available=self._fts5_available,
                )
                await _replace_evidence_rows(
                    session, "paper", paper_id, paper.evidence_list
                )
                await session.commit()
            return paper_id
        except StorageError:
            raise
        except Exception as exc:
            raise _write_storage_error("save paper", exc, self.db_path) from exc

    @_typed_read
    async def get_paper(self, paper_id: str) -> Paper | None:
        async with self._session() as session:
            # (FIX-AB-4) Single round trip: the paper row and its evidence
            # rows come back from one outer-join statement instead of
            # ``session.get`` + a second evidence query.  Every async SQL
            # round trip costs ~5-15ms on typical hosts, so halving the
            # trips measurably lowers the read latency.  Semantics are
            # unchanged: evidence-table rows win, and a paper with no
            # evidence rows falls back to the legacy ``papers.evidence``
            # JSON column (same as the old two-query path).
            stmt = (
                select(PaperRow, EvidenceRow)
                .outerjoin(
                    EvidenceRow,
                    and_(
                        EvidenceRow.entity_type == "paper",
                        EvidenceRow.entity_id == PaperRow.id,
                    ),
                )
                .where(PaperRow.id == paper_id)
                .order_by(EvidenceRow.id)
            )
            rows = (await session.execute(stmt)).all()
            if not rows:
                return None
            row = rows[0][0]
            evidence_rows = [r[1] for r in rows if r[1] is not None]
            if evidence_rows:
                evidences = [_evidence_row_to_model(r) for r in evidence_rows]
            else:
                evidences = _evidence_column_from_row(row.evidence)
            return _rebuild_synthetic_paper(
                _row_to_paper(row, evidence_list=evidences)
            )

    @_typed_read
    async def get_paper_by_arxiv_id(self, arxiv_id: str) -> Paper | None:
        """Look up a stored paper by arXiv id.

        Matches the ``papers.arxiv_id`` column by the exact input, the
        version-stripped form, and the stored version-suffixed form
        (``"2403.05525"`` finds a paper stored as ``"2403.05525v2"`` and
        vice versa).  First hit wins, with the same evidence-loaded
        :meth:`get_paper` result.
        """
        normalized = arxiv_id.strip().lower()
        if normalized.startswith("arxiv:"):
            normalized = normalized[len("arxiv:") :].strip()
        base = re.sub(r"v\d+$", "", normalized)
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(PaperRow.id).where(
                        or_(
                            func.lower(PaperRow.arxiv_id) == normalized,
                            func.lower(PaperRow.arxiv_id) == base,
                            func.lower(PaperRow.arxiv_id).like(
                                _escape_like(base) + "v%"
                            ),
                        )
                    )
                )
            ).scalars().all()
        if not rows:
            return None
        return await self.get_paper(str(rows[0]))

    @_retry_busy
    async def update_paper(self, paper_id: str, paper: Paper) -> bool:
        try:
            async with self._session() as session:
                row = await session.get(PaperRow, paper_id)
                if row is None:
                    return False
                affected_authors = await _paper_authorship_ids(session, [paper_id])
                row.title = paper.title
                row.authors = [a.model_dump(mode="json") for a in paper.authors]
                row.year = paper.year
                row.venue = paper.venue
                row.venue_type = paper.venue_type
                row.abstract = paper.abstract
                row.doi = paper.doi
                row.arxiv_id = paper.arxiv_id
                row.pmid = paper.pmid
                row.url = paper.url
                row.pdf_url = paper.pdf_url
                row.citations = paper.citations
                row.reference_count = paper.reference_count
                row.keywords = list(paper.keywords)
                row.fields_of_study = list(paper.fields_of_study)
                row.references = paper.references
                row.citations_list = paper.citations_list
                row.evidence = _evidence_to_json_list(paper.evidence_list)
                resolved = await _replace_authorships(session, paper_id, paper.authors)
                affected_authors.update(_authorship_key(ref) for ref in resolved)
                await _rebuild_coauthorships_for_authors(session, affected_authors)
                # (B7-P43 V2) Keep the author-name query index in lockstep.
                await _replace_paper_author_index(
                    session,
                    paper_id,
                    paper.authors,
                    fts5_available=self._fts5_available,
                )
                # (FIX-AB-4) Keep the paper title/abstract FTS index in lockstep.
                await _replace_paper_text_index(
                    session,
                    paper_id,
                    paper.title,
                    paper.abstract,
                    fts5_available=self._fts5_available,
                )
                await _replace_evidence_rows(
                    session, "paper", paper_id, paper.evidence_list
                )
                await session.commit()
                return True
        except Exception as exc:
            raise _write_storage_error("update paper", exc, self.db_path) from exc

    @_retry_busy
    async def delete_paper(self, paper_id: str) -> bool:
        try:
            async with self._session() as session:
                row = await session.get(PaperRow, paper_id)
                if row is None:
                    return False
                affected_authors = await _paper_authorship_ids(session, [paper_id])
                # (FIX-AF F2 / AF-2) Cascade-clean every row referencing the
                # paper so deletion leaves no orphans (privacy gap): evidence,
                # citation edges (both directions), authorship edges, paper
                # hashes and per-entity sync timestamps.
                await session.execute(
                    delete(EvidenceRow).where(
                        EvidenceRow.entity_type == "paper",
                        EvidenceRow.entity_id == paper_id,
                    )
                )
                await session.execute(
                    delete(CitationRow).where(
                        or_(
                            CitationRow.citing_paper_id == paper_id,
                            CitationRow.cited_paper_id == paper_id,
                        )
                    )
                )
                await session.execute(
                    delete(AuthorshipRow).where(AuthorshipRow.paper_id == paper_id)
                )
                await session.execute(
                    delete(PaperHashRow).where(PaperHashRow.paper_id == paper_id)
                )
                await session.execute(
                    delete(EntitySyncRow).where(
                        EntitySyncRow.entity_type == "paper",
                        EntitySyncRow.entity_id == paper_id,
                    )
                )
                await session.delete(row)
                # (B7-P43 V2) Drop the paper's author-name index rows so deleted
                # papers never surface through ``query_papers(author=...)``.
                await _replace_paper_author_index(
                    session,
                    paper_id,
                    [],
                    fts5_available=self._fts5_available,
                )
                # (FIX-AB-4) Drop the paper's title/abstract FTS rows so deleted
                # papers never surface through ``query_papers(keyword=...)``.
                await _replace_paper_text_index(
                    session,
                    paper_id,
                    "",
                    "",
                    fts5_available=self._fts5_available,
                )
                await _rebuild_coauthorships_for_authors(session, affected_authors)
                await session.commit()
                return True
        except Exception as exc:
            raise _write_storage_error("delete paper", exc, self.db_path) from exc

    @_retry_busy
    async def save_author(self, author: Author) -> str:
        author_id = author.id or _new_id()
        try:
            async with self._session() as session:
                existing = await session.get(AuthorRow, author_id)
                if existing is not None:
                    _update_author_row(existing, author)
                else:
                    session.add(_author_to_row(author, author_id))
                await _replace_evidence_rows(
                    session, "author", author_id, author.evidence_list
                )
                await session.commit()
            return author_id
        except Exception as exc:
            raise _write_storage_error("save author", exc, self.db_path) from exc

    @_typed_read
    async def get_author(self, author_id: str) -> Author | None:
        async with self._session() as session:
            row = await session.get(AuthorRow, author_id)
            if row is None:
                return None
            evidences = await _load_evidence_rows(
                session, "author", author_id, row.evidence
            )
            return _rebuild_synthetic_author(
                _row_to_author(row, evidence_list=evidences)
            )

    @_retry_busy
    async def update_author(self, author_id: str, author: Author) -> bool:
        try:
            async with self._session() as session:
                row = await session.get(AuthorRow, author_id)
                if row is None:
                    return False
                row.name = author.name
                row.orcid = author.orcid
                row.semantic_scholar_id = author.semantic_scholar_id
                row.openalex_id = author.openalex_id
                row.aliases = list(author.aliases)
                row.disambiguation_status = author.disambiguation_status
                row.coauthors = list(author.coauthors)
                row.venues = list(author.venues)
                row.active_years = list(author.active_years) if author.active_years else None
                row.affiliation = author.affiliation
                row.email = author.email
                row.homepage = author.homepage
                row.h_index = author.h_index
                row.citations = author.citations
                row.interests = list(author.interests)
                row.profile_url = author.profile_url
                row.evidence = _evidence_to_json_list(author.evidence_list)
                await _replace_evidence_rows(
                    session, "author", author_id, author.evidence_list
                )
                await session.commit()
                return True
        except Exception as exc:
            raise _write_storage_error("update author", exc, self.db_path) from exc

    @_retry_busy
    async def delete_author(self, author_id: str) -> bool:
        try:
            async with self._session() as session:
                row = await session.get(AuthorRow, author_id)
                if row is None:
                    return False
                # (FIX-AF F2 / AF-2) Cascade-clean every row referencing the
                # author: evidence, authorship edges (in any paper) and
                # coauthorship pairs on either side, plus per-entity sync
                # timestamps.  The papers themselves are untouched.
                await session.execute(
                    delete(EvidenceRow).where(
                        EvidenceRow.entity_type == "author",
                        EvidenceRow.entity_id == author_id,
                    )
                )
                await session.execute(
                    delete(AuthorshipRow).where(AuthorshipRow.author_id == author_id)
                )
                await session.execute(
                    delete(CoauthorshipRow).where(
                        or_(
                            CoauthorshipRow.author_a_id == author_id,
                            CoauthorshipRow.author_b_id == author_id,
                        )
                    )
                )
                await session.execute(
                    delete(EntitySyncRow).where(
                        EntitySyncRow.entity_type == "author",
                        EntitySyncRow.entity_id == author_id,
                    )
                )
                await session.delete(row)
                await session.commit()
                return True
        except Exception as exc:
            raise _write_storage_error("delete author", exc, self.db_path) from exc

    @_retry_busy
    async def save_citation(self, citation: Citation) -> str:
        citation_id = _new_id()
        try:
            async with self._session() as session:
                # (FIX-V F1) Upsert on the (citing, cited) pair: re-saving the
                # same edge refreshes its evidence instead of inserting a
                # duplicate row (P40 V-A).
                stmt = _CITATION_PAIR_UPSERT.values(
                    id=citation_id,
                    citing_paper_id=citation.citing_paper_id,
                    cited_paper_id=citation.cited_paper_id,
                    evidence=_evidence_to_dict(citation.evidence),
                ).returning(CitationRow.id)
                result = await session.execute(stmt)
                persisted_id = str(result.scalar_one())
                await session.commit()
            return persisted_id
        except Exception as exc:
            raise _write_storage_error("save citation", exc, self.db_path) from exc

    @_typed_read
    async def get_citations_by_paper(
        self,
        paper_id: str,
        *,
        direction: str = "outgoing",
    ) -> list[Citation]:
        async with self._session() as session:
            if direction == "incoming":
                stmt = select(CitationRow).where(CitationRow.cited_paper_id == paper_id)
            else:
                stmt = select(CitationRow).where(CitationRow.citing_paper_id == paper_id)
            result = await session.execute(stmt)
            return [_row_to_citation(r) for r in result.scalars().all()]

    # ------------------------------------------------------------------
    # Graph / relationship edges (3A v2 §8)
    # ------------------------------------------------------------------

    @_typed_read
    async def get_references(self, paper_id: str) -> list[str]:
        """Return the IDs of papers cited by *paper_id* (outgoing edges).

        Returns the deduplicated union of the ``citations`` edge table and
        the ``papers.references`` JSON column (FIX-E-2): edges are the
        collected citation relations, the column is the adapter-parsed full
        reference list (where FIX-B1 F2 persists the collected reference
        ids), and the two complement each other.  When the edge table is
        empty the result equals the column, so an expand after ``collect``
        (without citation collection) is served from storage instead of
        re-fetching (D-5).
        """
        async with self._session() as session:
            stmt = (
                select(CitationRow.cited_paper_id)
                .where(CitationRow.citing_paper_id == paper_id)
                .distinct()
            )
            result = await session.execute(stmt)
            refs = list(result.scalars().all())
            row = await session.get(PaperRow, paper_id)
            if row is not None and row.references:
                refs.extend(str(ref) for ref in row.references)
            return list(dict.fromkeys(refs))

    @_typed_read
    async def get_citations(self, paper_id: str) -> list[str]:
        """Return the IDs of papers that cite *paper_id* (incoming edges).

        Deduplicated union of the ``citations`` edge table and the
        ``papers.citations_list`` JSON column (FIX-E-2), mirroring
        :meth:`get_references`.
        """
        async with self._session() as session:
            stmt = (
                select(CitationRow.citing_paper_id)
                .where(CitationRow.cited_paper_id == paper_id)
                .distinct()
            )
            result = await session.execute(stmt)
            citing = list(result.scalars().all())
            row = await session.get(PaperRow, paper_id)
            if row is not None and row.citations_list:
                citing.extend(str(c) for c in row.citations_list)
            return list(dict.fromkeys(citing))

    @_typed_read
    async def get_author_papers(self, author_id: str) -> list[str]:
        """Return the IDs of papers authored by *author_id*."""
        async with self._session() as session:
            stmt = (
                select(AuthorshipRow.paper_id)
                .where(AuthorshipRow.author_id == author_id)
                .distinct()
            )
            result = await session.execute(stmt)
            # Paper IDs never carry the ``~`` pseudo-author prefix; the filter
            # keeps the API free of unresolved-author keys (M3).
            return [pid for pid in result.scalars().all() if not pid.startswith("~")]

    @_typed_read
    async def get_coauthors(self, author_id: str) -> list[str]:
        """Return the IDs of authors that co-authored papers with *author_id*.

        Reads the ``coauthorships`` table directly; falls back to deriving
        the pairs from the ``authorships`` edges when the table is empty
        (e.g. databases written before coauthorship tracking).
        """
        async with self._session() as session:
            stmt_a = (
                select(CoauthorshipRow.author_b_id)
                .where(CoauthorshipRow.author_a_id == author_id)
                .distinct()
            )
            stmt_b = (
                select(CoauthorshipRow.author_a_id)
                .where(CoauthorshipRow.author_b_id == author_id)
                .distinct()
            )
            result_a = await session.execute(stmt_a)
            result_b = await session.execute(stmt_b)
            coauthors = list(result_a.scalars().all()) + list(result_b.scalars().all())
            if coauthors:
                # Drop ``~name`` pseudo-author keys (unresolved byline names
                # are not real authors table primary keys) so graph callers
                # never expand a dead key (M3).
                return [
                    c for c in dict.fromkeys(coauthors) if not c.startswith("~")
                ]
            # Fallback: papers of this author → other authors on those papers.
            paper_stmt = (
                select(AuthorshipRow.paper_id)
                .where(AuthorshipRow.author_id == author_id)
            )
            paper_result = await session.execute(paper_stmt)
            paper_ids = list(paper_result.scalars().all())
            if not paper_ids:
                return []
            from_edges: list[str] = []
            for pid in paper_ids:
                edge_stmt = (
                    select(AuthorshipRow.author_id)
                    .where(
                        AuthorshipRow.paper_id == pid,
                        AuthorshipRow.author_id != author_id,
                    )
                )
                edge_result = await session.execute(edge_stmt)
                from_edges.extend(edge_result.scalars().all())
            # Same ``~`` pseudo-key filter as the table path (M3).
            return [c for c in dict.fromkeys(from_edges) if not c.startswith("~")]

    @_retry_busy
    async def save_evidence(
        self,
        entity_type: str,
        entity_id: str,
        evidence_list: list[Evidence],
    ) -> None:
        """Persist an evidence list for a record (``"paper"`` / ``"author"``)."""
        try:
            async with self._session() as session:
                await _replace_evidence_rows(
                    session, entity_type, entity_id, evidence_list
                )
                await session.commit()
        except Exception as exc:
            raise _write_storage_error("save evidence", exc, self.db_path) from exc

    @_typed_read
    async def get_evidence(
        self,
        entity_type: str,
        entity_id: str,
    ) -> list[Evidence]:
        """Return the persisted evidence list for a record."""
        async with self._session() as session:
            stmt = (
                select(EvidenceRow)
                .where(
                    EvidenceRow.entity_type == entity_type,
                    EvidenceRow.entity_id == entity_id,
                )
                .order_by(EvidenceRow.id)
            )
            result = await session.execute(stmt)
            return [_evidence_row_to_model(r) for r in result.scalars().all()]

    @_retry_busy
    async def save_batch(
        self,
        *,
        authors: list[Author] | None = None,
        papers: list[Paper] | None = None,
        citations: list[Citation] | None = None,
    ) -> dict[str, list[str]]:
        """Persist authors/papers/citations in one transaction (C-1).

        Batch write path (FIX-G F1): paper/author rows are upserted with a
        single SQLite ``INSERT ... ON CONFLICT DO UPDATE`` executemany
        instead of per-record ``session.get`` + ORM upsert; evidence and
        authorship edges are replaced per batch with one DELETE + one bulk
        INSERT; coauthorship counters are aggregated in Python and applied
        with one upsert.  Semantics are unchanged: ids are returned in input
        order, the whole batch commits atomically, and re-saving an existing
        id updates the record in place (idempotent, C-1).
        """
        ids: dict[str, list[str]] = {"authors": [], "papers": [], "citations": []}
        try:
            async with self._session() as session:
                # ---------------------------------------------------------- authors
                author_order: list[str] = []
                final_authors: dict[str, Author] = {}
                for author in authors or []:
                    author_id = author.id or _new_id()
                    final_authors[author_id] = author  # last occurrence wins
                    author_order.append(author_id)
                ids["authors"] = author_order
                if final_authors:
                    await session.execute(
                        _AUTHOR_UPSERT,
                        [
                            _author_to_values(author, author_id)
                            for author_id, author in final_authors.items()
                        ],
                    )
                    await _replace_evidence_batch(
                        session,
                        "author",
                        [
                            (author_id, author.evidence_list)
                            for author_id, author in final_authors.items()
                        ],
                    )

                # ------------------------------------------------------------ papers
                paper_order: list[str] = []
                final_papers: dict[str, tuple[Paper, bool]] = {}
                known_ids: set[str] = set()
                batch_ids = [paper.id for paper in papers or [] if paper.id]
                if batch_ids:
                    result = await session.execute(
                        select(PaperRow.id).where(PaperRow.id.in_(batch_ids))
                    )
                    known_ids = set(result.scalars().all())
                for paper in papers or []:
                    paper_id = paper.id or _new_id()
                    if paper_id in final_papers:
                        # Duplicate inside one batch: the last occurrence's
                        # data wins, the first occurrence's newness decision
                        # (coauthorship counting) stands.
                        final_papers[paper_id] = (
                            paper,
                            final_papers[paper_id][1],
                        )
                    else:
                        final_papers[paper_id] = (
                            paper,
                            paper_id not in known_ids,
                        )
                    paper_order.append(paper_id)
                ids["papers"] = paper_order
                if final_papers:
                    updated_paper_ids = [
                        paper_id
                        for paper_id, (_paper, is_new) in final_papers.items()
                        if not is_new
                    ]
                    affected_updated_authors = await _paper_authorship_ids(
                        session, updated_paper_ids
                    )
                    await session.execute(
                        _PAPER_UPSERT,
                        [
                            _paper_to_values(paper, paper_id)
                            for paper_id, (paper, _count) in final_papers.items()
                        ],
                    )
                    # (FIX-M F1 / M1) Re-key ``~name`` byline refs to the ids
                    # of same-name Author records — in this batch or already in
                    # storage — so author→paper edges exist for sources without
                    # a stable author id (pubmed / arxiv).
                    batch_name_to_id: dict[str, str] = {
                        _author_name_key(author.name): author_id
                        for author_id, author in final_authors.items()
                    }
                    # (FIX-N F1 / N1) Resolve every unresolved byline name with
                    # a single ``authors`` query instead of one per paper: the
                    # pre-fix per-paper loop fired an author ``select`` for
                    # each paper whose byline had no in-batch match, which
                    # degraded papers-only name-only batches to O(papers) round
                    # trips (10k papers: 5.05s on the P31 box).  Names that
                    # were queried and matched nothing land in a negative
                    # cache so the per-paper resolution never re-queries.
                    resolved_by_paper = await _resolve_name_author_refs_batch(
                        session,
                        {
                            paper_id: paper.authors
                            for paper_id, (paper, _count) in final_papers.items()
                        },
                        batch_name_to_id,
                    )
                    await session.execute(
                        delete(AuthorshipRow).where(
                            AuthorshipRow.paper_id.in_(list(final_papers))
                        )
                    )
                    authorship_rows = [
                        _authorship_to_values(paper_id, ref)
                        for paper_id, (paper, _count) in final_papers.items()
                        for ref in _dedupe_author_refs(resolved_by_paper[paper_id])
                    ]
                    if authorship_rows:
                        await session.execute(insert(AuthorshipRow), authorship_rows)
                    await _replace_evidence_batch(
                        session,
                        "paper",
                        [
                            (paper_id, paper.evidence_list)
                            for paper_id, (paper, _count) in final_papers.items()
                        ],
                    )
                    await _apply_coauthorship_deltas(
                        session, final_papers, resolved_refs=resolved_by_paper
                    )
                    for paper_id in updated_paper_ids:
                        affected_updated_authors.update(
                            _authorship_key(ref)
                            for ref in resolved_by_paper[paper_id]
                        )
                    await _rebuild_coauthorships_for_authors(
                        session, affected_updated_authors
                    )
                    # (B7-P43 V2) Rebuild the author-name query index for the
                    # whole batch with one DELETE + one bulk INSERT per
                    # artifact (token rows are deduplicated by the composite
                    # primary key, so an INSERT ... ON CONFLICT is not needed).
                    await session.execute(
                        delete(PaperAuthorTokenRow).where(
                            PaperAuthorTokenRow.paper_id.in_(list(final_papers))
                        )
                    )
                    token_rows = [
                        row
                        for paper_id, (paper, _count) in final_papers.items()
                        for row in _paper_author_tokens_values(
                            paper_id, paper.authors
                        )
                    ]
                    if token_rows:
                        await session.execute(insert(PaperAuthorTokenRow), token_rows)
                    if self._fts5_available:
                        await session.execute(
                            text(
                                f"DELETE FROM {_AUTHOR_FTS_TABLE} "
                                "WHERE paper_id IN :ids"
                            ).bindparams(bindparam("ids", expanding=True)),
                            {"ids": list(final_papers)},
                        )
                        name_rows = [
                            row
                            for paper_id, (paper, _count) in final_papers.items()
                            for row in _paper_author_names_values(
                                paper_id, paper.authors
                            )
                        ]
                        if name_rows:
                            await session.execute(
                                text(
                                    f"INSERT INTO {_AUTHOR_FTS_TABLE} "
                                    "(paper_id, name) VALUES (:paper_id, :name)"
                                ),
                                name_rows,
                            )
                    # (FIX-AB-4) Rebuild the paper title/abstract FTS index for
                    # the whole batch with one DELETE + one bulk INSERT.
                    if self._fts5_available:
                        await session.execute(
                            text(
                                f"DELETE FROM {_PAPER_TEXT_FTS_TABLE} "
                                "WHERE paper_id IN :ids"
                            ).bindparams(bindparam("ids", expanding=True)),
                            {"ids": list(final_papers)},
                        )
                        text_rows = [
                            row
                            for paper_id, (paper, _count) in final_papers.items()
                            for row in [
                                _paper_text_values(
                                    paper_id, paper.title, paper.abstract
                                )
                            ]
                            if row is not None
                        ]
                        if text_rows:
                            await session.execute(
                                text(
                                    f"INSERT INTO {_PAPER_TEXT_FTS_TABLE} "
                                    "(paper_id, title, abstract) "
                                    "VALUES (:paper_id, :title, :abstract)"
                                ),
                                text_rows,
                            )

                # ---------------------------------------------------------- citations
                citation_rows: list[dict[str, Any]] = []
                for citation in citations or []:
                    citation_id = _new_id()
                    citation_rows.append(_citation_to_values(citation, citation_id))
                if citation_rows:
                    # (FIX-V F1) Upsert on the (citing, cited) pair so re-
                    # persisting the same citation batch does not inflate the
                    # table (P40 V-A: 3→6 / 2→10 before the fix).
                    result = await session.execute(
                        _CITATION_PAIR_UPSERT.returning(CitationRow.id),
                        citation_rows,
                    )
                    ids["citations"].extend(str(value) for value in result.scalars())

                await session.commit()
            return ids
        except Exception as exc:
            raise _write_storage_error("save batch", exc, self.db_path) from exc

    @_typed_read
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
        # (FIX-I F4) Reject negative pagination up front so both backends
        # agree: sqlite's LIMIT -1 previously meant "all" while the JSON
        # backend sliced the last row away (I-4).
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be >= 0")
        if order_by not in {"id", "title", "year"}:
            raise ValueError("paper order_by must be one of: id, title, year")
        if after is not None and cursor is not None:
            raise ValueError("specify only one of after or cursor")
        after = after if after is not None else cursor
        # (FIX-W W3) NFC-normalize every free-text query input so a decomposed
        # caller spelling ("Re\\u0301sume\\u0301") hits the composed stored
        # text ("Résumé") and vice versa.
        if author is not None:
            author = normalize_nfc(author)
        if venue is not None:
            venue = normalize_nfc(venue)
        if keyword is not None:
            keyword = normalize_nfc(keyword)
        async with self._session() as session:
            sort_column = {
                "id": PaperRow.id,
                "title": PaperRow.title,
                "year": PaperRow.year,
            }[order_by]
            stmt = select(PaperRow)
            if order_by == "id":
                stmt = stmt.order_by(PaperRow.id)
            else:
                stmt = stmt.order_by(sort_column, PaperRow.id)
            if after is not None:
                cursor_row = await session.get(PaperRow, after)
                if cursor_row is None:
                    raise ValueError(f"paper cursor {after!r} was not found")
                if order_by == "id":
                    stmt = stmt.where(PaperRow.id > after)
                elif order_by == "title":
                    stmt = stmt.where(
                        or_(
                            PaperRow.title > cursor_row.title,
                            and_(
                                PaperRow.title == cursor_row.title,
                                PaperRow.id > after,
                            ),
                        )
                    )
                elif cursor_row.year is None:
                    stmt = stmt.where(
                        or_(
                            PaperRow.year.is_not(None),
                            and_(PaperRow.year.is_(None), PaperRow.id > after),
                        )
                    )
                else:
                    stmt = stmt.where(
                        or_(
                            PaperRow.year > cursor_row.year,
                            and_(PaperRow.year == cursor_row.year, PaperRow.id > after),
                        )
                    )
            if year is not None:
                stmt = stmt.where(PaperRow.year == year)
            if year_from is not None:
                stmt = stmt.where(PaperRow.year >= year_from)
            if year_to is not None:
                stmt = stmt.where(PaperRow.year <= year_to)
            # (FIX-W W2) SQLite LIKE is only ASCII-case-insensitive, so a
            # non-ASCII query (uppercase Cyrillic/Greek, decomposed
            # spellings, …) cannot be folded by the database.  Such queries
            # defer the filter to the Python fold (:func:`_fold_papers`,
            # consistent with the JSON backend's ``.lower()`` matching) and
            # skip the SQL LIMIT so no match falls outside the window.
            deferred_venue: str | None = None
            deferred_keyword: str | None = None
            if venue is not None:
                if venue.isascii():
                    # (FIX-I F1) Escape LIKE wildcards in user input so a
                    # venue such as "100%" matches literally — consistent
                    # with the JSON backend's pure-substring semantics (I-1).
                    stmt = stmt.where(
                        PaperRow.venue.ilike(
                            f"%{_escape_like(venue)}%",
                            escape="\\",
                        )
                    )
                else:
                    deferred_venue = _prepare_folded_query(venue)
            if keyword is not None:
                if keyword.isascii():
                    # (FIX-I F1) Same literal matching for keywords: "100%"
                    # must not act as a wildcard and also match "100x Speedup
                    # Report".
                    pattern = f"%{_escape_like(keyword)}%"
                    keyword_values = text(
                        "EXISTS ("
                        "SELECT 1 FROM json_each(papers.keywords) AS keyword_value "
                        "WHERE CAST(keyword_value.value AS TEXT) "
                        "LIKE :structured_keyword_pattern ESCAPE '\\'"
                        ")"
                    ).bindparams(structured_keyword_pattern=pattern)
                    stmt = stmt.where(
                        or_(
                            PaperRow.title.ilike(pattern, escape="\\"),
                            PaperRow.abstract.ilike(pattern, escape="\\"),
                            keyword_values,
                        )
                    )
                else:
                    deferred_keyword = _prepare_folded_query(keyword)
            if author:
                # (B7-P43 V2) Author filter through the materialized
                # author-name index (U1).  The coarse prefilter is the union of
                # two indexed supersets of :func:`author_name_matches`:
                #
                #  * key equality — every index key of the query (significant
                #    tokens, plus the 2-char windows of its 3-4-char tokens)
                #    appears in ``paper_author_tokens`` for the paper, and
                #  * substring — the folded query is a substring of some
                #    folded byline (FTS5 trigram ``LIKE``; on SQLite builds
                #    without FTS5 the pre-fix raw-JSON ``authors LIKE`` scan
                #    serves the same role, correctly but slowly).
                #
                # A name that matches by either rule is guaranteed to satisfy
                # the prefilter, so all candidate rows are fetched WITHOUT
                # LIMIT and the exact match + pagination happen in Python —
                # applying LIMIT first would silently drop matches beyond the
                # window (G2).  This replaces the FIX-G F1 raw-JSON LIKE scan
                # (the only super-subsecond query path at 100k: 718-829ms in
                # P39) with indexed lookups.
                tokens = normalize_author_tokens(author)
                candidate_ids: set[str] = set()
                if tokens:
                    # Key equality: intersect the per-key (paper, name) sets
                    # starting from the rarest key (per-key COUNTs are cheap
                    # index-range scans; a group-by/Having formulation would
                    # instead walk every row of a ubiquitous key such as
                    # "author" — the degenerate 100k-token case measured at
                    # ~750ms).  The keys are the query's significant tokens
                    # only: 2-char tokens double as windows of stored 3-4-char
                    # tokens (see :func:`_author_index_keys`), and longer
                    # fragment substrings are caught by the FTS branch.  The
                    # intersection is scoped to a single byline name (name_idx)
                    # so a paper whose query tokens land on different bylines
                    # is not a false candidate.
                    query_keys = set(tokens)
                    key_counts: dict[str, int] = {}
                    for key in query_keys:
                        key_counts[key] = int(
                            await session.scalar(
                                select(func.count())
                                .select_from(PaperAuthorTokenRow)
                                .where(PaperAuthorTokenRow.token == key)
                            )
                            or 0
                        )
                    rarest = min(query_keys, key=lambda key: key_counts[key])
                    pairs_result = await session.execute(
                        select(
                            PaperAuthorTokenRow.paper_id,
                            PaperAuthorTokenRow.name_idx,
                        ).where(PaperAuthorTokenRow.token == rarest)
                    )
                    pairs = [
                        (str(row_id), int(name_idx))
                        for row_id, name_idx in pairs_result.all()
                    ]
                    remaining = query_keys - {rarest}
                    if pairs:
                        if remaining:
                            papers_with_rarest = {pid for pid, _ in pairs}
                            # Force the paper_id index: the planner otherwise
                            # picks the token index for a ubiquitous key (e.g.
                            # 'author' -> 250k-row walk measured at ~160ms).
                            verify_result = await session.execute(
                                text(
                                    "SELECT paper_id, name_idx, token FROM "
                                    "paper_author_tokens INDEXED BY "
                                    "ix_paper_author_tokens_paper "
                                    "WHERE paper_id IN :papers AND token IN :tokens"
                                ).bindparams(
                                    bindparam("papers", expanding=True),
                                    bindparam("tokens", expanding=True),
                                ),
                                {
                                    "papers": sorted(papers_with_rarest),
                                    "tokens": sorted(remaining),
                                },
                            )
                            have: dict[tuple[str, int], set[str]] = {}
                            for pid, name_idx, key in verify_result.all():
                                have.setdefault(
                                    (str(pid), int(name_idx)), set()
                                ).add(str(key))
                            candidate_ids = {
                                pid
                                for pid, name_idx in pairs
                                if remaining
                                <= have.get((pid, name_idx), set())
                            }
                        else:
                            candidate_ids = {pid for pid, _ in pairs}
                # Substring branch: ``query in stored name``.  The query is
                # folded (NFC-normalized at the top + lowercased) exactly like
                # :func:`author_name_matches` folds the stored side, and the
                # FTS table stores the same folding, so any substring match is
                # caught.  NUL / lone surrogates (which SQLite cannot bind)
                # skip the substring branch: the key branch still runs and the
                # Python exact match keeps the pre-index semantics.
                folded_query = author.lower().strip()
                if (
                    folded_query
                    and "\x00" not in folded_query
                    and _can_encode_utf8(folded_query)
                ):
                    if self._fts5_available:
                        # (FIX-I F1 / M6) Patterns containing LIKE wildcards
                        # must escape them so "100%" / "a_b" match literally;
                        # patterns without wildcards skip the ESCAPE clause,
                        # which keeps the trigram index usable (an ESCAPE'd
                        # LIKE degrades to a full scan).  Either way the
                        # prefilter stays a superset and the Python exact
                        # match does the final filtering.
                        if any(ch in folded_query for ch in "%_\\"):
                            pattern = f"%{_escape_like(folded_query)}%"
                            fts_sql = (
                                f"SELECT paper_id FROM {_AUTHOR_FTS_TABLE} "
                                "WHERE name LIKE :pattern ESCAPE '\\'"
                            )
                        else:
                            pattern = f"%{folded_query}%"
                            fts_sql = (
                                f"SELECT paper_id FROM {_AUTHOR_FTS_TABLE} "
                                "WHERE name LIKE :pattern"
                            )
                        fts_result = await session.execute(
                            text(fts_sql), {"pattern": pattern}
                        )
                        candidate_ids.update(
                            str(row[0]) for row in fts_result.all()
                        )
                    else:
                        # No-FTS5 fallback: the pre-fix superset prefilter —
                        # every ASCII query token must appear in the authors
                        # JSON text (the ASCII-only SQL LIKE limitation that
                        # motivated FIX-I F2 applies as before).
                        scan = select(PaperRow.id, PaperRow.authors)
                        for token in sorted(tokens):
                            if token.isascii():
                                scan = scan.where(
                                    PaperRow.authors.ilike(
                                        f"%{_escape_like(token)}%", escape="\\"
                                    )
                                )
                        scan_result = await session.execute(scan)
                        for row_id, row_authors in scan_result.all():
                            names = (
                                [
                                    str(name)
                                    for name in (
                                        a.get("name")
                                        for a in row_authors
                                        if isinstance(a, dict)
                                    )
                                    if name
                                ]
                                if row_authors
                                else []
                            )
                            if any(
                                author_name_matches(author, name) for name in names
                            ):
                                candidate_ids.add(str(row_id))
                if not candidate_ids:
                    return []
                # Phase 1: fetch only (id, authors) for the candidate set in
                # insertion order (FIX-W W6) and run the exact
                # :func:`author_name_matches` filter — the prefilter is a
                # superset, so most candidates are rejected here without
                # paying for full-row reconstruction or confidence scoring.
                id_result = await session.execute(
                    select(PaperRow.id, PaperRow.authors)
                    .where(PaperRow.id.in_(candidate_ids))
                    .order_by(text("rowid"))
                )
                matched_ids: list[str] = []
                for row_id, row_authors in id_result.all():
                    names = (
                        [
                            str(name)
                            for name in (
                                a.get("name")
                                for a in row_authors
                                if isinstance(a, dict)
                            )
                            if name
                        ]
                        if row_authors
                        else []
                    )
                    if any(author_name_matches(author, name) for name in names):
                        matched_ids.append(str(row_id))
                if not matched_ids:
                    return []
                # Phase 2: fetch the full matched rows (the year/venue/keyword
                # SQL filters on *stmt* still apply), rebuild synthetic
                # confidence, fold deferred filters, and paginate in Python.
                stmt = stmt.where(PaperRow.id.in_(matched_ids))
                result = await session.execute(stmt)
                papers = [
                    _rebuild_synthetic_paper(_row_to_paper(row))
                    for row in result.scalars().all()
                ]
                papers = _fold_papers(
                    papers, keyword=deferred_keyword, venue=deferred_venue
                )
                return papers[offset : offset + limit]

            if deferred_keyword is not None or deferred_venue is not None:
                # (FIX-W W2) Deferred non-ASCII filters: fetch every candidate
                # row (other SQL filters still apply) and fold in Python —
                # applying LIMIT before the fold would drop matches (G2).
                result = await session.execute(stmt)
                rows = list(result.scalars().all())
                papers = [_rebuild_synthetic_paper(_row_to_paper(r)) for r in rows]
                papers = _fold_papers(
                    papers, keyword=deferred_keyword, venue=deferred_venue
                )
                return papers[offset : offset + limit]

            # (FIX-AB-4) ASCII keyword queries are served through the FTS5
            # trigram paper-text index when it is available and no other
            # deferred / author filters are in play.  The FTS table stores
            # NFC-folded lowercase title/abstract, so its trigram ``LIKE`` is
            # a superset of the SQL ``LIKE`` match for every ASCII casing
            # variant; the join re-applies the same year / venue conditions
            # and the same ``rowid`` insertion order, keeping the exact
            # pre-index semantics in a single round trip (every async SQL
            # round trip costs ~5-15ms, so adding a probe query would eat
            # the win).  A keyword matching a large share of the corpus is
            # the documented degenerate case: the trigram walk + sort then
            # costs ~2x the plain scan (measured), which is the indexed
            # substring search's trade-off.
            if (
                keyword is not None
                and keyword.isascii()
                and self._fts5_available
                and author is None
                and deferred_venue is None
                and after is None
                and order_by == "id"
            ):
                quoted_cols = ", ".join(f'p."{name}"' for name in _PAPER_ROW_COLUMNS)
                if any(ch in keyword for ch in "%_\\"):
                    # (FIX-I F1) Literal wildcards: ESCAPE'd LIKE on the FTS
                    # table (trigram unusable → full FTS scan, correct).
                    fts_where = (
                        "f.title LIKE :pattern ESCAPE '\\' "
                        "OR f.abstract LIKE :pattern ESCAPE '\\' "
                        "OR EXISTS (SELECT 1 FROM json_each(p.keywords) kw "
                        "WHERE CAST(kw.value AS TEXT) LIKE :pattern ESCAPE '\\')"
                    )
                else:
                    fts_where = (
                        "f.title LIKE :pattern OR f.abstract LIKE :pattern "
                        "OR EXISTS (SELECT 1 FROM json_each(p.keywords) kw "
                        "WHERE CAST(kw.value AS TEXT) LIKE :pattern)"
                    )
                sql = (
                    f"SELECT {quoted_cols} FROM {_PAPER_TEXT_FTS_TABLE} f "
                    f"JOIN papers p ON p.id = f.paper_id WHERE ({fts_where})"
                )
                params: dict[str, Any] = {"pattern": pattern}
                if year is not None:
                    sql += " AND p.year = :year"
                    params["year"] = year
                if year_from is not None:
                    sql += " AND p.year >= :year_from"
                    params["year_from"] = year_from
                if year_to is not None:
                    sql += " AND p.year <= :year_to"
                    params["year_to"] = year_to
                if venue is not None:
                    # ASCII venue (non-ASCII defers above); the B-tree scan
                    # of the narrowed candidate set is fine.
                    venue_pat = f"%{_escape_like(venue)}%"
                    sql += " AND p.venue LIKE :venue_pat ESCAPE '\\'"
                    params["venue_pat"] = venue_pat
                sql += " ORDER BY p.id LIMIT :limit OFFSET :offset"
                params["limit"] = limit
                params["offset"] = offset
                result = await session.execute(text(sql), params)
                papers = [
                    _rebuild_synthetic_paper(_row_to_paper(_paper_row_from_mapping(m)))
                    for m in result.mappings()
                ]
                return papers

            stmt = stmt.offset(offset).limit(limit)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

            papers = [_rebuild_synthetic_paper(_row_to_paper(r)) for r in rows]
            return papers

    @_typed_read
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
        # (FIX-I F4) Negative pagination rejected (see query_papers).
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be >= 0")
        if order_by not in {"id", "name"}:
            raise ValueError("author order_by must be one of: id, name")
        if after is not None and cursor is not None:
            raise ValueError("specify only one of after or cursor")
        after = after if after is not None else cursor
        # (FIX-W W3) NFC-normalize free-text inputs (see query_papers).
        if name is not None:
            name = normalize_nfc(name)
        if affiliation is not None:
            affiliation = normalize_nfc(affiliation)
        if interest is not None:
            interest = normalize_nfc(interest)
        async with self._session() as session:
            stmt = select(AuthorRow)
            if order_by == "id":
                stmt = stmt.order_by(AuthorRow.id)
            else:
                stmt = stmt.order_by(AuthorRow.name, AuthorRow.id)
            if after is not None:
                cursor_row = await session.get(AuthorRow, after)
                if cursor_row is None:
                    raise ValueError(f"author cursor {after!r} was not found")
                if order_by == "id":
                    stmt = stmt.where(AuthorRow.id > after)
                else:
                    stmt = stmt.where(
                        or_(
                            AuthorRow.name > cursor_row.name,
                            and_(
                                AuthorRow.name == cursor_row.name,
                                AuthorRow.id > after,
                            ),
                        )
                    )
            # (FIX-W W2) Non-ASCII name/affiliation queries cannot be folded
            # by SQLite LIKE (ASCII-only), so they defer to the Python fold.
            deferred_name: str | None = None
            deferred_affiliation: str | None = None
            if name is not None:
                # (FIX-W W1) Escape LIKE wildcards so ``name='%'`` matches a
                # literal ``%`` instead of every author — the one gap FIX-I
                # F1 left open (it covered query_papers only).
                if name.isascii():
                    stmt = stmt.where(
                        AuthorRow.name.ilike(f"%{_escape_like(name)}%", escape="\\")
                    )
                else:
                    deferred_name = _prepare_folded_query(name)
            if affiliation is not None:
                # (FIX-W W1) Same literal matching for affiliation.
                if affiliation.isascii():
                    stmt = stmt.where(
                        AuthorRow.affiliation.ilike(
                            f"%{_escape_like(affiliation)}%", escape="\\"
                        )
                    )
                else:
                    deferred_affiliation = _prepare_folded_query(affiliation)
            if (
                deferred_name is not None
                or deferred_affiliation is not None
                or interest is not None
            ):
                result = await session.execute(stmt)
                authors = [
                    _rebuild_synthetic_author(_row_to_author(r))
                    for r in result.scalars().all()
                ]
                if deferred_name is not None:
                    nl = deferred_name.lower()
                    authors = [a for a in authors if nl in a.name.lower()]
                if deferred_affiliation is not None:
                    al = deferred_affiliation.lower()
                    authors = [
                        a
                        for a in authors
                        if a.affiliation and al in a.affiliation.lower()
                    ]
            else:
                stmt = stmt.offset(offset).limit(limit)
                result = await session.execute(stmt)
                authors = [
                    _rebuild_synthetic_author(_row_to_author(r))
                    for r in result.scalars().all()
                ]
            if interest:
                interest_lower = interest.casefold()
                authors = [
                    a
                    for a in authors
                    if any(
                        interest_lower in normalize_nfc(i).casefold()
                        for i in a.interests
                    )
                ]
            if (
                deferred_name is not None
                or deferred_affiliation is not None
                or interest is not None
            ):
                authors = authors[offset : offset + limit]
            return authors

    @_typed_read
    async def get_stats(self) -> dict[str, Any]:
        async with self._session() as session:
            papers = await session.scalar(select(func.count()).select_from(PaperRow))
            authors = await session.scalar(select(func.count()).select_from(AuthorRow))
            citations = await session.scalar(select(func.count()).select_from(CitationRow))
            return {
                "total_papers": int(papers or 0),
                "total_authors": int(authors or 0),
                "total_citations": int(citations or 0),
                "backend": self.backend_name,
                "db_path": _safe_path_label(self.db_path),
            }

    # ------------------------------------------------------------------
    # Incremental update metadata
    # ------------------------------------------------------------------

    @_typed_read
    async def get_paper_hash(self, paper_id: str) -> str | None:
        async with self._session() as session:
            row = await session.get(PaperHashRow, paper_id)
            return row.content_hash if row else None

    @_retry_busy
    async def save_paper_hash(self, paper_id: str, hash: str) -> None:
        try:
            async with self._session() as session:
                existing = await session.get(PaperHashRow, paper_id)
                now = datetime.now(UTC).replace(tzinfo=None)
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
            raise _write_storage_error(
                "save paper hash", exc, self.db_path
            ) from exc

    @_typed_read
    async def get_last_update_time(self, source: str) -> datetime | None:
        async with self._session() as session:
            row = await session.get(SourceUpdateRow, source)
            return row.last_update if row else None

    @_retry_busy
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
            raise _write_storage_error(
                "save last update time", exc, self.db_path
            ) from exc

    @_typed_read
    async def get_entity_sync(
        self,
        entity_type: str,
        entity_id: str,
        source: str,
    ) -> datetime | None:
        async with self._session() as session:
            row = await session.get(
                EntitySyncRow, (entity_type, entity_id, source)
            )
            return row.last_synced_at if row else None

    @_retry_busy
    async def save_entity_sync(
        self,
        entity_type: str,
        entity_id: str,
        source: str,
        time: datetime,
    ) -> None:
        try:
            async with self._session() as session:
                existing = await session.get(
                    EntitySyncRow, (entity_type, entity_id, source)
                )
                # Store as naive UTC for SQLite DateTime compatibility
                stored = time.replace(tzinfo=None) if time.tzinfo else time
                if existing is not None:
                    existing.last_synced_at = stored
                else:
                    session.add(
                        EntitySyncRow(
                            entity_type=entity_type,
                            entity_id=entity_id,
                            source=source,
                            last_synced_at=stored,
                        )
                    )
                await session.commit()
        except Exception as exc:
            raise _write_storage_error(
                "save entity sync time", exc, self.db_path
            ) from exc

    # ------------------------------------------------------------------
    # Budget usage (WP5: budget_usage table)
    # ------------------------------------------------------------------

    @_typed_read
    async def get_budget_usage(self, source: str, period: str) -> float | None:
        """Return the recorded usage for a ``(source, period)`` bucket.

        Args:
            source: Source identifier (e.g. ``"openalex"``).
            period: Period bucket key (UTC day or aligned window, see
                :mod:`academic_intelligence.budget`).

        Returns:
            The accumulated ``used`` value, or ``None`` when the bucket has
            never been consumed (the caller treats missing as zero usage).
        """
        async with self._session() as session:
            row = await session.get(BudgetUsageRow, (source, period))
            return row.used if row else None

    @_retry_busy
    async def save_budget_usage(
        self, source: str, period: str, used: float, unit: str
    ) -> None:
        """Upsert the usage of a ``(source, period)`` budget bucket.

        Args:
            source: Source identifier.
            period: Period bucket key.
            used: Accumulated usage (requests or estimated USD/credit).
            unit: Usage unit (e.g. ``"req"`` or ``"usd"``).
        """
        try:
            async with self._session() as session:
                existing = await session.get(BudgetUsageRow, (source, period))
                if existing is not None:
                    existing.used = used
                    existing.unit = unit
                else:
                    session.add(
                        BudgetUsageRow(
                            source=source,
                            period=period,
                            used=used,
                            unit=unit,
                        )
                    )
                await session.commit()
        except Exception as exc:
            raise _write_storage_error(
                "save budget usage", exc, self.db_path
            ) from exc

    # ------------------------------------------------------------------
    # Web crawl cache (IM-5: crawl_cache table, technical-design §2)
    # ------------------------------------------------------------------

    @_typed_read
    async def get_crawl_cache(self, url: str) -> CrawlCacheRecord | None:
        """Return the persisted crawl-cache row for *url*, or ``None``.

        Args:
            url: The crawled URL (primary key of the ``crawl_cache`` row).

        Returns:
            A :class:`CrawlCacheRecord` with the row's status / fetch time /
            etag / body hash and the serialized :class:`WebDocument` dict,
            or ``None`` when the URL was never crawled (or its JSON cell is
            corrupt — the row is still returned, with ``web_doc=None``, so
            the status stays queryable).
        """
        async with self._session() as session:
            row = await session.get(CrawlCacheRow, url)
            if row is None:
                return None
            web_doc: dict[str, Any] | None = None
            if row.web_doc:
                try:
                    web_doc = json.loads(row.web_doc)
                except ValueError:
                    web_doc = None
            return CrawlCacheRecord(
                url=row.url,
                status=row.status or "",
                fetched_at=row.fetched_at,
                etag=row.etag,
                body_hash=row.body_hash,
                web_doc=web_doc,
            )

    @_retry_busy
    async def save_crawl_cache(self, record: CrawlCacheRecord) -> None:
        """Upsert a web crawl outcome into the ``crawl_cache`` table.

        Args:
            record: The crawl outcome to persist (keyed by ``record.url``);
                re-saving the same URL overwrites the previous row.
        """
        try:
            async with self._session() as session:
                existing = await session.get(CrawlCacheRow, record.url)
                web_doc = json.dumps(record.web_doc) if record.web_doc is not None else None
                if existing is not None:
                    existing.status = record.status
                    existing.fetched_at = record.fetched_at
                    existing.etag = record.etag
                    existing.body_hash = record.body_hash
                    existing.web_doc = web_doc
                else:
                    session.add(
                        CrawlCacheRow(
                            url=record.url,
                            status=record.status,
                            fetched_at=record.fetched_at,
                            etag=record.etag,
                            body_hash=record.body_hash,
                            web_doc=web_doc,
                        )
                    )
                await session.commit()
        except Exception as exc:
            raise _write_storage_error(
                "save crawl cache", exc, self.db_path
            ) from exc

    # ------------------------------------------------------------------
    # Author identity (WP6: author_identity_global + author_identity)
    # ------------------------------------------------------------------

    @_retry_busy
    async def save_author_identity_global(
        self,
        *,
        author_name: str,
        author_id: str,
        source: str,
        status: str,
        confidence: float | None = None,
        confirmed_by: str | None = None,
    ) -> None:
        """Upsert one row of the cross-paper identity table (I8).

        Args:
            author_name: Byline name the identity is attached to.
            author_id: External authority id (OpenAlex ``A...`` / S2 id /
                ORCID).
            source: Authority system (``"openalex"`` / ``"s2"`` /
                ``"orcid"``).
            status: ``"confirmed"`` / ``"auto"`` / ``"ambiguous"``.
            confidence: Optional disambiguation confidence (0..1).
            confirmed_by: Who/what confirmed the identity (free text, e.g.
                ``"cli"`` or a test label).

        ``confirm`` writes ``status="confirmed"`` here; re-confirming the
        same ``(author_name, author_id, source)`` overwrites in place
        (idempotent upsert, same as the other upgrade tables).
        """
        try:
            async with self._session() as session:
                existing = await session.get(
                    AuthorIdentityGlobalRow,
                    (author_name, author_id, source),
                )
                if existing is not None:
                    existing.status = status
                    existing.confidence = confidence
                    existing.confirmed_by = confirmed_by
                else:
                    session.add(
                        AuthorIdentityGlobalRow(
                            author_name=author_name,
                            author_id=author_id,
                            source=source,
                            status=status,
                            confidence=confidence,
                            confirmed_by=confirmed_by,
                        )
                    )
                await session.commit()
        except Exception as exc:
            raise _write_storage_error(
                "save author identity global", exc, self.db_path
            ) from exc

    @_typed_read
    async def get_author_identity_global(
        self,
        author_name: str,
        author_id: str,
        source: str,
    ) -> dict[str, Any] | None:
        """Return one global identity row, or ``None`` when absent."""
        async with self._session() as session:
            row = await session.get(
                AuthorIdentityGlobalRow,
                (author_name, author_id, source),
            )
            return _author_identity_global_row_to_dict(row) if row else None

    @_typed_read
    async def get_author_identities_for_name(
        self,
        author_name: str,
    ) -> list[dict[str, Any]]:
        """Return every global identity row for one byline name.

        Used by :class:`~academic_intelligence.identity.resolver.Resolver`
        for the I8 cross-paper reuse check: a confirmed row for the name is
        returned directly instead of re-fetching the source.
        """
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(AuthorIdentityGlobalRow)
                    .where(AuthorIdentityGlobalRow.author_name == author_name)
                    .order_by(
                        AuthorIdentityGlobalRow.status,
                        AuthorIdentityGlobalRow.author_id,
                    )
                )
            ).scalars().all()
            return [_author_identity_global_row_to_dict(row) for row in rows]

    @_retry_busy
    async def save_author_identity(
        self,
        *,
        paper_id: str,
        author_name: str,
        author_id: str,
        source: str,
    ) -> None:
        """Upsert the paper-level identity evidence link.

        Maps a byline name inside *paper_id* to the global identity
        ``(author_id, source)`` (traceable paper-level evidence, I8).  The
        row is validated by the ``author_identity_global`` foreign key when
        FK enforcement is enabled.
        """
        try:
            async with self._session() as session:
                existing = await session.get(
                    AuthorIdentityRow,
                    (paper_id, author_name),
                )
                if existing is not None:
                    existing.author_id = author_id
                    existing.source = source
                else:
                    session.add(
                        AuthorIdentityRow(
                            paper_id=paper_id,
                            author_name=author_name,
                            author_id=author_id,
                            source=source,
                        )
                    )
                await session.commit()
        except Exception as exc:
            raise _write_storage_error(
                "save author identity", exc, self.db_path
            ) from exc

    @_typed_read
    async def get_author_identity(
        self,
        paper_id: str,
        author_name: str,
    ) -> dict[str, Any] | None:
        """Return the paper-level identity link row, or ``None``."""
        async with self._session() as session:
            row = await session.get(
                AuthorIdentityRow,
                (paper_id, author_name),
            )
            if row is None:
                return None
            return {
                "paper_id": row.paper_id,
                "author_name": row.author_name,
                "author_id": row.author_id,
                "source": row.source,
            }
