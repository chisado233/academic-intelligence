"""Round-2 review regressions for consistency, wiring, and durability."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.models import AuthorRef, Citation, Evidence, Paper
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.processors.incremental import IncrementalProcessor
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from academic_intelligence.utils.cache import Cache


def _evidence(confidence: float = 0.8) -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W1",
        confidence=confidence,
    )


def _paper(
    paper_id: str,
    *,
    citations: int = 1,
    authors: list[AuthorRef] | None = None,
) -> Paper:
    return Paper(
        id=paper_id,
        title="Stable Identity",
        doi="10.5555/stable.identity",
        citations=citations,
        authors=authors or [AuthorRef(name="Ada", author_id="a", position=1)],
        evidence=_evidence(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["json", "sqlite"])
async def test_matched_incremental_update_keeps_storage_id(
    tmp_path: Path, backend: str
) -> None:
    store = (
        JSONStorage(str(tmp_path / "identity-json"))
        if backend == "json"
        else SQLiteStorage(str(tmp_path / "identity.db"))
    )
    await store.connect()
    try:
        await store.save_paper(_paper("stored-id", citations=1))
        old = await store.get_paper("stored-id")
        assert old is not None

        processor = IncrementalProcessor(store)
        result = await processor.detect_changes(
            [_paper("source-id", citations=2)], [old]
        )

        assert len(result.updated) == 1
        assert result.updated[0].paper_id == "stored-id"
        await processor.apply_changes(result)

        stats = await store.get_stats()
        assert stats["total_papers"] == 1
        assert await store.get_paper("source-id") is None
        updated = await store.get_paper("stored-id")
        assert updated is not None
        assert updated.citations == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_citation_pair_is_idempotent_for_single_and_batch_writes(
    tmp_path: Path,
) -> None:
    store = JSONStorage(str(tmp_path / "citations"))
    await store.connect()
    try:
        citation = Citation(
            citing_paper_id="p1", cited_paper_id="p2", evidence=_evidence()
        )
        first = await store.save_citation(citation)
        second = await store.save_citation(citation)
        batch = await store.save_batch(citations=[citation, citation])

        assert first == second
        assert batch["citations"] == [first, first]
        assert len(await store.get_citations_by_paper("p1")) == 1
        assert (await store.get_stats())["total_citations"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_coauthorship_is_recomputed_on_replay_and_author_change(
    tmp_path: Path,
) -> None:
    store = JSONStorage(str(tmp_path / "coauthors"))
    await store.connect()
    try:
        ab = [
            AuthorRef(name="Ada", author_id="a", position=1),
            AuthorRef(name="Bob", author_id="b", position=2),
        ]
        ac = [
            AuthorRef(name="Ada", author_id="a", position=1),
            AuthorRef(name="Chen", author_id="c", position=2),
        ]
        await store.save_batch(papers=[_paper("p1", authors=ab)])
        await store.save_batch(papers=[_paper("p1", authors=ab)])
        assert store._coauthorships == {
            "a|b": {"paper_count": 1, "first_year": None, "last_year": None}
        }

        await store.save_batch(papers=[_paper("p1", authors=ac)])
        assert "a|b" not in store._coauthorships
        assert store._coauthorships == {
            "a|c": {"paper_count": 1, "first_year": None, "last_year": None}
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_network_config_controls_are_wired(tmp_path: Path) -> None:
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "network-config"),
            cache_enabled=False,
            rate_limit=7.5,
            max_concurrent_requests=2,
        )
    )
    await ai.connect()
    try:
        assert ai._http is not None
        assert ai._http._rate_limiter.config.requests_per_second == 7.5
        assert ai._http._request_semaphore._value == 2
    finally:
        await ai.close()


def test_google_scholar_enable_flag_controls_registration() -> None:
    disabled = AcademicIntelligence(
        Config(sources=["openalex", "google_scholar"], enable_google_scholar=False)
    )
    enabled = AcademicIntelligence(
        Config(sources=["openalex", "google_scholar"], enable_google_scholar=True)
    )

    assert set(disabled._build_sources(disabled.config.sources)) == {"openalex"}
    assert set(enabled._build_sources(enabled.config.sources)) == {
        "openalex",
        "google_scholar",
    }


@pytest.mark.asyncio
async def test_author_updates_use_author_refresh_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    async def not_stale(
        self: IncrementalProcessor,
        sources: list[str],
        *,
        refresh_days: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> bool:
        seen.append(refresh_days)
        return False

    monkeypatch.setattr(IncrementalProcessor, "is_stale", not_stale)
    ai = AcademicIntelligence(
        Config(
            sources=["openalex"],
            storage_type="json",
            storage_path=str(tmp_path / "author-refresh"),
            author_refresh_days=61,
            paper_refresh_days=7,
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        await ai.update_author_papers("Ada", sources=["openalex"])
        assert seen == [61]
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_json_uses_atomic_snapshot_and_delegates_disk_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import academic_intelligence.storage.json_store as json_store_module

    store = JSONStorage(str(tmp_path / "snapshot"))
    await store.connect()
    original_to_thread = asyncio.to_thread
    calls: list[str] = []

    async def tracking_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(func, "__name__", repr(func)))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(json_store_module.asyncio, "to_thread", tracking_to_thread)
    try:
        await store.save_paper(_paper("p1"))
        assert (store.base_path / "store.json").exists()
        assert calls, "async persistence must delegate blocking disk I/O"
        assert not list(store.base_path.glob("*.tmp-*"))
    finally:
        await store.close()

    # The atomically replaced single snapshot is authoritative even if a
    # legacy mirror is later corrupted.
    (store.base_path / "papers.json").write_text("{broken", encoding="utf-8")
    reopened = JSONStorage(str(store.base_path))
    await reopened.connect()
    try:
        assert await reopened.get_paper("p1") is not None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_persistent_cache_is_single_flight_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import academic_intelligence.utils.cache as cache_module

    original_to_thread = asyncio.to_thread
    thread_calls: list[str] = []

    async def tracking_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        thread_calls.append(getattr(func, "__name__", repr(func)))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(cache_module.asyncio, "to_thread", tracking_to_thread)
    path = tmp_path / "cache.json"
    cache = Cache(ttl=60, persistent=True, persist_path=path)
    factory_calls = 0

    async def factory() -> dict[str, bool]:
        nonlocal factory_calls
        factory_calls += 1
        await asyncio.sleep(0.02)
        return {"ok": True}

    results = await asyncio.gather(
        *[cache.get_or_set("shared", factory) for _ in range(10)]
    )

    assert results == [{"ok": True}] * 10
    assert factory_calls == 1
    assert path.exists()
    assert thread_calls, "persistent cache writes must run off the event loop"
    assert not list(tmp_path.glob("cache.json.tmp-*"))


@pytest.mark.asyncio
async def test_close_is_best_effort_and_always_clears_state() -> None:
    events: list[str] = []

    class Resource:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        async def close(self) -> None:
            events.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} failed")

    ai = AcademicIntelligence(Config(sources=["openalex"]))
    ai._sources = {
        "bad": Resource("source-bad", fail=True),  # type: ignore[dict-item]
        "good": Resource("source-good"),  # type: ignore[dict-item]
    }
    ai._storage = Resource("storage")  # type: ignore[assignment]
    ai._http = Resource("http")  # type: ignore[assignment]
    ai._collector = object()  # type: ignore[assignment]
    ai._connected = True

    with pytest.raises(ExceptionGroup) as excinfo:
        await ai.close()

    assert [str(error) for error in excinfo.value.exceptions] == [
        "source-bad failed"
    ]
    assert events == ["source-bad", "source-good", "storage", "http"]
    assert ai._sources == {}
    assert ai._storage is None
    assert ai._http is None
    assert ai._collector is None
    assert ai._connected is False
