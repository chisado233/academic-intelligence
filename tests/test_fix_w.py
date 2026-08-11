"""FIX-W: query_authors LIKE escaping (W1), non-ASCII case contract (W2),
Unicode NFC normalization (W3), query ordering contract (W6).

Covers (from P41 round-23 i18n findings):
- F1 (W1): ``query_authors`` name/affiliation filters must treat ``%`` /
  ``_`` literally (matching ``query_papers`` since FIX-I F1) instead of
  leaking them as SQL LIKE wildcards — ``name='%'`` used to return every
  author (14/14 in the P41 probe).
- F2 (W2): SQLite LIKE is only ASCII-case-insensitive, so an uppercase
  Cyrillic/Greek keyword/venue/author-name query used to miss; non-ASCII
  queries are now case-folded in Python, consistent with the JSON backend.
- F3 (W3): NFC and NFD spellings of the same text must interoperate for
  queries and dedup; already-stored NFD data (written before the fix) is
  still queryable through read-side normalization.
- F4 (W6): query results are explicitly ordered by insertion order (rowid),
  stable across calls and consistent with the JSON backend.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from academic_intelligence.core.models import Author, AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(conf: float = 0.8) -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://e.com",
        confidence=conf,
    )


def _paper(**kwargs: object) -> Paper:
    defaults: dict[str, object] = {
        "title": "FIX-W Paper",
        "authors": ["Ada"],
        "year": 2020,
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Paper(**defaults)  # type: ignore[arg-type]


def _author(**kwargs: object) -> Author:
    defaults: dict[str, object] = {"name": "FIX-W Author", "evidence": _ev()}
    defaults.update(kwargs)
    return Author(**defaults)  # type: ignore[arg-type]


def _make_store(tmp_path: Path, backend: str) -> SQLiteStorage | JSONStorage:
    if backend == "sqlite":
        return SQLiteStorage(str(tmp_path / "w.db"))
    return JSONStorage(str(tmp_path / "w"))


# ---------------------------------------------------------------------------
# F1 (W1): query_authors name/affiliation LIKE wildcard escaping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_query_authors_wildcards_match_literally(
    tmp_path: Path, backend: str
) -> None:
    """W1: ``name='%'`` must only match a literal ``%`` (not every author),
    and ``name='under_score'`` only a literal underscore — on both backends."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            authors=[
                _author(id="a1", name="100% Pure"),
                _author(id="a2", name="100x Pure"),
                _author(id="a3", name="under_score"),
                _author(id="a4", name="under-score"),
                _author(id="a5", name="Plain"),
            ]
        )
        assert [a.id for a in await store.query_authors(name="%")] == ["a1"]
        assert [a.id for a in await store.query_authors(name="under_score")] == ["a3"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_query_authors_affiliation_wildcards_match_literally(
    tmp_path: Path, backend: str
) -> None:
    """W1: an affiliation containing a literal ``%`` matches only that literal
    affiliation, not every ``%``-prefixed one."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            authors=[
                _author(id="v1", name="A", affiliation="100% Lab"),
                _author(id="v2", name="B", affiliation="100x Lab"),
            ]
        )
        assert [a.id for a in await store.query_authors(affiliation="100%")] == ["v1"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_query_authors_wildcards_backends_consistent(tmp_path: Path) -> None:
    """W1: sqlite and json return identical author ids for wildcard-bearing
    name / affiliation queries."""
    authors = [
        _author(id="a1", name="100% Pure", affiliation="100% Lab"),
        _author(id="a2", name="100x Pure", affiliation="100x Lab"),
        _author(id="a3", name="under_score", affiliation="Plain"),
        _author(id="a4", name="under-score", affiliation="Plain"),
    ]
    sqlite_store = SQLiteStorage(str(tmp_path / "c.db"))
    json_store = JSONStorage(str(tmp_path / "c"))
    await sqlite_store.connect()
    await json_store.connect()
    try:
        await sqlite_store.save_batch(authors=authors)
        await json_store.save_batch(authors=authors)

        async def ids(store: SQLiteStorage | JSONStorage, **kw: object) -> list[str]:
            return [a.id for a in await store.query_authors(**kw)]

        for query in ({"name": "%"}, {"name": "under_score"}, {"affiliation": "100%"}):
            assert await ids(sqlite_store, **query) == await ids(json_store, **query)
    finally:
        await sqlite_store.close()
        await json_store.close()


# ---------------------------------------------------------------------------
# F2 (W2): non-ASCII case contract — Python-side folding for non-ASCII queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_cyrillic_uppercase_keyword_hits(tmp_path: Path, backend: str) -> None:
    """W2: an uppercase Cyrillic keyword must hit the stored mixed-case title
    on both backends (SQLite LIKE alone is ASCII-only case-insensitive)."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _paper(
                    id="ru-1",
                    title="Глубокое обучение в обработке естественного языка",
                ),
                _paper(id="en-1", title="Deep Learning Survey"),
            ]
        )
        assert [p.id for p in await store.query_papers(keyword="ГЛУБОКОЕ")] == ["ru-1"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_greek_case_contract_documented(tmp_path: Path, backend: str) -> None:
    """W2: Greek case contract is documented, not silently inconsistent.

    All-caps Greek ('ΑΛΓΟΡΙΘΜΟΙ') drops the accents, and ``str.lower()``
    cannot restore them, so uppercase Greek queries stay unmatched on BOTH
    backends — closing that gap needs accent folding (FIX-W W4, out of
    scope). The exact-case and lowercase spellings do hit.
    """
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[_paper(id="gr-1", title="Αλγόριθμοι μάθησης και εφαρμογές")]
        )
        assert [p.id for p in await store.query_papers(keyword="Αλγόριθμοι")] == [
            "gr-1"
        ]
        assert [p.id for p in await store.query_papers(keyword="αλγόριθμοι")] == [
            "gr-1"
        ]
        # Documented limitation (W4): accent folding is out of scope, so the
        # accent-less all-caps spelling still misses on both backends.
        assert await store.query_papers(keyword="ΑΛΓΟΡΙΘΜΟΙ") == []
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_cyrillic_uppercase_venue_hits(tmp_path: Path, backend: str) -> None:
    """W2: uppercase Cyrillic venue must hit the stored mixed-case venue."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[_paper(id="v1", title="T", venue="Глубокое обучение журнал")]
        )
        assert [p.id for p in await store.query_papers(venue="ГЛУБОКОЕ")] == ["v1"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_cyrillic_uppercase_author_name_hits_query_authors(
    tmp_path: Path, backend: str
) -> None:
    """W2: ``query_authors(name=...)`` must be non-ASCII case-insensitive like
    the ``query_papers`` author path (P41: 'ИВАНОВ' used to miss)."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            authors=[
                _author(id="a-ru", name="Иванов Иван"),
                _author(id="a-en", name="John Smith"),
            ]
        )
        assert [a.id for a in await store.query_authors(name="ИВАНОВ")] == ["a-ru"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_ascii_case_insensitive_unchanged(tmp_path: Path, backend: str) -> None:
    """W2: ASCII keyword case-insensitivity is unchanged by the folding."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(papers=[_paper(id="p1", title="Deep Learning Survey")])
        assert [p.id for p in await store.query_papers(keyword="DEEP")] == ["p1"]
        assert [p.id for p in await store.query_papers(keyword="deep")] == ["p1"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_non_ascii_case_backends_consistent(tmp_path: Path) -> None:
    """W2: sqlite and json agree on uppercase non-ASCII keyword / author-name
    queries."""
    sqlite_store = SQLiteStorage(str(tmp_path / "c.db"))
    json_store = JSONStorage(str(tmp_path / "c"))
    await sqlite_store.connect()
    await json_store.connect()
    try:
        await sqlite_store.save_batch(
            papers=[_paper(id="ru-1", title="Глубокое обучение")],
            authors=[_author(id="a-ru", name="Иванов Иван")],
        )
        await json_store.save_batch(
            papers=[_paper(id="ru-1", title="Глубокое обучение")],
            authors=[_author(id="a-ru", name="Иванов Иван")],
        )

        async def paper_ids(
            store: SQLiteStorage | JSONStorage, **kw: object
        ) -> list[str]:
            return [p.id for p in await store.query_papers(**kw)]

        async def author_ids(
            store: SQLiteStorage | JSONStorage, **kw: object
        ) -> list[str]:
            return [a.id for a in await store.query_authors(**kw)]

        assert await paper_ids(sqlite_store, keyword="ГЛУБОКОЕ") == await paper_ids(
            json_store, keyword="ГЛУБОКОЕ"
        )
        assert await author_ids(sqlite_store, name="ИВАНОВ") == await author_ids(
            json_store, name="ИВАНОВ"
        )
    finally:
        await sqlite_store.close()
        await json_store.close()


# ---------------------------------------------------------------------------
# F3 (W3): Unicode NFC normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_nfc_nfd_keyword_interop(tmp_path: Path, backend: str) -> None:
    """W3: a precomposed 'Résumé' and a decomposed 'Re\\u0301sume\\u0301' are
    the same text: either spelling must hit both stored records."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _paper(id="comp-1", title="Résumé"),
                _paper(id="decomp-1", title="Re\u0301sume\u0301"),
            ]
        )
        assert [p.id for p in await store.query_papers(keyword="Résumé")] == [
            "comp-1",
            "decomp-1",
        ]
        assert [
            p.id for p in await store.query_papers(keyword="Re\u0301sume\u0301")
        ] == ["comp-1", "decomp-1"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_nfc_nfd_author_query_interop(tmp_path: Path, backend: str) -> None:
    """W3: decomposed and precomposed author-name spellings interoperate for
    ``query_authors`` and ``query_papers(author=...)``."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            authors=[_author(id="jos", name="Jos\u00e9 Silva")],  # NFC é
            papers=[
                _paper(
                    id="p1",
                    title="T",
                    authors=[AuthorRef(name="Jose\u0301 Silva")],  # NFD e + U+0301
                )
            ],
        )
        assert [a.id for a in await store.query_authors(name="Jose\u0301")] == ["jos"]
        assert [p.id for p in await store.query_papers(author="Jos\u00e9")] == ["p1"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pre_fix_nfd_stored_data_still_queryable(tmp_path: Path) -> None:
    """W3: data written before normalization (a decomposed title forced into
    the db behind the model's back) is still found by a precomposed query —
    the read path normalizes too."""
    store = SQLiteStorage(str(tmp_path / "old.db"))
    await store.connect()
    try:
        await store.save_batch(papers=[_paper(id="old-1", title="Résumé")])
        # Simulate pre-fix storage: rewrite the row with the decomposed form.
        with sqlite3.connect(str(tmp_path / "old.db")) as conn:
            conn.execute(
                "UPDATE papers SET title = ? WHERE id = ?",
                ("Re\u0301sume\u0301", "old-1"),
            )
        assert [p.id for p in await store.query_papers(keyword="Résumé")] == ["old-1"]
    finally:
        await store.close()


def test_nfc_nfd_dedup_merges() -> None:
    """W3: precomposed and decomposed spellings of the same title merge in
    dedup instead of being treated as different texts."""
    nfc = Paper(
        title="Résumé",
        authors=[AuthorRef(name="A", position=1)],
        year=2020,
        evidence_list=[_ev()],
    )
    nfd = Paper(
        title="Re\u0301sume\u0301",
        authors=[AuthorRef(name="B", position=1)],
        year=2020,
        evidence_list=[_ev()],
    )
    merged = Deduplicator().deduplicate_papers([nfc, nfd])
    assert len(merged) == 1


# ---------------------------------------------------------------------------
# F4 (W6): query ordering contract — insertion order, stable across calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_query_papers_order_stable_and_insertion(
    tmp_path: Path, backend: str
) -> None:
    """W6: query_papers returns results in insertion order, identically on
    every call and across pagination pages."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        papers = [_paper(id=f"p{i}", title=f"Ordered Title {i}") for i in range(5)]
        await store.save_batch(papers=papers)
        first = [p.id for p in await store.query_papers()]
        second = [p.id for p in await store.query_papers()]
        assert first == second == ["p0", "p1", "p2", "p3", "p4"]
        # pagination pages never overlap or skip
        page1 = [p.id for p in await store.query_papers(limit=3)]
        page2 = [p.id for p in await store.query_papers(limit=3, offset=3)]
        assert page1 + page2 == ["p0", "p1", "p2", "p3", "p4"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_query_authors_order_stable_and_insertion(
    tmp_path: Path, backend: str
) -> None:
    """W6: query_authors returns results in insertion order, stable across
    calls."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        authors = [_author(id=f"a{i}", name=f"Author {i}") for i in range(4)]
        await store.save_batch(authors=authors)
        first = [a.id for a in await store.query_authors()]
        second = [a.id for a in await store.query_authors()]
        assert first == second == ["a0", "a1", "a2", "a3"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "json"])
async def test_query_order_with_filter_preserved(tmp_path: Path, backend: str) -> None:
    """W6: filtered queries (author / non-ASCII keyword) preserve insertion
    order too."""
    store = _make_store(tmp_path, backend)
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _paper(id="b1", title="Глубокое A", authors=["Bob"]),
                _paper(id="b2", title="Глубокое B", authors=["Bob"]),
                _paper(id="a1", title="Other", authors=["Ada"]),
            ]
        )
        assert [p.id for p in await store.query_papers(author="Bob")] == ["b1", "b2"]
        assert [p.id for p in await store.query_papers(keyword="глубокое")] == [
            "b1",
            "b2",
        ]
    finally:
        await store.close()
