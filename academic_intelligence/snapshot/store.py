"""SQLite schema and queries for the OpenAlex works snapshot index.

Schema (per the snapshot design; ``snapshot_parts`` is the internal
build-state table that makes ``paper snapshot build`` interruptible/resumable):

.. code-block:: sql

    CREATE TABLE snapshot_meta (snapshot_date TEXT PRIMARY KEY, status TEXT, built_at TEXT);
    CREATE TABLE snapshot_works (
      id TEXT PRIMARY KEY, title TEXT, year INTEGER, doi TEXT, cited_by_count INTEGER
    );
    CREATE INDEX idx_works_year ON snapshot_works(year);
    CREATE TABLE snapshot_citations (
      cited_id TEXT, citing_id TEXT, PRIMARY KEY (cited_id, citing_id)
    );
    CREATE INDEX idx_cit_citing ON snapshot_citations(citing_id);

The citation table is the **inverted** form of OpenAlex ``referenced_works``:
each work row contributes ``(cited_id=<referenced work>, citing_id=<this work>)``
edges, so "who cites W" is a single ``cited_id`` lookup — the exact query
``paper trace-citing --use-snapshot`` needs.

All ids are normalized to bare OpenAlex ``W…`` form (``https://openalex.org/W1``
→ ``W1``) and DOIs to bare ``10.…`` form, matching
:mod:`academic_intelligence.trace.citing`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from academic_intelligence.core.models import normalize_doi as _normalize_doi
from academic_intelligence.snapshot import (
    SnapshotError,
    routing_config_path,
)

#: Bare OpenAlex work id (``W123``), optionally URL-prefixed.
_WORK_ID_RE = re.compile(r"^(?:https?://openalex\.org/)?(W\d+)/?$", re.IGNORECASE)

_STATUS_BUILDING = "building"
_STATUS_BUILT = "built"


def normalize_work_id(value: Any) -> str | None:
    """Normalize an OpenAlex work id input to the bare ``W123`` form.

    Accepts bare ids and full ``https://openalex.org/W123`` URLs (trailing
    slash tolerated); returns ``None`` for anything else.
    """
    if not isinstance(value, str):
        return None
    match = _WORK_ID_RE.match(value.strip())
    return match.group(1) if match else None


def normalize_doi(value: Any) -> str | None:
    """Normalize a DOI input to bare ``10.…`` form (None when not a DOI)."""
    if not isinstance(value, str):
        return None
    return _normalize_doi(value)


def read_routing_config(snapshot_dir: Path) -> bool | None:
    """Read the ``paper snapshot enable/disable`` routing switch.

    Returns ``True``/``False`` when the config file exists and carries a
    boolean ``use_snapshot``, else ``None`` (no explicit switch stored).
    """
    path = routing_config_path(snapshot_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("use_snapshot") if isinstance(data, dict) else None
    return value if isinstance(value, bool) else None


def write_routing_config(snapshot_dir: Path, enabled: bool) -> None:
    """Persist the routing switch to ``<snapshot_dir>/config.json``."""
    path = routing_config_path(snapshot_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"use_snapshot": enabled}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class SnapshotStore:
    """SQLite store for the snapshot index (works + inverted citations)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # connection / schema
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the SQLite connection and ensure the schema exists."""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        # Autocommit mode: transactions are started/committed explicitly by
        # :meth:`transaction` (Python 3.12's legacy implicit transaction mode
        # would otherwise reject the explicit ``BEGIN``).
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._conn = conn
        self.init_schema()

    def close(self) -> None:
        """Close the SQLite connection (idempotent)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _check_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise SnapshotError("snapshot store is not connected")
        return self._conn

    def init_schema(self) -> None:
        """Create all tables/indexes (idempotent)."""
        self._check_conn().executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshot_meta (
                snapshot_date TEXT PRIMARY KEY,
                status TEXT,
                built_at TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshot_works (
                id TEXT PRIMARY KEY,
                title TEXT,
                year INTEGER,
                doi TEXT,
                cited_by_count INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_works_year ON snapshot_works(year);
            CREATE TABLE IF NOT EXISTS snapshot_citations (
                cited_id TEXT,
                citing_id TEXT,
                PRIMARY KEY (cited_id, citing_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cit_citing
                ON snapshot_citations(citing_id);
            -- Internal build state: which partition files are fully indexed
            -- (makes `paper snapshot build` interruptible and resumable).
            CREATE TABLE IF NOT EXISTS snapshot_parts (
                file_key TEXT PRIMARY KEY,
                built_at TEXT
            );
            """
        )

    def drop_all(self) -> None:
        """Drop every snapshot table (fresh rebuild)."""
        self._check_conn().executescript(
            """
            DROP TABLE IF EXISTS snapshot_citations;
            DROP TABLE IF EXISTS snapshot_works;
            DROP TABLE IF EXISTS snapshot_meta;
            DROP TABLE IF EXISTS snapshot_parts;
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block inside one SQLite transaction (commit/rollback)."""
        conn = self._check_conn()
        conn.execute("BEGIN")
        try:
            yield
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # meta / build state
    # ------------------------------------------------------------------

    def get_meta(self) -> dict[str, Any] | None:
        """Return the most recent ``snapshot_meta`` row (or None)."""
        row = (
            self._check_conn()
            .execute(
                "SELECT snapshot_date, status, built_at FROM snapshot_meta "
                "ORDER BY snapshot_date DESC LIMIT 1"
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def set_meta(self, snapshot_date: str, status: str) -> None:
        """Upsert the snapshot status row (ISO-8601 ``built_at``)."""
        built_at = datetime.now(UTC).isoformat()
        self._check_conn().execute(
            "INSERT OR REPLACE INTO snapshot_meta (snapshot_date, status, built_at) "
            "VALUES (?, ?, ?)",
            (snapshot_date, status, built_at),
        )

    def is_built(self) -> bool:
        """True when a snapshot finished building successfully."""
        meta = self.get_meta()
        return bool(meta and meta.get("status") == _STATUS_BUILT)

    def is_part_built(self, file_key: str) -> bool:
        """True when the partition file was already indexed."""
        row = (
            self._check_conn()
            .execute("SELECT 1 FROM snapshot_parts WHERE file_key = ?", (file_key,))
            .fetchone()
        )
        return row is not None

    def mark_part_built(self, file_key: str) -> None:
        """Record a partition file as fully indexed."""
        built_at = datetime.now(UTC).isoformat()
        self._check_conn().execute(
            "INSERT OR REPLACE INTO snapshot_parts (file_key, built_at) VALUES (?, ?)",
            (file_key, built_at),
        )

    def built_part_keys(self) -> set[str]:
        """Return the set of indexed partition file keys (resume bookkeeping)."""
        rows = self._check_conn().execute("SELECT file_key FROM snapshot_parts").fetchall()
        return {row["file_key"] for row in rows}

    # ------------------------------------------------------------------
    # data writes
    # ------------------------------------------------------------------

    def insert_works(self, rows: list[dict[str, Any]]) -> None:
        """Bulk-insert works (``INSERT OR IGNORE`` — idempotent)."""
        if not rows:
            return
        self._check_conn().executemany(
            "INSERT OR IGNORE INTO snapshot_works "
            "(id, title, year, doi, cited_by_count) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    row["id"],
                    row.get("title"),
                    row.get("year"),
                    row.get("doi"),
                    row.get("cited_by_count", 0),
                )
                for row in rows
            ],
        )

    def insert_citation_pairs(self, pairs: list[tuple[str, str]]) -> None:
        """Bulk-insert inverted citation edges ``(cited_id, citing_id)``."""
        if not pairs:
            return
        self._check_conn().executemany(
            "INSERT OR IGNORE INTO snapshot_citations (cited_id, citing_id) VALUES (?, ?)",
            pairs,
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def query_work(self, work_id: str) -> dict[str, Any] | None:
        """Look up one work by bare W-id; None when absent."""
        row = (
            self._check_conn()
            .execute(
                "SELECT id, title, year, doi, cited_by_count FROM snapshot_works WHERE id = ?",
                (work_id,),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def query_work_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Look up the first work with this bare DOI; None when absent."""
        row = (
            self._check_conn()
            .execute(
                "SELECT id, title, year, doi, cited_by_count FROM snapshot_works "
                "WHERE doi = ? ORDER BY cited_by_count DESC LIMIT 1",
                (doi,),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def query_citing(self, work_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Return works citing *work_id* (most-cited first).

        Joins the inverted edge table with the works table so the caller gets
        ``citing_id`` plus title/year/doi/cited_by_count without a second pass.
        """
        sql = (
            "SELECT w.id AS citing_id, w.title, w.year, w.doi, w.cited_by_count "
            "FROM snapshot_citations c JOIN snapshot_works w ON w.id = c.citing_id "
            "WHERE c.cited_id = ? ORDER BY w.cited_by_count DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = self._check_conn().execute(sql, (work_id,)).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        """Return (works_count, citation_count) for the current index."""
        conn = self._check_conn()
        works = conn.execute("SELECT COUNT(*) AS n FROM snapshot_works").fetchone()
        citations = conn.execute("SELECT COUNT(*) AS n FROM snapshot_citations").fetchone()
        return {
            "works_count": int(works["n"]) if works is not None else 0,
            "citation_count": int(citations["n"]) if citations is not None else 0,
        }
