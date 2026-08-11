"""FIX-AD tests: P49 round-31 fault-injection defects.

- AD-1: write-path error messages must not leak the full SQL statement
  (``[SQL: ...]`` / ``[parameters: ...]``) — ``_trim_error_detail`` was only
  applied on the connect/read paths (FIX-AA-1); every write method now
  sanitizes and truncates its ``StorageError`` detail and carries a
  basename-only ``db_path`` context (FIX-AA-2 alignment).
- AD-2: a leftover ``-wal`` (unclean shutdown / crash) must be warned about
  on connect and checkpointed so the crash-loss window shrinks to the frames
  committed after connect; a clean database must not warn.
- AD-3: JSON backend ``_load_file``/``_save_file`` ``StorageError`` messages
  must use basename labels, never absolute paths (sqlite FIX-AA-2 alignment).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import Author, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _evidence() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W1",
        confidence=0.8,
    )


def _paper(paper_id: str) -> Paper:
    return Paper(
        id=paper_id,
        title=f"Paper {paper_id}",
        authors=["A"],
        year=2000,
        evidence=_evidence(),
    )


def _author(author_id: str) -> Author:
    return Author(
        id=author_id,
        name=f"Author {author_id}",
        evidence=_evidence(),
    )


def _citation(citing: str, cited: str) -> Citation:
    return Citation(
        citing_paper_id=citing,
        cited_paper_id=cited,
        evidence=_evidence(),
    )


def _norm(text: str) -> str:
    """Normalize slash style so Windows paths match in every spelling.

    Windows ``OSError`` messages escape the path separator (``C:\\\\Users``),
    while ``Path.__str__`` yields single backslashes and pytest's
    ``tmp_path`` uses forward slashes — collapse all three.
    """
    return text.replace("\\\\", "/").replace("\\", "/")


# ---------------------------------------------------------------------------
# F1 (AD-1): write-path StorageError strips [SQL: ...] / [parameters: ...]
# ---------------------------------------------------------------------------

_WRITE_OPS = (
    "save_paper",
    "save_batch",
    "save_author",
    "save_citation",
    "update_paper",
    "delete_paper",
)


async def _inject_write_failure(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Raise ENOSPC on the first write statement at the DBAPI boundary.

    SQLAlchemy wraps the raw ``sqlite3.OperationalError`` and appends
    ``[SQL: ...]`` / ``[parameters: ...]`` — the exact shape P49 observed
    (AD-1).  Reads (``SELECT``) are left alone so the failure lands on the
    write statement.
    """
    fired: dict[str, int] = {"n": 0}
    original = aiosqlite.Cursor.execute

    async def injected(
        self: Any, query: Any, parameters: Any = None
    ) -> Any:
        if isinstance(query, str) and query.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE")
        ):
            fired["n"] += 1
            raise sqlite3.OperationalError("database or disk is full")
        return await original(self, query, parameters)

    monkeypatch.setattr(aiosqlite.Cursor, "execute", injected)
    return fired


@pytest.mark.asyncio
@pytest.mark.parametrize("op", _WRITE_OPS)
async def test_ad1_write_error_message_strips_sql(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, op: str
) -> None:
    """Every write method surfaces a typed StorageError without SQL leaks."""
    db = tmp_path / "ad1.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        if op in ("update_paper", "delete_paper"):
            await store.save_paper(_paper("p-exists"))

        fired = await _inject_write_failure(monkeypatch)
        with pytest.raises(StorageError) as excinfo:
            if op == "save_paper":
                await store.save_paper(_paper("p-new"))
            elif op == "save_batch":
                await store.save_batch(papers=[_paper("p-batch")])
            elif op == "save_author":
                await store.save_author(_author("a-new"))
            elif op == "save_citation":
                await store.save_citation(_citation("c-new-1", "c-new-2"))
            elif op == "update_paper":
                await store.update_paper("p-exists", _paper("p-exists"))
            elif op == "delete_paper":
                await store.delete_paper("p-exists")

        err = excinfo.value
        assert fired["n"] >= 1, "injection must have hit a write statement"
        assert isinstance(err, StorageError)
        assert err.backend == "sqlite"
        assert "[SQL:" not in str(err)
        assert "[parameters:" not in str(err)
        # FIX-AA-2 alignment: basename-only db_path, no absolute path anywhere
        assert _norm(str(tmp_path)) not in _norm(str(err))
        assert err.context["db_path"] == "ad1.db"
        # still diagnosable: the failure category survives
        assert "database or disk is full" in str(err)
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F2 (AD-2): leftover -wal is warned about and checkpointed on connect
# ---------------------------------------------------------------------------


def _build_unclean_shutdown_wal(db: Path) -> sqlite3.Connection:
    """Write committed rows through a raw WAL connection and keep it open.

    A crash leaves exactly this state: a main db whose newest commits live
    only in the ``-wal`` file.  The connection stays open so the ``-wal`` is
    not auto-checkpointed away before the storage layer connects.
    """
    raw = sqlite3.connect(str(db))
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT)")
    raw.execute("INSERT INTO papers VALUES ('p-1', 'crash survivor 1')")
    raw.execute("INSERT INTO papers VALUES ('p-2', 'crash survivor 2')")
    raw.commit()
    wal = f"{db}-wal"
    assert os.path.exists(wal), "leftover -wal must exist (unclean shutdown)"
    assert os.path.getsize(wal) > 0, "leftover -wal must hold committed frames"
    return raw


@pytest.mark.asyncio
async def test_ad2_leftover_wal_warns_and_checkpoints(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A leftover ``-wal`` triggers a warning and its frames survive connect.

    The connect-time ``wal_checkpoint(TRUNCATE)`` folds the un-checkpointed
    committed frames into the main database, so the data survives even if the
    ``-wal`` is later discarded (P49's silent-loss window, AD-2).
    """
    db = tmp_path / "ad2_crash.db"
    raw = _build_unclean_shutdown_wal(db)
    try:
        store = SQLiteStorage(str(db))
        with caplog.at_level(
            logging.WARNING, logger="academic_intelligence.storage.sqlite_store"
        ):
            await store.connect()
        try:
            # the residue is surfaced, not silent (P49: no warning before)
            assert any("WAL" in r.message for r in caplog.records)
            # the raw writer still reads both rows after connect + checkpoint
            assert raw.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
        finally:
            await store.close()
    finally:
        raw.close()

    # checkpoint folded the frames into the main db: the data is safe even
    # with the -wal gone entirely
    wal = f"{db}-wal"
    if os.path.exists(wal):
        os.remove(wal)
    fresh = sqlite3.connect(str(db))
    try:
        assert fresh.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
    finally:
        fresh.close()


@pytest.mark.asyncio
async def test_ad2_clean_db_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A clean database (no -wal residue) connects without any warning."""
    db = tmp_path / "ad2_clean.db"
    store = SQLiteStorage(str(db))
    with caplog.at_level(
        logging.WARNING, logger="academic_intelligence.storage.sqlite_store"
    ):
        await store.connect()
        await store.save_paper(_paper("p-clean"))
        await store.close()
        # reconnect after a clean shutdown: still no -wal residue
        await store.connect()
        await store.close()
    assert not any("WAL" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# F3 (AD-3): JSON backend error messages use basename labels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ad3_json_load_error_has_no_absolute_path(
    tmp_path: Path,
) -> None:
    """A corrupt JSON file surfaces StorageError without the absolute path."""
    base = tmp_path / "json_leak"
    base.mkdir(parents=True, exist_ok=True)
    (base / "papers.json").write_text("{invalid json", encoding="utf-8")

    store = JSONStorage(str(base))
    with pytest.raises(StorageError) as excinfo:
        await store.connect()
    err = excinfo.value
    assert err.backend == "json"
    assert _norm(str(tmp_path)) not in _norm(str(err))
    assert "papers.json" in str(err)  # basename label keeps it diagnosable


@pytest.mark.asyncio
async def test_ad3_json_save_error_has_no_absolute_path(
    tmp_path: Path,
) -> None:
    """A failing JSON save surfaces StorageError without the absolute path."""
    base = tmp_path / "json_save_leak"
    store = JSONStorage(str(base))
    await store.connect()
    papers_file = store._papers_file
    try:
        papers_file.write_text("{}", encoding="utf-8")
        os.chmod(papers_file, stat.S_IREAD)
        with pytest.raises(StorageError) as excinfo:
            await store.close()
        err = excinfo.value
        assert _norm(str(tmp_path)) not in _norm(str(err))
        assert "papers.json" in str(err)
    finally:
        if papers_file.exists():
            os.chmod(papers_file, stat.S_IWRITE)
        if store._connected:
            await store.close()
