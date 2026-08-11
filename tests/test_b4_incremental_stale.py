"""B4: incremental stale-refresh strategy (3A v2 §9).

A re-pull only happens when the recorded last-update time is older than the
configured refresh window (``paper_refresh_days`` / ``author_refresh_days``);
a fresh second update within the window is skipped as unchanged.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.models import CollectionResult, Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.processors.incremental import (
    IncrementalProcessor,
    author_entity_key,
)
from academic_intelligence.storage.json_store import JSONStorage


def _ev() -> Evidence:
    return Evidence(source=SourceType.OPENALEX, source_url="https://e.com", confidence=0.8)


def _paper(**kwargs: object) -> Paper:
    defaults: dict[str, object] = {
        "title": "Stale Strategy Paper",
        "authors": ["Ada"],
        "year": 2020,
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Paper(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# IncrementalProcessor.is_stale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_stale_without_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            proc = IncrementalProcessor(store)
            assert await proc.is_stale(["openalex"], refresh_days=7) is True
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_is_stale_fresh_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            await store.save_last_update_time(
                "openalex", datetime.now(timezone.utc)
            )
            proc = IncrementalProcessor(store)
            assert await proc.is_stale(["openalex"], refresh_days=7) is False
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_is_stale_expired_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            await store.save_last_update_time(
                "openalex", datetime.now(timezone.utc) - timedelta(days=30)
            )
            proc = IncrementalProcessor(store)
            assert await proc.is_stale(["openalex"], refresh_days=7) is True
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_is_stale_any_source_triggers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            await store.save_last_update_time(
                "openalex", datetime.now(timezone.utc)
            )
            await store.save_last_update_time(
                "arxiv", datetime.now(timezone.utc) - timedelta(days=40)
            )
            proc = IncrementalProcessor(store)
            assert await proc.is_stale(["openalex", "arxiv"], refresh_days=7) is True
            assert await proc.is_stale(["openalex"], refresh_days=7) is False
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_is_stale_handles_naive_utc_timestamps() -> None:
    """SQLite stores naive UTC datetimes; stale check must treat them as UTC."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            await store.save_last_update_time(
                "openalex", datetime.now(timezone.utc).replace(tzinfo=None)
            )
            proc = IncrementalProcessor(store)
            assert await proc.is_stale(["openalex"], refresh_days=7) is False
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_is_stale_empty_sources() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            proc = IncrementalProcessor(store)
            assert await proc.is_stale([], refresh_days=7) is True
        finally:
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector",
    [["all"], ["*"], ["openalex", "all"], ["*", "openalex"]],
)
async def test_source_names_expands_all_aliases_to_active_sources(
    tmp_path: Path,
    selector: list[str],
) -> None:
    ai = AcademicIntelligence(
        Config(
            sources=["openalex", "arxiv"],
            storage_type="json",
            storage_path=str(tmp_path / "source-names"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        assert ai._source_names(selector) == ["openalex", "arxiv"]
    finally:
        await ai.close()


# ---------------------------------------------------------------------------
# Facade: update_author_papers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_author_papers_first_sync_pulls(tmp_path: Path) -> None:
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "a1"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        calls: list[str] = []

        async def fake_collect(name: str, **kwargs: object) -> CollectionResult:
            calls.append(name)
            return CollectionResult(
                papers=[_paper(title=f"Paper of {name}")],
                authors=[],
            )

        ai.collect_author_papers = fake_collect  # type: ignore[method-assign]
        result = await ai.update_author_papers("Ada", sources=["openalex"])
        assert calls == ["Ada"]  # first sync re-pulls
        assert result.total_checked == 1
        assert result.new
        # sync state was written
        last = await ai.storage.get_last_update_time("openalex")
        assert last is not None
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_update_author_papers_skips_when_fresh(tmp_path: Path) -> None:
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "a2"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        # Freshness is gated per (entity, source) (I-5): seed this author's
        # own entity sync row so the update is skipped.
        await ai.storage.save_entity_sync(
            "author", author_entity_key("Ada"), "openalex", datetime.now(timezone.utc)
        )
        calls: list[str] = []

        async def fake_collect(name: str, **kwargs: object) -> CollectionResult:
            calls.append(name)
            return CollectionResult(papers=[_paper()])

        ai.collect_author_papers = fake_collect  # type: ignore[method-assign]
        result = await ai.update_author_papers("Ada", sources=["openalex"])
        assert calls == []  # not stale -> no re-pull
        assert result.total_checked == 0
        assert result.new == []
        assert result.updated == []
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_update_author_papers_repulls_when_expired(tmp_path: Path) -> None:
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "a3"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        await ai.storage.save_last_update_time(
            "openalex", datetime.now(timezone.utc) - timedelta(days=30)
        )
        calls: list[str] = []

        async def fake_collect(name: str, **kwargs: object) -> CollectionResult:
            calls.append(name)
            return CollectionResult(
                papers=[_paper(title="Fresh paper")],
                authors=[],
            )

        ai.collect_author_papers = fake_collect  # type: ignore[method-assign]
        result = await ai.update_author_papers("Ada", sources=["openalex"])
        assert calls == ["Ada"]  # expired -> re-pull
        assert result.total_checked == 1
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_update_author_papers_skips_after_own_sync(tmp_path: Path) -> None:
    """Second immediate update after a real first sync is skipped (unchanged)."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "a4"),
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
async def test_update_author_papers_all_alias_skips_after_own_sync(
    tmp_path: Path,
) -> None:
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "a-all"),
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

        first = await ai.update_author_papers("Ada", sources=["all"])
        assert calls == ["Ada"]
        assert first.total_checked == 1

        second = await ai.update_author_papers("Ada", sources=["all"])
        assert calls == ["Ada"]
        assert second.total_checked == 0
        assert second.sources_used == ["openalex"]
    finally:
        await ai.close()


# ---------------------------------------------------------------------------
# Facade: update_paper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_paper_skips_when_fresh(tmp_path: Path) -> None:
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "p1"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        pid = await ai.storage.save_paper(_paper(id="p-stored"))
        # Freshness is gated per (entity, source) (I-5): seed this paper's
        # own entity sync row so the update is skipped.
        await ai.storage.save_entity_sync(
            "paper", pid, "openalex", datetime.now(timezone.utc)
        )
        calls: list[str] = []

        async def fake_collect(query: str, **kwargs: object) -> CollectionResult:
            calls.append(query)
            return CollectionResult(papers=[_paper(id="p-stored")])

        ai.collect_paper = fake_collect  # type: ignore[method-assign]
        result = await ai.update_paper(pid, sources=["openalex"])
        assert calls == []
        assert result.total_checked == 0
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_update_paper_repulls_when_expired(tmp_path: Path) -> None:
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "p2"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        pid = await ai.storage.save_paper(_paper(id="p-stored", citations=1))
        await ai.storage.save_last_update_time(
            "openalex", datetime.now(timezone.utc) - timedelta(days=30)
        )
        calls: list[str] = []

        async def fake_collect(query: str, **kwargs: object) -> CollectionResult:
            calls.append(query)
            return CollectionResult(papers=[_paper(id="p-stored", citations=99)])

        ai.collect_paper = fake_collect  # type: ignore[method-assign]
        result = await ai.update_paper(pid, sources=["openalex"])
        assert len(calls) == 1  # expired -> re-pull
        assert result.total_checked == 1
    finally:
        await ai.close()


# ---------------------------------------------------------------------------
# FIX-H F4 (H5): failed/empty collections must not write entity_sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_author_papers_does_not_sync_when_collection_empty(
    tmp_path: Path,
) -> None:
    """H5: ``update_author_papers("No Such Person")`` with a fully-failed
    collection must NOT write entity_sync (the stale gate would otherwise lock
    the misspelled entity out of retries for refresh_days), and the source
    errors must stay observable on the result."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "h5"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        calls: list[str] = []

        async def fake_collect(name: str, **kwargs: object) -> CollectionResult:
            calls.append(name)
            return CollectionResult(errors=["openalex: 404 not found"])

        ai.collect_author_papers = fake_collect  # type: ignore[method-assign]
        result = await ai.update_author_papers("No Such Person", sources=["openalex"])

        assert result.total_checked == 0
        # entity_sync must NOT be recorded for a fully-failed collection
        last = await ai.storage.get_entity_sync(
            "author", author_entity_key("No Such Person"), "openalex"
        )
        assert last is None
        # the source failure is observable via warnings
        assert "openalex: 404 not found" in result.warnings

        # a second update is NOT short-circuited by the stale gate -> retried
        second = await ai.update_author_papers("No Such Person", sources=["openalex"])
        assert calls == ["No Such Person", "No Such Person"]
        assert second.total_checked == 0
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_update_paper_does_not_sync_when_collection_empty(
    tmp_path: Path,
) -> None:
    """H5: same gating for ``update_paper`` — a failed collection neither
    records entity_sync nor masks the source error."""
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "h5p"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        pid = await ai.storage.save_paper(_paper(id="p-h5", title="Stored"))
        calls: list[str] = []

        async def fake_collect(query: str, **kwargs: object) -> CollectionResult:
            calls.append(query)
            return CollectionResult(errors=["openalex: 404 not found"])

        ai.collect_paper = fake_collect  # type: ignore[method-assign]
        result = await ai.update_paper(pid, sources=["openalex"])

        assert result.total_checked == 0
        last = await ai.storage.get_entity_sync("paper", pid, "openalex")
        assert last is None
        assert "openalex: 404 not found" in result.warnings

        # retry is not gated by a phantom sync timestamp
        second = await ai.update_paper(pid, sources=["openalex"])
        assert len(calls) == 2
        assert second.total_checked == 0
    finally:
        await ai.close()
