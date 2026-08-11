"""FIX-P: extreme data-boundary fixes found by B7-P34 round 16.

Covers:
- F1 (P4): single-record ``save_paper`` coauthorship counting reuses the
  batch path's Python aggregation (:func:`_apply_coauthorship_deltas`)
  instead of one ``session.get`` per author pair — 200 resolved authors
  drop from ~66s to seconds, and the C(n,2) counters stay correct.
- F2 (P1): NUL (``\\x00``) in LIKE inputs is rejected instead of silently
  truncating the pattern to a wildcard (SQLite ``LIKE`` follows C-string
  semantics, so ``keyword="\\x00"`` used to match the whole table).
- F3 (P2): the read path wraps ``UnicodeEncodeError`` (lone surrogates in
  query strings) into :class:`StorageError` like the other read failures.
- F4 (P3): ``raw_data`` nesting deeper than pydantic-core's ~99-level
  serialization limit is truncated before storage (a clean cap instead of
  the misleading "Circular reference detected" ValueError); 99 levels are
  untouched.
- F5 (P5): forced merges that share an exact ID report a ``title conflict``
  warning when the normalized titles differ.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import Author, AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.deduplicator import (
    Deduplicator,
    detect_field_conflicts,
)
from academic_intelligence.storage.sqlite_store import CoauthorshipRow, SQLiteStorage

# pydantic-core refuses to serialize dict/list nesting deeper than this many
# levels ("Circular reference detected (depth exceeded)" — a depth misreport);
# 99 levels serialize fine, 100 fail (P34 V2.1).
RAW_DATA_MAX_DEPTH = 99


def _ev(source: SourceType = SourceType.OPENALEX, conf: float = 0.8) -> Evidence:
    return Evidence(source=source, source_url="https://e.com", confidence=conf)


def _paper(**kwargs: object) -> Paper:
    defaults: dict[str, object] = {
        "title": "FIX-P Paper",
        "authors": ["Ada"],
        "year": 2020,
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Paper(**defaults)  # type: ignore[arg-type]


def _deep_raw(depth: int) -> dict[str, Any]:
    """A pure dict chain nested *depth* levels (root at level 1)."""
    data: dict[str, Any] = {"leaf": "end"}
    for _ in range(depth - 1):
        data = {"inner": data}
    return data


def _max_depth(value: Any) -> int:
    """Container nesting depth of *value* (root container counts as 1)."""
    if not isinstance(value, (dict, list)):
        return 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    deepest = 0
    while stack:
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return deepest


# ---------------------------------------------------------------------------
# F1 (P4): single save_paper coauthorship aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_p_f1_save_paper_coauthorship_semantics(tmp_path: Path) -> None:
    """F1: ``save_paper`` counts coauthorship pairs like ``save_batch`` —
    once per new paper, not re-counted on idempotent re-saves or updates."""
    store = SQLiteStorage(str(tmp_path / "f1.db"))
    await store.connect()
    try:

        async def _counts() -> dict[tuple[str, str], int]:
            async with store._session() as session:
                rows = (await session.execute(select(CoauthorshipRow))).scalars().all()
            return {(r.author_a_id, r.author_b_id): r.paper_count for r in rows}

        paper = Paper(
            id="p1",
            title="Coauth",
            authors=[
                AuthorRef(author_id="A1", name="Alice", position=1),
                AuthorRef(author_id="A2", name="Bob", position=2),
                AuthorRef(author_id="A3", name="Carol", position=3),
            ],
            year=2020,
            evidence=_ev(),
        )
        await store.save_paper(paper)
        assert await _counts() == {("A1", "A2"): 1, ("A1", "A3"): 1, ("A2", "A3"): 1}

        # idempotent re-save must not double-count
        await store.save_paper(paper)
        assert await _counts() == {("A1", "A2"): 1, ("A1", "A3"): 1, ("A2", "A3"): 1}

        # update_paper rewrites edges but never re-counts
        await store.update_paper("p1", paper.model_copy(update={"title": "Coauth v2"}))
        assert await _counts() == {("A1", "A2"): 1, ("A1", "A3"): 1, ("A2", "A3"): 1}

        # a second paper sharing a pair counts once more
        paper2 = Paper(
            id="p2",
            title="Coauth 2",
            authors=[
                AuthorRef(author_id="A1", name="Alice", position=1),
                AuthorRef(author_id="A2", name="Bob", position=2),
            ],
            year=2021,
            evidence=_ev(),
        )
        await store.save_paper(paper2)
        assert await _counts() == {("A1", "A2"): 2, ("A1", "A3"): 1, ("A2", "A3"): 1}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_p_f1_save_paper_resolves_names_before_counting(
    tmp_path: Path,
) -> None:
    """F1: name-only bylines resolved against stored authors are counted with
    the resolved ids (FIX-M F1 consistency on the single-record path)."""
    store = SQLiteStorage(str(tmp_path / "f1b.db"))
    await store.connect()
    try:
        await store.save_author(Author(id="au-alice", name="Alice", evidence=_ev()))
        await store.save_author(Author(id="au-bob", name="Bob", evidence=_ev()))
        paper = Paper(
            id="p-n",
            title="Named",
            authors=[
                AuthorRef(name="Alice", position=1),
                AuthorRef(name="Bob", position=2),
            ],
            year=2020,
            evidence=_ev(),
        )
        await store.save_paper(paper)
        async with store._session() as session:
            rows = (await session.execute(select(CoauthorshipRow))).scalars().all()
        assert {(r.author_a_id, r.author_b_id): r.paper_count for r in rows} == {
            ("au-alice", "au-bob"): 1
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_p_f1_200_author_save_paper_is_fast_and_correct(
    tmp_path: Path,
) -> None:
    """F1: saving one paper with 200 resolved authors must persist the
    C(200,2)=19,900 coauthorship edges with correct counts and take seconds,
    not the ~66s of the old per-pair ``session.get`` path (P34 V2.3)."""
    store = SQLiteStorage(str(tmp_path / "f1c.db"))
    await store.connect()
    try:
        authors = [
            AuthorRef(author_id=f"A{i:04d}", name=f"Author {i}", position=i + 1) for i in range(200)
        ]
        paper = Paper(
            id="p-big",
            title="Big Collaboration",
            authors=authors,
            year=2020,
            evidence=_ev(),
        )
        start = time.perf_counter()
        await store.save_paper(paper)
        elapsed = time.perf_counter() - start

        async with store._session() as session:
            rows = (await session.execute(select(CoauthorshipRow))).scalars().all()
        assert len(rows) == 19900
        assert all(r.paper_count == 1 for r in rows)
        counts = {(r.author_a_id, r.author_b_id): r.paper_count for r in rows}
        assert counts[("A0000", "A0001")] == 1
        assert counts[("A0198", "A0199")] == 1
        assert elapsed < 15.0, (
            f"single save_paper with 200 authors took {elapsed:.1f}s "
            "(pre-fix ~66s; aggregated path must be seconds)"
        )
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F2 (P1): NUL control characters in LIKE inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_p_f2_nul_keyword_rejected(tmp_path: Path) -> None:
    """F2: a keyword / venue containing NUL is rejected with ValueError —
    SQLite ``LIKE`` follows C-string semantics and truncates the pattern at
    the first NUL, silently degrading ``keyword="\\x00"`` to a full-table
    wildcard (P34 V1.4b: 11/11 rows matched)."""
    store = SQLiteStorage(str(tmp_path / "f2.db"))
    await store.connect()
    try:
        await store.save_batch(papers=[_paper(id="a", title="AAA"), _paper(id="b", title="BBB")])
        with pytest.raises(ValueError):
            await store.query_papers(keyword="\x00")
        with pytest.raises(ValueError):
            await store.query_papers(keyword="X\x00Y")
        with pytest.raises(ValueError):
            await store.query_papers(venue="\x00")
        # a plain keyword still matches exactly as before
        assert [p.id for p in await store.query_papers(keyword="AAA")] == ["a"]
        assert [p.id for p in await store.query_papers(keyword="BBB")] == ["b"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_p_f2_existing_like_escaping_untouched(tmp_path: Path) -> None:
    """F2: the FIX-I F1 escaping behavior (``%`` / ``_`` / ``\\`` matched
    literally) is unchanged by the NUL guard."""
    store = SQLiteStorage(str(tmp_path / "f2b.db"))
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
        assert [p.id for p in await store.query_papers(keyword="dash-test")] == ["p7"]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F3 (P2): UnicodeEncodeError wrapped on the read path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_p_f3_lone_surrogate_keyword_wrapped(tmp_path: Path) -> None:
    """F3: a lone-surrogate keyword must surface as StorageError, not a bare
    UnicodeEncodeError (P34 V1.3b: the read path let it escape)."""
    store = SQLiteStorage(str(tmp_path / "f3.db"))
    await store.connect()
    try:
        await store.save_batch(papers=[_paper(id="p", title="T")])
        with pytest.raises(StorageError):
            await store.query_papers(keyword="\ud800")
        # ordinary queries are unaffected
        assert [p.id for p in await store.query_papers()] == ["p"]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F4 (P3): raw_data nesting depth guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_p_f4_99_level_raw_data_roundtrips_exactly(tmp_path: Path) -> None:
    """F4: raw_data nested exactly 99 levels (pydantic's serialization
    boundary) is stored and read back untouched — no truncation fires."""
    store = SQLiteStorage(str(tmp_path / "f4.db"))
    await store.connect()
    try:
        raw = _deep_raw(99)
        paper = Paper(
            id="p-99",
            title="Depth 99",
            authors=["Ada"],
            evidence_list=[
                Evidence(
                    source=SourceType.OPENALEX,
                    source_url="https://e.com",
                    raw_data=raw,
                )
            ],
        )
        await store.save_paper(paper)
        got = await store.get_paper("p-99")
        assert got is not None
        assert got.evidence_list[0].raw_data == raw
        got.evidence_list[0].to_dict()  # serializable
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_p_f4_100_level_raw_data_saved_truncated(tmp_path: Path) -> None:
    """F4: raw_data nested 100 levels (pydantic's failure boundary) must save
    with the over-deep tail truncated to 99 levels instead of raising the
    misleading "Circular reference detected" ValueError."""
    store = SQLiteStorage(str(tmp_path / "f4b.db"))
    await store.connect()
    try:
        raw = _deep_raw(100)
        paper = Paper(
            id="p-100",
            title="Depth 100",
            authors=["Ada"],
            evidence_list=[
                Evidence(
                    source=SourceType.OPENALEX,
                    source_url="https://e.com",
                    raw_data=raw,
                )
            ],
        )
        await store.save_paper(paper)  # must not raise
        got = await store.get_paper("p-100")
        assert got is not None
        assert _max_depth(got.evidence_list[0].raw_data) == RAW_DATA_MAX_DEPTH
        got.evidence_list[0].to_dict()  # serializable
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_p_f4_500_level_raw_data_saved_truncated(tmp_path: Path) -> None:
    """F4: raw_data nested 500 levels is truncated to the cap on save (both
    single-record and batch paths) and never breaks save/export."""
    store = SQLiteStorage(str(tmp_path / "f4c.db"))
    await store.connect()
    try:
        raw = _deep_raw(500)
        paper = Paper(
            id="p-500",
            title="Depth 500",
            authors=["Ada"],
            evidence_list=[
                Evidence(
                    source=SourceType.OPENALEX,
                    source_url="https://e.com",
                    raw_data=raw,
                )
            ],
        )
        await store.save_paper(paper)
        await store.save_batch(
            papers=[paper.model_copy(update={"id": "p-500b", "title": "Depth 500 batch"})]
        )
        for pid in ("p-500", "p-500b"):
            got = await store.get_paper(pid)
            assert got is not None
            assert _max_depth(got.evidence_list[0].raw_data) == RAW_DATA_MAX_DEPTH
            got.evidence_list[0].to_dict()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_p_f4_truncation_preserves_list_order(tmp_path: Path) -> None:
    """F4: truncating an over-deep raw_data must keep list element order and
    leave shallow siblings intact (regression: the first cut of the depth
    guard reversed list elements while capping)."""
    store = SQLiteStorage(str(tmp_path / "f4d.db"))
    await store.connect()
    try:
        deep_el: dict[str, Any] = {"leaf": "end"}
        for _ in range(119):  # 120 levels -> over the cap
            deep_el = {"inner": deep_el}
        raw: dict[str, Any] = {"items": [1, 2, deep_el, 3]}
        paper = Paper(
            id="p-list",
            title="List order",
            authors=["Ada"],
            evidence_list=[
                Evidence(
                    source=SourceType.OPENALEX,
                    source_url="https://e.com",
                    raw_data=raw,
                )
            ],
        )
        await store.save_paper(paper)
        got = await store.get_paper("p-list")
        assert got is not None
        stored = got.evidence_list[0].raw_data
        assert stored is not None
        # list order and shallow siblings preserved; the deep element capped
        assert stored["items"][0] == 1
        assert stored["items"][3] == 3
        assert _max_depth(stored) == RAW_DATA_MAX_DEPTH
        got.evidence_list[0].to_dict()
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F5 (P5): title conflict warning on ID-forced merges
# ---------------------------------------------------------------------------


def test_fix_p_f5_title_conflict_warned_on_same_doi() -> None:
    """P5: two records with the same DOI but normalized-different titles merge
    (exact-ID rule wins) and surface a ``title conflict`` warning."""
    a = Paper(
        title="The Real Title",
        authors=["A"],
        year=2020,
        doi="10.1234/xyz",
        evidence=_ev(SourceType.OPENALEX, 0.9),
    )
    b = Paper(
        title="A Completely Different Title",
        authors=["B"],
        year=2020,
        doi="10.1234/xyz",
        evidence=_ev(SourceType.SEMANTIC_SCHOLAR, 0.85),
    )
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers([a, b])
    assert len(merged) == 1
    warnings = dedup.pop_warnings()
    assert any("title conflict" in w for w in warnings)
    assert any("openalex=The Real Title" in w for w in warnings)


def test_fix_p_f5_same_title_no_title_conflict() -> None:
    """P5: same DOI with titles that only differ in case/punctuation (equal
    after normalization) must not raise a title-conflict warning."""
    a = Paper(
        title="Attention Is All You Need",
        authors=["A"],
        year=2017,
        doi="10.5555/3295222.3295349",
        evidence=_ev(SourceType.OPENALEX, 0.9),
    )
    b = Paper(
        title="Attention is all you need!",
        authors=["B"],
        year=2017,
        doi="10.5555/3295222.3295349",
        evidence=_ev(SourceType.SEMANTIC_SCHOLAR, 0.85),
    )
    dedup = Deduplicator()
    merged = dedup.deduplicate_papers([a, b])
    assert len(merged) == 1
    assert not any("title conflict" in w for w in dedup.get_warnings())


def test_fix_p_f5_title_conflict_only_for_shared_id() -> None:
    """P5: a title difference is only a conflict when the merge is forced by a
    shared exact ID — title-similarity merges gate on title similarity
    themselves and must not report it."""
    a = Paper(
        title="Title Alpha",
        authors=["A"],
        year=2020,
        evidence=_ev(SourceType.OPENALEX, 0.9),
    )
    b = Paper(
        title="Title Beta",
        authors=["B"],
        year=2020,
        evidence=_ev(SourceType.SEMANTIC_SCHOLAR, 0.85),
    )
    # no shared id: different titles are not a conflict
    assert detect_field_conflicts([a, b]) == []
    # shared DOI: the same title difference now is a conflict
    c = a.model_copy(update={"doi": "10.1234/shared"})
    d = b.model_copy(update={"doi": "10.1234/shared"})
    warnings = detect_field_conflicts([c, d])
    assert any("title conflict" in w for w in warnings)
