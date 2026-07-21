"""Unit tests for incremental update mechanism."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from academic_intelligence.core.models import (
    ChangeDetection,
    ChangeType,
    Evidence,
    IncrementalUpdateResult,
    Paper,
)
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.incremental import IncrementalProcessor
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(
    source: SourceType = SourceType.SEMANTIC_SCHOLAR,
    conf: float = 0.8,
) -> Evidence:
    return Evidence(
        source=source,
        source_url="https://example.com/p",
        confidence=conf,
    )


def _paper(**kwargs) -> Paper:
    defaults = {
        "title": "Attention Is All You Need",
        "authors": ["Vaswani", "Shazeer"],
        "year": 2017,
        "venue": "NeurIPS",
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Paper(**defaults)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


def test_change_type_values() -> None:
    assert ChangeType.NEW == "new"
    assert ChangeType.UPDATED == "updated"
    assert ChangeType.UNCHANGED == "unchanged"
    assert ChangeType.DELETED == "deleted"


def test_change_detection_roundtrip() -> None:
    cd = ChangeDetection(
        paper_id="p1",
        change_type=ChangeType.UPDATED,
        changed_fields=["citations"],
        old_values={"citations": 10},
        new_values={"citations": 20},
        confidence_delta=0.1,
    )
    restored = ChangeDetection.from_dict(cd.to_dict())
    assert restored.paper_id == "p1"
    assert restored.changed_fields == ["citations"]
    assert restored.confidence_delta == pytest.approx(0.1)


def test_incremental_update_result_json() -> None:
    result = IncrementalUpdateResult(
        new=[_paper(id="n1")],
        updated=[],
        unchanged=["u1"],
        total_checked=2,
        sources_used=["semantic_scholar"],
    )
    raw = result.to_json()
    restored = IncrementalUpdateResult.from_json(raw)
    assert restored.total_checked == 2
    assert restored.unchanged == ["u1"]
    assert len(restored.new) == 1


# ---------------------------------------------------------------------------
# Hash & compare
# ---------------------------------------------------------------------------


def test_calculate_hash_stable() -> None:
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    p1 = _paper()
    p2 = _paper()
    assert proc._calculate_hash(p1) == proc._calculate_hash(p2)
    assert len(proc._calculate_hash(p1)) == 16


def test_calculate_hash_changes_with_title() -> None:
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    a = _paper(title="Paper A")
    b = _paper(title="Paper B")
    assert proc._calculate_hash(a) != proc._calculate_hash(b)


def test_compare_papers_unchanged() -> None:
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    old = _paper(id="p1", abstract="abs", citations=10)
    new = _paper(id="p1", abstract="abs", citations=10)
    det = proc._compare_papers(old, new)
    assert det.change_type == ChangeType.UNCHANGED
    assert det.changed_fields == []


def test_compare_papers_updated_fields() -> None:
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    old = _paper(id="p1", citations=10, abstract="old", evidence=_ev(conf=0.7))
    new = _paper(id="p1", citations=50, abstract="new", evidence=_ev(conf=0.9))
    det = proc._compare_papers(old, new)
    assert det.change_type == ChangeType.UPDATED
    assert "citations" in det.changed_fields
    assert "abstract" in det.changed_fields
    assert det.old_values["citations"] == 10
    assert det.new_values["citations"] == 50
    assert det.confidence_delta == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# detect_changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_new_papers() -> None:
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    old: list[Paper] = []
    new = [_paper(id="n1"), _paper(id="n2", title="Other Paper", doi="10.1234/x.y")]
    result = await proc.detect_changes(new, old)
    assert len(result.new) == 2
    assert result.updated == []
    assert result.unchanged == []
    assert result.total_checked == 2


@pytest.mark.asyncio
async def test_detect_unchanged_by_hash() -> None:
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    old = [_paper(id="p1", abstract="same", citations=5)]
    new = [_paper(id="p1", abstract="same", citations=5)]
    result = await proc.detect_changes(new, old)
    assert result.new == []
    assert result.updated == []
    assert "p1" in result.unchanged


@pytest.mark.asyncio
async def test_detect_updated_by_doi_match() -> None:
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    old = [
        _paper(
            id="stored-1",
            doi="10.5555/3295222.3295349",
            citations=100,
            evidence=_ev(conf=0.7),
        )
    ]
    new = [
        _paper(
            id=None,
            doi="10.5555/3295222.3295349",
            citations=250,
            abstract="Full abstract now available",
            evidence=_ev(SourceType.OPENALEX, conf=0.9),
        )
    ]
    result = await proc.detect_changes(new, old)
    assert result.new == []
    assert len(result.updated) == 1
    det = result.updated[0]
    assert det.change_type == ChangeType.UPDATED
    assert "citations" in det.changed_fields or "abstract" in det.changed_fields
    assert det.paper_id == "stored-1"


@pytest.mark.asyncio
async def test_detect_mixed_new_updated_unchanged() -> None:
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    old = [
        _paper(id="p1", title="Paper One", year=2020, citations=1),
        _paper(id="p2", title="Paper Two", year=2021, citations=5),
    ]
    new = [
        _paper(id="p1", title="Paper One", year=2020, citations=1),  # unchanged
        _paper(id="p2", title="Paper Two", year=2021, citations=99),  # updated
        _paper(id="p3", title="Paper Three", year=2022),  # new
    ]
    result = await proc.detect_changes(new, old)
    assert len(result.new) == 1
    assert result.new[0].id == "p3"
    assert len(result.updated) == 1
    assert result.updated[0].paper_id == "p2"
    assert "p1" in result.unchanged
    assert result.total_checked == 3


# ---------------------------------------------------------------------------
# apply_changes (with real storage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_changes_json_storage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            proc = IncrementalProcessor(store)

            # Seed one existing paper
            old = _paper(id="p1", title="Paper One", year=2020, citations=1)
            await store.save_paper(old)
            await store.save_paper_hash("p1", proc._calculate_hash(old))

            new_papers = [
                _paper(id="p1", title="Paper One", year=2020, citations=1),  # unch
                _paper(
                    id="p1",
                    title="Paper One",
                    year=2020,
                    citations=50,
                    abstract="new abs",
                    evidence=_ev(conf=0.95),
                ),  # will match as update if we only pass updated
                _paper(id=None, title="Brand New", year=2023, doi="10.1234/brand.new"),
            ]
            # Use detect properly
            old_list = [await store.get_paper("p1")]
            assert old_list[0] is not None

            # Separate: only one new with higher citations for p1 + brand new
            collected = [
                _paper(
                    id="p1",
                    title="Paper One",
                    year=2020,
                    citations=50,
                    abstract="new abs",
                    evidence=_ev(conf=0.95),
                ),
                _paper(
                    title="Brand New",
                    year=2023,
                    doi="10.1234/brand.new",
                    authors=["Ada"],
                ),
            ]
            result = await proc.detect_changes(collected, [old_list[0]])
            assert len(result.new) == 1
            assert len(result.updated) == 1

            counts = await proc.apply_changes(result)
            assert counts["new"] == 1
            assert counts["updated"] == 1

            updated = await store.get_paper("p1")
            assert updated is not None
            assert updated.citations == 50
            assert updated.abstract == "new abs"

            # New paper inserted
            papers = await store.query_papers(keyword="Brand")
            assert len(papers) == 1

            # Hash stored
            h = await store.get_paper_hash("p1")
            assert h is not None
            assert len(h) == 16
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_apply_skips_unchanged_no_extra_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            proc = IncrementalProcessor(store)
            paper = _paper(id="p1", title="Stable Paper", year=2019, citations=3)
            await store.save_paper(paper)

            result = await proc.detect_changes([paper], [paper])
            assert result.unchanged == ["p1"]
            assert result.new == []
            assert result.updated == []

            counts = await proc.apply_changes(result)
            assert counts["unchanged"] == 1
            assert counts["new"] == 0
            assert counts["updated"] == 0
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_merge_prefers_higher_confidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            proc = IncrementalProcessor(store)
            old = _paper(
                id="p1",
                title="Merge Test",
                year=2020,
                venue="Old Venue",
                abstract="Keep me if conf higher",
                citations=10,
                evidence=_ev(SourceType.SEMANTIC_SCHOLAR, conf=0.95),
            )
            await store.save_paper(old)

            new = _paper(
                id="p1",
                title="Merge Test",
                year=2020,
                venue="New Venue Better Source",
                abstract=None,
                citations=100,
                evidence=_ev(SourceType.OPENALEX, conf=0.5),
            )
            result = await proc.detect_changes([new], [old])
            assert len(result.updated) == 1
            await proc.apply_changes(result)

            merged = await store.get_paper("p1")
            assert merged is not None
            # Higher conf old keeps abstract; citations take max
            assert merged.abstract == "Keep me if conf higher"
            assert merged.citations == 100
            # New has higher conf? No, old 0.95 > new 0.5, so venue from old
            # unless pick prefers non-empty from primary (old)
            assert merged.venue == "Old Venue"
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_sqlite_hash_and_update_time() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "test.db")
        store = SQLiteStorage(db)
        await store.connect()
        try:
            paper = _paper(id="sq1", title="SQLite Paper")
            await store.save_paper(paper)
            await store.save_paper_hash("sq1", "abcd1234efgh5678")
            assert await store.get_paper_hash("sq1") == "abcd1234efgh5678"

            now = datetime.now(timezone.utc)
            await store.save_last_update_time("semantic_scholar", now)
            got = await store.get_last_update_time("semantic_scholar")
            assert got is not None

            assert await store.get_last_update_time("unknown_source") is None
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_json_hash_and_update_time() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            await store.save_paper_hash("j1", "hashvalue1234567")
            assert await store.get_paper_hash("j1") == "hashvalue1234567"

            now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
            await store.save_last_update_time("openalex", now)
            got = await store.get_last_update_time("openalex")
            assert got is not None
            assert got.year == 2026
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_detect_fuzzy_title_match() -> None:
    proc = IncrementalProcessor(storage=None)  # type: ignore[arg-type]
    old = [_paper(id="p1", title="Deep Learning for NLP", authors=["Smith"], year=2020)]
    new = [
        _paper(
            title="Deep Learning for NLP",
            authors=["A. Smith"],
            year=2020,
            citations=42,
        )
    ]
    result = await proc.detect_changes(new, old)
    # Should match by hash or fuzzy and detect citation update
    assert len(result.new) == 0
    assert len(result.updated) == 1 or "p1" in result.unchanged or len(result.updated) == 1
    # citations changed so updated
    assert len(result.updated) == 1
    assert result.updated[0].paper_id == "p1"


def test_import_incremental_processor() -> None:
    from academic_intelligence.processors import IncrementalProcessor as IP
    from academic_intelligence.processors.incremental import IncrementalProcessor as IP2

    assert IP is IP2


def test_import_models_from_package() -> None:
    from academic_intelligence import ChangeType, IncrementalUpdateResult

    assert ChangeType.NEW.value == "new"
    assert IncrementalUpdateResult(total_checked=0).total_checked == 0
