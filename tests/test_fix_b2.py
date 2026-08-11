"""FIX-B2: entity-dimension incremental gating + normalized author matching +
composite confidence persistence (I-5 / I-6).

Covers:
- F1: ``(entity, source)`` incremental gating — syncing author/paper A never
  blocks a never-synced author/paper B; per-entity freshness still skips.
- F2: ``query_papers(author=...)`` matches stored names with middle initials
  (``"Geoffrey Hinton"`` -> ``"Geoffrey E. Hinton"``); expired refresh loads
  old papers so ``unchanged`` is non-empty.
- F3: composite confidence survives ``to_dict()/from_dict()`` and the JSON
  backend ``save``/``load`` round-trip.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    CollectionResult,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.processors.incremental import (
    IncrementalProcessor,
    author_entity_key,
)
from academic_intelligence.processors.scorer import ConfidenceScorer
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(
    source: SourceType = SourceType.OPENALEX, conf: float = 0.8
) -> Evidence:
    return Evidence(source=source, source_url="https://e.com", confidence=conf)


def _paper(**kwargs: object) -> Paper:
    defaults: dict[str, object] = {
        "title": "FIX-B2 Paper",
        "authors": ["Ada"],
        "year": 2020,
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Paper(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# F1: (entity, source) incremental gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_stale_entity_dimension() -> None:
    """Entity-level gating is per (entity, source); source-level path is kept."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            await store.save_entity_sync(
                "author", "ada", "openalex", datetime.now(UTC)
            )
            proc = IncrementalProcessor(store)
            assert (
                await proc.is_stale(
                    ["openalex"],
                    refresh_days=7,
                    entity_type="author",
                    entity_id="ada",
                )
                is False
            )
            # never-synced entity is stale even though the source was synced
            assert (
                await proc.is_stale(
                    ["openalex"],
                    refresh_days=7,
                    entity_type="author",
                    entity_id="bob",
                )
                is True
            )
            # no entity -> legacy source-level semantics (never synced here)
            assert await proc.is_stale(["openalex"], refresh_days=7) is True
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_author_b_never_synced_not_blocked_by_author_a_sync(
    tmp_path: Path,
) -> None:
    """I-5: syncing author A must not gate a never-synced author B."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "f1a"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        calls: list[str] = []

        async def fake_collect(name: str, **kwargs: object) -> CollectionResult:
            calls.append(name)
            return CollectionResult(papers=[_paper(title=f"Paper of {name}")])

        ai.collect_author_papers = fake_collect  # type: ignore[method-assign]
        first = await ai.update_author_papers("Ada", sources=["openalex"])
        assert calls == ["Ada"]
        assert first.total_checked == 1

        # B was never synced as an entity -> must NOT be skipped
        second = await ai.update_author_papers("Bob", sources=["openalex"])
        assert calls == ["Ada", "Bob"]
        assert second.total_checked == 1
        assert second.new
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_same_author_second_sync_within_window_skipped(
    tmp_path: Path,
) -> None:
    """A 7-day second update of the same author is still skipped."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "f1b"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        calls: list[str] = []

        async def fake_collect(name: str, **kwargs: object) -> CollectionResult:
            calls.append(name)
            return CollectionResult(papers=[_paper(title=f"Paper of {name}")])

        ai.collect_author_papers = fake_collect  # type: ignore[method-assign]
        first = await ai.update_author_papers("Ada", sources=["openalex"])
        assert calls == ["Ada"]
        assert first.total_checked == 1

        second = await ai.update_author_papers("Ada", sources=["openalex"])
        assert calls == ["Ada"]  # no second pull
        assert second.total_checked == 0
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_author_entity_sync_rows_written(tmp_path: Path) -> None:
    """update_author_papers records the (entity, source) sync timestamp."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "f1c"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:

        async def fake_collect(name: str, **kwargs: object) -> CollectionResult:
            return CollectionResult(papers=[_paper(title=f"Paper of {name}")])

        ai.collect_author_papers = fake_collect  # type: ignore[method-assign]
        await ai.update_author_papers("Geoffrey Hinton", sources=["openalex"])
        last = await ai.storage.get_entity_sync(
            "author", author_entity_key("Geoffrey Hinton"), "openalex"
        )
        assert last is not None
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_paper_b_not_blocked_by_paper_a_sync(tmp_path: Path) -> None:
    """I-5 for update_paper: syncing paper A must not gate paper B."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "f1p"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        pid_a = await ai.storage.save_paper(_paper(id="p-a", title="Paper A"))
        pid_b = await ai.storage.save_paper(_paper(id="p-b", title="Paper B"))
        calls: list[str] = []

        async def fake_collect(query: str, **kwargs: object) -> CollectionResult:
            calls.append(query)
            return CollectionResult(papers=[_paper(id=query, title=query)])

        ai.collect_paper = fake_collect  # type: ignore[method-assign]
        first = await ai.update_paper(pid_a, sources=["openalex"])
        assert calls == ["Paper A"]
        assert first.total_checked == 1

        # B is a distinct paper entity -> must NOT be skipped by A's sync
        second = await ai.update_paper(pid_b, sources=["openalex"])
        assert calls == ["Paper A", "Paper B"]
        assert second.total_checked == 1
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_entity_sync_sqlite_roundtrip(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "es.db"))
    await store.connect()
    try:
        now = datetime.now(UTC)
        await store.save_entity_sync("paper", "p1", "openalex", now)
        got = await store.get_entity_sync("paper", "p1", "openalex")
        assert got is not None
        assert await store.get_entity_sync("paper", "p2", "openalex") is None
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# F2: normalized author matching (query layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_papers_normalized_author_match_both_backends(
    tmp_path: Path,
) -> None:
    """'Geoffrey Hinton' must match a stored 'Geoffrey E. Hinton' byline."""
    stores = [
        JSONStorage(str(tmp_path / "qj")),
        SQLiteStorage(str(tmp_path / "qs.db")),
    ]
    for store in stores:
        await store.connect()
        try:
            p = _paper(
                title="Deep Learning",
                authors=[AuthorRef(name="Geoffrey E. Hinton", position=1)],
            )
            await store.save_paper(p)
            assert len(await store.query_papers(author="Geoffrey Hinton")) == 1
            assert len(await store.query_papers(author="Hinton")) == 1
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_expired_refresh_loads_old_papers_via_normalized_name(
    tmp_path: Path,
) -> None:
    """I-5: expired refresh loads old papers by normalized name, so the
    refresh does not re-classify everything as new."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "f2"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        old = _paper(
            id="w1",
            title="Deep Learning",
            authors=[AuthorRef(name="Geoffrey E. Hinton", position=1)],
            year=2015,
        )
        await ai.storage.save_paper(old)
        stale = datetime.now(UTC) - timedelta(days=30)
        await ai.storage.save_last_update_time("openalex", stale)
        await ai.storage.save_entity_sync(
            "author", author_entity_key("Geoffrey Hinton"), "openalex", stale
        )

        async def fake_collect(name: str, **kwargs: object) -> CollectionResult:
            return CollectionResult(papers=[old])  # same paper re-collected

        ai.collect_author_papers = fake_collect  # type: ignore[method-assign]
        result = await ai.update_author_papers("Geoffrey Hinton", sources=["openalex"])
        assert result.total_checked == 1
        assert "w1" in result.unchanged
        assert result.new == []
    finally:
        await ai.close()


# ---------------------------------------------------------------------------
# F3: composite confidence persistence
# ---------------------------------------------------------------------------


def test_composite_confidence_roundtrips_via_dict() -> None:
    """I-6: score_paper composite survives to_dict()/from_dict()."""
    evs = [_ev(SourceType.OPENALEX, 0.88), _ev(SourceType.ARXIV, 0.95)]
    scored = ConfidenceScorer().score_paper(
        Paper(title="T", doi="10.1234/x.y", evidence_list=evs)
    )
    assert scored.primary_evidence is not None
    assert scored.primary_evidence.confidence == pytest.approx(1.0)

    restored = Paper.from_dict(scored.to_dict())
    assert restored.primary_evidence is not None
    assert restored.primary_evidence.confidence == pytest.approx(1.0)


def test_author_composite_confidence_roundtrips_via_dict() -> None:
    author = Author(
        name="Ada",
        evidence_list=[
            _ev(SourceType.IEEE, 0.85),
            _ev(SourceType.OPENALEX, 0.9),
        ],
    )
    scored = ConfidenceScorer().score_author(author)
    assert scored.primary_evidence is not None
    restored = Author.from_dict(scored.to_dict())
    assert restored.primary_evidence is not None
    assert restored.primary_evidence.confidence == pytest.approx(
        scored.primary_evidence.confidence
    )


@pytest.mark.asyncio
async def test_composite_confidence_json_save_load(tmp_path: Path) -> None:
    """I-6: JSON backend save/load keeps the composite confidence."""
    store = JSONStorage(str(tmp_path / "f3"))
    await store.connect()
    try:
        evs = [_ev(SourceType.OPENALEX, 0.88), _ev(SourceType.ARXIV, 0.95)]
        scored = ConfidenceScorer().score_paper(
            Paper(title="T", doi="10.1234/x.y", evidence_list=evs)
        )
        pid = await store.save_paper(scored)
        got = await store.get_paper(pid)
        assert got is not None
        assert got.primary_evidence is not None
        assert got.primary_evidence.confidence == pytest.approx(1.0)
    finally:
        await store.close()
