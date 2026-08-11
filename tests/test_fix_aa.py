"""FIX-AA tests: security-residual cleanup (P46 round-28 M-2/M-4/M-5/M-6).

- AA-1: storage error messages must not embed the full SQL statement
  (``[SQL: ...]``) nor absolute paths.
- AA-2: ``get_stats`` / ``StorageError.context`` must not leak absolute
  paths (basename-only labels).
- AA-3: retry defaults are narrowed to HTTP status + transport errors; a
  status outside ``retry_on_status`` (e.g. 400) is never retried, while
  429/500/503/504 and timeouts / connection errors still are.
- AA-4: ``connect()`` rolls back partial initialization on failure so no
  live HTTP client or undisposed storage engine is left behind.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import AuthorRef, Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from academic_intelligence.utils.http import HTTPClient
from academic_intelligence.utils.retry import RetryConfig, RetryHandler


def _evidence() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W1",
        confidence=0.8,
    )


def _paper(i: int) -> Paper:
    return Paper(
        id=f"paper-{i}",
        title=f"Paper {i}",
        authors=[AuthorRef(name="A", position=1)],
        year=2000,
        evidence=_evidence(),
    )


# ---------------------------------------------------------------------------
# F1 (AA-1): error messages carry the failure category, not the SQL statement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aa1_read_error_message_excludes_sql_statement(tmp_path: Path) -> None:
    """A read failing on a dropped table must not embed ``[SQL: ...]``."""
    db = tmp_path / "aa1.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        await store.save_paper(_paper(1))
        conn = sqlite3.connect(str(db))
        conn.execute("DROP TABLE papers")
        conn.commit()
        conn.close()

        with pytest.raises(StorageError) as excinfo:
            await store.query_papers()
        err = excinfo.value
        assert "[SQL:" not in str(err)
        assert "[SQL:" not in err.message
        # still diagnosable: the failure category is preserved
        assert "no such table" in str(err)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_aa1_connect_error_message_excludes_sql_and_path(
    tmp_path: Path,
) -> None:
    """A connect failure keeps its category but no SQL / absolute path."""
    db = tmp_path / "missing_dir" / "aa1.db"  # parent directory does not exist
    store = SQLiteStorage(str(db))
    try:
        with pytest.raises(StorageError) as excinfo:
            await store.connect()
        err = excinfo.value
        assert "[SQL:" not in str(err)
        assert "Failed to connect SQLite storage" in str(err)
        assert err.context["db_path"] == "aa1.db"  # basename only, no absolute path
    finally:
        # dispose the engine created before the failed connect (keeps the
        # aiosqlite worker thread from outliving the test event loop)
        await store.close()


# ---------------------------------------------------------------------------
# F2 (AA-2): get_stats / StorageError.context must not leak absolute paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aa2_get_stats_excludes_absolute_paths(tmp_path: Path) -> None:
    """``get_stats`` reports basename-only path labels for both backends."""
    sqlite_store = SQLiteStorage(str(tmp_path / "stats.db"))
    await sqlite_store.connect()
    try:
        stats = await sqlite_store.get_stats()
        assert json.dumps(stats)  # serializable
        assert str(tmp_path) not in json.dumps(stats)
        assert stats["db_path"] == "stats.db"
    finally:
        await sqlite_store.close()

    json_store = JSONStorage(str(tmp_path / "stats_json"))
    await json_store.connect()
    try:
        stats = await json_store.get_stats()
        assert str(tmp_path) not in json.dumps(stats)
        assert stats["base_path"] == "stats_json"
    finally:
        await json_store.close()


@pytest.mark.asyncio
async def test_aa2_storage_error_context_excludes_absolute_paths(
    tmp_path: Path,
) -> None:
    """``StorageError`` rendered context must not carry an absolute db path."""
    db = tmp_path / "aa2.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        await store.save_paper(_paper(1))
        conn = sqlite3.connect(str(db))
        conn.execute("DROP TABLE papers")
        conn.commit()
        conn.close()

        with pytest.raises(StorageError) as excinfo:
            await store.query_papers()
        err = excinfo.value
        assert str(tmp_path) not in str(err)
        assert str(tmp_path) not in str(err.context)
        assert err.context["db_path"] == "aa2.db"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F3 (AA-3): retry defaults narrowed to HTTP status + transport errors
# ---------------------------------------------------------------------------


def _status_error(status: int) -> httpx.HTTPStatusError:
    """Build an ``HTTPStatusError`` the way ``utils/http.py`` does (with
    ``status_code`` set via ``setattr``; httpx 0.28 has no such attribute)."""
    request = httpx.Request("GET", "https://api.example.com/resource")
    response = httpx.Response(status, request=request)
    err = httpx.HTTPStatusError(
        f"HTTP {status} for url", request=request, response=response
    )
    err.status_code = status  # type: ignore[attr-defined]  # mirror utils/http.py
    return err


def test_aa3_default_retry_on_narrowed_to_http_errors() -> None:
    assert RetryConfig().retry_on == (
        httpx.HTTPStatusError,
        httpx.TransportError,
    )


def test_aa3_status_outside_retry_on_status_not_retried() -> None:
    """A 400 response (not in retry_on_status) must never be retried."""
    assert RetryConfig().should_retry(_status_error(400)) is False


def test_aa3_retryable_statuses_still_retried() -> None:
    for status in (429, 500, 503, 504):
        assert RetryConfig().should_retry(_status_error(status)) is True


def test_aa3_transport_error_retried_without_status() -> None:
    assert RetryConfig().should_retry(httpx.TimeoutException("timed out")) is True
    assert RetryConfig().should_retry(httpx.ConnectError("refused")) is True


def test_aa3_generic_exception_not_retried_by_default() -> None:
    assert RetryConfig().should_retry(RuntimeError("boom")) is False


@pytest.mark.asyncio
async def test_aa3_retry_handler_retries_transport_error() -> None:
    handler = RetryHandler(
        RetryConfig(max_retries=2, base_delay=0.01, jitter=False)
    )
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TransportError("transient")
        return "ok"

    assert await handler.execute(flaky) == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_aa3_retry_handler_does_not_retry_400() -> None:
    handler = RetryHandler(
        RetryConfig(max_retries=3, base_delay=0.01, jitter=False)
    )
    calls = {"n": 0}

    def always_400() -> httpx.Response:
        calls["n"] += 1
        raise _status_error(400)

    with pytest.raises(httpx.HTTPStatusError):
        await handler.execute(always_400)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# F4 (AA-4): connect() rolls back partial initialization on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aa4_connect_failure_rolls_back_partial_init(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A storage connect failure must close the HTTP client and storage, null
    both handles, leave ``_connected`` False, and propagate the error."""

    closed_http: list[Any] = []
    orig_close = HTTPClient.close

    async def _tracking_close(self: Any) -> None:
        closed_http.append(self)
        await orig_close(self)

    monkeypatch.setattr(HTTPClient, "close", _tracking_close)

    storage_closed: list[Any] = []

    class _FailingStorage:
        async def connect(self) -> None:
            raise StorageError("connect boom", backend="sqlite")

        async def close(self) -> None:
            storage_closed.append(self)

    def _bad_storage(self: Any) -> Any:
        return _FailingStorage()

    monkeypatch.setattr(AcademicIntelligence, "_build_storage", _bad_storage)

    ai = AcademicIntelligence(
        Config(storage_type="sqlite", storage_path=str(tmp_path / "x.db"))
    )
    with pytest.raises(StorageError, match="connect boom"):
        await ai.connect()

    assert ai._http is None
    assert ai._storage is None
    assert ai._connected is False
    assert closed_http, "HTTP client must be closed during rollback"
    assert storage_closed, "storage must be closed during rollback"


@pytest.mark.asyncio
async def test_aa4_connect_failure_bad_sqlite_path_cleans_up(
    tmp_path: Path,
) -> None:
    """Real bad path: a SQLite open failure leaves no handles behind."""
    bad_db = str(tmp_path / "no_such_dir" / "x.db")
    ai = AcademicIntelligence(Config(storage_type="sqlite", storage_path=bad_db))
    with pytest.raises(StorageError):
        await ai.connect()
    assert ai._http is None
    assert ai._storage is None
    assert ai._connected is False
