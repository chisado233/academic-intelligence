"""FIX-I: LIKE escaping (I-1), non-ASCII author query (I-2), concurrent
create_all race (I-3), negative pagination (I-4).

Covers:
- F1: ``query_papers`` keyword/venue filters must treat ``%`` / ``_`` /
  ``\\`` in user input literally (matching the JSON backend's pure-substring
  semantics) instead of leaking them as SQL LIKE wildcards.
- F2: ``query_papers(author=...)`` on the sqlite backend must find papers
  whose authors contain non-ASCII (Chinese/Japanese) names, which the SQL
  token prefilter cannot match against the ASCII-escaped JSON column.
- F3: two connections racing ``connect()`` on a brand-new database must both
  succeed instead of one failing with ``table papers already exists``.
- F4: negative ``limit`` / ``offset`` are rejected on both backends; the two
  backends agree on pagination boundaries.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from academic_intelligence.core.models import Author, AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import Base, SQLiteStorage


def _ev(conf: float = 0.8) -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://e.com",
        confidence=conf,
    )


def _paper(**kwargs: object) -> Paper:
    defaults: dict[str, object] = {
        "title": "FIX-I Paper",
        "authors": ["Ada"],
        "year": 2020,
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Paper(**defaults)  # type: ignore[arg-type]


def _make_store(tmp_path: Path, backend: str) -> SQLiteStorage | JSONStorage:
    if backend == "sqlite":
        return SQLiteStorage(str(tmp_path / "i.db"))
    return JSONStorage(str(tmp_path / "i"))


# ---------------------------------------------------------------------------
# F1 (I-1): LIKE wildcard escaping for keyword / venue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_like_wildcards_in_keyword_match_literally(
    tmp_path: Path, backend: str
) -> None:
    """I-1: keyword ``100%`` must only match a literal ``100%`` (not ``100x``);
    ``under_score`` only a literal underscore, on both backends."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _paper(id="p6", title="100% Pure Machine Code"),
                _paper(id="p7", title="Under_score and dash-test"),
                _paper(id="p10", title="100x Speedup Report"),
            ]
        )
        assert [p.id for p in await store.query_papers(keyword="100%")] == ["p6"]
        assert [p.id for p in await store.query_papers(keyword="under_score")] == ["p7"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_like_wildcards_in_venue_match_literally(
    tmp_path: Path, backend: str
) -> None:
    """I-1: a venue containing ``%`` matches only the literal venue string."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _paper(id="v1", title="A", venue="100% Club"),
                _paper(id="v2", title="B", venue="100x Club"),
            ]
        )
        assert [p.id for p in await store.query_papers(venue="100%")] == ["v1"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_like_escaping_backends_consistent(tmp_path: Path) -> None:
    """I-1: sqlite and json backends return identical ids for wildcard-bearing
    keyword / venue queries."""
    papers = [
        _paper(id="p6", title="100% Pure Machine Code", venue="100% Club"),
        _paper(id="p7", title="Under_score and dash-test", venue="Plain"),
        _paper(id="p10", title="100x Speedup Report", venue="100x Club"),
    ]
    sqlite_store = SQLiteStorage(str(tmp_path / "c.db"))
    json_store = JSONStorage(str(tmp_path / "c"))
    await sqlite_store.connect()
    await json_store.connect()
    try:
        await sqlite_store.save_batch(papers=papers)
        await json_store.save_batch(papers=papers)

        async def ids(store: SQLiteStorage | JSONStorage, **kw: object) -> list[str]:
            return [p.id for p in await store.query_papers(**kw)]

        for query in (
            {"keyword": "100%"},
            {"keyword": "under_score"},
            {"keyword": "dash-test"},
            {"venue": "100%"},
        ):
            assert await ids(sqlite_store, **query) == await ids(json_store, **query)
    finally:
        await sqlite_store.close()
        await json_store.close()


# ---------------------------------------------------------------------------
# F2 (I-2): non-ASCII (Chinese) author query on sqlite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_chinese_author_query_hits(tmp_path: Path, backend: str) -> None:
    """I-2: querying a Chinese author name must hit the paper on both
    backends; the English part of a mixed name is queryable too."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _paper(
                    id="cn-1",
                    title="深度学习研究",
                    authors=[AuthorRef(name="张三·Zhang San", position=1)],
                ),
                _paper(
                    id="en-1",
                    title="Other Paper",
                    authors=[AuthorRef(name="John Smith", position=1)],
                ),
            ]
        )
        assert [p.id for p in await store.query_papers(author="张三")] == ["cn-1"]
        assert [p.id for p in await store.query_papers(author="Zhang")] == ["cn-1"]
        assert [p.id for p in await store.query_papers(author="张三·Zhang San")] == [
            "cn-1"
        ]
        assert await store.query_papers(author="李四") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_chinese_author_query_backends_consistent(tmp_path: Path) -> None:
    """I-2: sqlite and json agree on non-ASCII author queries."""
    papers = [
        _paper(
            id="cn-1",
            title="深度学习研究",
            authors=[AuthorRef(name="张三·Zhang San", position=1)],
        ),
        _paper(
            id="cn-2",
            title="自然语言处理",
            authors=[AuthorRef(name="田中太郎", position=1)],
        ),
        _paper(
            id="en-1",
            title="Other",
            authors=[AuthorRef(name="John Smith", position=1)],
        ),
    ]
    sqlite_store = SQLiteStorage(str(tmp_path / "c2.db"))
    json_store = JSONStorage(str(tmp_path / "c2"))
    await sqlite_store.connect()
    await json_store.connect()
    try:
        await sqlite_store.save_batch(papers=papers)
        await json_store.save_batch(papers=papers)

        async def ids(store: SQLiteStorage | JSONStorage, author: str) -> list[str]:
            return [p.id for p in await store.query_papers(author=author)]

        for author in ("张三", "田中", "Zhang", "张三·Zhang San", "李四"):
            assert await ids(sqlite_store, author) == await ids(json_store, author), (
                f"author={author!r} backend mismatch"
            )
    finally:
        await sqlite_store.close()
        await json_store.close()


# ---------------------------------------------------------------------------
# F3 (I-3): concurrent first connect() create_all race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_first_connect_no_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-3: two connections racing to create a brand-new database both
    succeed.  ``create_all`` is patched so the loser deterministically hits
    the ``table ... already exists`` collision once: both processes pass the
    existence check against an empty schema, the loser's CREATE TABLE
    collides with the winner's, and the retry then converges on the winner's
    complete schema."""
    db = tmp_path / "race.db"
    original_create_all = Base.metadata.create_all
    state: dict[str, int] = {"calls": 0}

    def racing_create_all(*args: object, **kwargs: object) -> object:
        state["calls"] += 1
        if state["calls"] == 1:
            # winner: creates the brand-new schema
            return original_create_all(*args, **kwargs)
        if state["calls"] == 2:
            # loser, first attempt: its existence check already ran against
            # the empty schema, so it re-attempts creation without re-checking
            # -> collides with the winner's tables, raising
            # "table papers already exists".
            return original_create_all(*args, checkfirst=False, **kwargs)
        # loser, retry: the winner's schema is complete; the default
        # checkfirst sees every table and converges.
        return original_create_all(*args, **kwargs)

    monkeypatch.setattr(Base.metadata, "create_all", racing_create_all)

    store1 = SQLiteStorage(str(db))
    store2 = SQLiteStorage(str(db))
    await asyncio.gather(store1.connect(), store2.connect())
    try:
        # both connections are usable after the racing first connect
        assert await store1.query_papers() == []
        assert await store2.query_papers() == []
        await store1.save_batch(papers=[_paper(id="race-1", title="Raced")])
        assert [p.id for p in await store2.query_papers()] == ["race-1"]
    finally:
        await store1.close()
        await store2.close()


@pytest.mark.asyncio
async def test_concurrent_connect_existing_db_unaffected(tmp_path: Path) -> None:
    """I-3: concurrent connects on an already-created database remain fine."""
    db = tmp_path / "exists.db"
    first = SQLiteStorage(str(db))
    await first.connect()
    await first.save_batch(papers=[_paper(id="e-1", title="Existing")])
    await first.close()

    store1 = SQLiteStorage(str(db))
    store2 = SQLiteStorage(str(db))
    await asyncio.gather(store1.connect(), store2.connect())
    try:
        assert [p.id for p in await store1.query_papers()] == ["e-1"]
        assert [p.id for p in await store2.query_papers()] == ["e-1"]
    finally:
        await store1.close()
        await store2.close()


# ---------------------------------------------------------------------------
# F4 (I-4): negative limit / offset rejected, both backends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_negative_limit_offset_rejected(
    tmp_path: Path, backend: str
) -> None:
    """I-4: negative limit / offset are rejected with ValueError on both
    backends (previously sqlite LIMIT -1 meant 'all' and json sliced the
    last row away)."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(papers=[_paper(id="n-1", title="T")])
        with pytest.raises(ValueError):
            await store.query_papers(limit=-1)
        with pytest.raises(ValueError):
            await store.query_papers(offset=-1)
        with pytest.raises(ValueError):
            await store.query_papers(author="x", limit=-1)
        with pytest.raises(ValueError):
            await store.query_authors(limit=-1)
        with pytest.raises(ValueError):
            await store.query_authors(offset=-1)
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_limit_zero_returns_empty(tmp_path: Path, backend: str) -> None:
    """I-4: limit=0 returns zero rows on both backends (paper and author)."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[_paper(id="z-1", title="T")],
            authors=[Author(id="za-1", name="N", evidence=_ev())],
        )
        assert await store.query_papers(limit=0) == []
        assert await store.query_papers(limit=0, offset=0) == []
        assert await store.query_authors(limit=0) == []
    finally:
        await store.close()
