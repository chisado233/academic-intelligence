"""FIX-O ticket tests (B7-P33 round 15 known items — execution round).

- F1 (N4): OpenAlex author lookups fetch multiple candidates (not the
  single ``per_page=1`` top result) and pick the best one — exact
  normalized-name match first, ``cited_by_count`` descending among exact
  same-name candidates, ``results[0]`` fallback when no exact match exists.
  Common-name queries ("Wei Zhang") no longer deterministically resolve to
  a different person ("Yong-Wei Zhang").
- F2 (JSON backend): name-only bylines are re-keyed to the same-name
  stored ``Author`` record (case-insensitive) on ``save_paper`` /
  ``update_paper`` / ``save_batch``, mirroring the sqlite FIX-M/N1
  semantics — ``get_author_papers(<author id>)`` serves name-only papers
  and both backends agree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_intelligence.core.models import Author, AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.sources.openalex import OpenAlexSource, _select_author_candidate
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(source: SourceType, conf: float, sid: str) -> Evidence:
    return Evidence(
        source=source,
        source_url=f"https://{source.value}/record",
        source_id=sid,
        confidence=conf,
        collected_at=datetime.now(UTC),
    )


def _mock_response(*, status_code: int = 200, json_data: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = ""
    resp.headers = {}
    resp.json = MagicMock(return_value=json_data)
    return resp


def _http_with(*responses: Any) -> MagicMock:
    http = MagicMock()
    http.get = AsyncMock(side_effect=list(responses))
    return http


# ---------------------------------------------------------------------------
# F1 (N4): OpenAlex author candidate selection
# ---------------------------------------------------------------------------


def _author(author_id: str, name: str, cited: int | None = None) -> dict[str, Any]:
    """Canned OpenAlex ``/authors`` candidate (id bare or full URL)."""
    full = author_id if author_id.startswith("https://") else f"https://openalex.org/{author_id}"
    item: dict[str, Any] = {"id": full, "display_name": name}
    if cited is not None:
        item["cited_by_count"] = cited
        item["summary_stats"] = {"cited_by_count": cited}
    return item


def test_select_author_candidate_prefers_exact_name_over_top1() -> None:
    """N4: a non-exact top-1 loses to an exact same-name candidate, and the
    highest-cited exact candidate wins among several."""
    results = [
        _author("A-top1", "Yong-Wei Zhang", 52381),
        _author("A2", "Wei Zhang", 500),
        _author("A3", "Wei Zhang", 900),
    ]
    chosen = _select_author_candidate("Wei Zhang", results)
    assert chosen is not None
    assert chosen["id"] == "https://openalex.org/A3"


def test_select_author_candidate_wei_zhang_scenario() -> None:
    """N4 (P33 real-data shape): top-1 is a non-exact "Yong-Wei Zhang" and
    8 of the 9 candidates are exact "Wei Zhang" — the highest-cited exact
    candidate is selected, not the top-1."""
    results = [
        _author("A5100675809", "Yong-Wei Zhang", 52381),
        _author("A1", "Wei Zhang", 200),
        _author("A2", "Wei Zhang", 87),
        _author("A3", "Wei Zhang", 400),
        _author("A4", "Wei Zhang", 150),
        _author("A5", "Wei Zhang", 300),
        _author("A6", "Wei Zhang", 50),
        _author("A7", "Wei Zhang", 250),
        _author("A8", "Wei Zhang", 100),
    ]
    chosen = _select_author_candidate("Wei Zhang", results)
    assert chosen is not None
    assert chosen["id"] == "https://openalex.org/A3"


def test_select_author_candidate_falls_back_to_top1_without_exact() -> None:
    """N4: "J. Li" / "John Smith" — no exact candidate among the results —
    keeps the pre-fix top-1 (honest fallback, no context to do better)."""
    results = [
        _author("A1", "Jing Li", 118984),
        _author("A2", "Jun Li", 90000),
        _author("A3", "Jin Li", 80000),
    ]
    chosen = _select_author_candidate("J. Li", results)
    assert chosen is results[0]


def test_select_author_candidate_middle_initial_tolerated() -> None:
    """N4: normalization drops single-char middle initials, so
    "Geoffrey Hinton" matches "Geoffrey E. Hinton" as an exact candidate."""
    results = [
        _author("A1", "Geoffrey E. Hinton", 600000),
        _author("A2", "Geoffrey Hinton", 100),
    ]
    chosen = _select_author_candidate("Geoffrey Hinton", results)
    assert chosen is not None
    assert chosen["id"] == "https://openalex.org/A1"


def test_select_author_candidate_uses_summary_stats_cited_by_count() -> None:
    """N4: ``cited_by_count`` may live under ``summary_stats``; a candidate
    lacking the top-level field is ranked by its summary count."""
    results = [
        _author("A1", "Wei Zhang"),  # no citation fields at all
        {
            "id": "https://openalex.org/A2",
            "display_name": "Wei Zhang",
            "summary_stats": {"cited_by_count": 700},
        },
        {
            "id": "https://openalex.org/A3",
            "display_name": "Wei Zhang",
            "summary_stats": {"cited_by_count": 300},
        },
    ]
    chosen = _select_author_candidate("Wei Zhang", results)
    assert chosen is not None
    assert chosen["id"] == "https://openalex.org/A2"


def test_select_author_candidate_single_candidate_unchanged() -> None:
    """N4: a single candidate is returned as before (backward compatible)."""
    results = [_author("A1", "Test Author", 10)]
    assert _select_author_candidate("Test Author", results) is results[0]


def test_select_author_candidate_empty_results_none() -> None:
    assert _select_author_candidate("Nobody", []) is None


@pytest.mark.asyncio
async def test_get_author_papers_uses_selected_exact_candidate() -> None:
    """N4 end-to-end: the ``/works`` request filters by the selected
    exact-name candidate's id, and the author search asks for 25 candidates."""
    src = OpenAlexSource(
        http_client=_http_with(
            _mock_response(
                json_data={
                    "results": [
                        _author("A-top1", "Yong-Wei Zhang", 52381),
                        _author("A2", "Wei Zhang", 900),
                    ]
                }
            ),
            _mock_response(json_data={"results": [{"id": "W1", "title": "Work"}]}),
        )
    )
    papers = await src.get_author_papers("Wei Zhang")
    assert len(papers) == 1
    assert papers[0].title == "Work"
    search_params = src._http.get.await_args_list[0].kwargs["params"]
    assert search_params["per_page"] == 25
    works_params = src._http.get.await_args_list[1].kwargs["params"]
    assert works_params["filter"] == "author.id:https://openalex.org/A2"


@pytest.mark.asyncio
async def test_get_author_profile_uses_selected_exact_candidate() -> None:
    """N4 end-to-end: ``get_author_profile`` returns the exact same-name
    candidate, not the non-exact top-1."""
    src = OpenAlexSource(
        http_client=_http_with(
            _mock_response(
                json_data={
                    "results": [
                        _author("A-top1", "Yong-Wei Zhang", 52381),
                        _author("A2", "Wei Zhang", 900),
                    ]
                }
            )
        )
    )
    author = await src.get_author_profile("Wei Zhang")
    assert author is not None
    assert author.id == "A2"
    assert author.name == "Wei Zhang"
    assert src._http.get.await_args_list[0].kwargs["params"]["per_page"] == 25


@pytest.mark.asyncio
async def test_get_author_papers_fallback_behavior_unchanged() -> None:
    """N4: author-search miss still falls back to a plain paper search, and
    a candidate without an id triggers the same fallback."""
    src = OpenAlexSource(
        http_client=_http_with(
            _mock_response(json_data={"results": []}),
            _mock_response(json_data={"results": [{"title": "Fallback"}]}),
        )
    )
    papers = await src.get_author_papers("Nobody")
    assert len(papers) == 1
    assert papers[0].title == "Fallback"


# ---------------------------------------------------------------------------
# F2 (JSON backend): name-only author linking (mirrors FIX-M F1 / N1)
# ---------------------------------------------------------------------------


def _alice_author() -> Author:
    return Author(
        name="Alice Smith",
        evidence_list=[_ev(SourceType.PUBMED, 0.92, "pm-alice")],
    )


def _alice_paper(paper_id: str = "p-alice") -> Paper:
    return Paper(
        id=paper_id,
        title="A Paper by Alice",
        year=2024,
        authors=[AuthorRef(name="Alice Smith", position=1)],
        evidence_list=[_ev(SourceType.PUBMED, 0.92, "pm-p1")],
    )


@pytest.mark.asyncio
async def test_fix_o_f2_json_save_batch_links_name_only_author(tmp_path: Path) -> None:
    """F2: JSON ``save_batch`` re-keys a name-only byline to the same-batch
    Author record — ``get_author_papers(<author id>)`` serves the paper and
    the ``~name`` pseudo-key no longer answers."""
    store = JSONStorage(str(tmp_path / "f2"))
    await store.connect()
    try:
        ids = await store.save_batch(authors=[_alice_author()], papers=[_alice_paper()])
        author_id = ids["authors"][0]
        assert author_id
        assert await store.get_author_papers(author_id) == ["p-alice"]
        assert await store.get_author_papers("~Alice Smith") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_o_f2_json_save_paper_links_name_only_author(tmp_path: Path) -> None:
    """F2: the single-record ``save_paper`` path links name-only bylines to a
    persisted same-name author too."""
    store = JSONStorage(str(tmp_path / "f2b"))
    await store.connect()
    try:
        author_id = await store.save_author(_alice_author())
        await store.save_paper(_alice_paper())
        assert await store.get_author_papers(author_id) == ["p-alice"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_o_f2_json_update_paper_links_name_only_author(tmp_path: Path) -> None:
    """F2: ``update_paper`` re-keys name-only bylines as well."""
    store = JSONStorage(str(tmp_path / "f2e"))
    await store.connect()
    try:
        author_id = await store.save_author(_alice_author())
        await store.save_paper(_alice_paper())
        updated = _alice_paper().model_copy(update={"title": "Updated Title"})
        assert await store.update_paper("p-alice", updated) is True
        assert await store.get_author_papers(author_id) == ["p-alice"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_o_f2_json_pre_existing_author_linked_from_later_batch(
    tmp_path: Path,
) -> None:
    """F2: an author persisted in an earlier batch is matched by name when a
    later batch persists papers carrying her byline."""
    store = JSONStorage(str(tmp_path / "f2c"))
    await store.connect()
    try:
        first = await store.save_batch(authors=[_alice_author()], papers=[])
        author_id = first["authors"][0]
        await store.save_batch(papers=[_alice_paper()])
        assert await store.get_author_papers(author_id) == ["p-alice"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_o_f2_json_case_insensitive_name_match(tmp_path: Path) -> None:
    """F2: the name match is case-insensitive (mirrors sqlite
    ``func.lower(AuthorRow.name)``)."""
    store = JSONStorage(str(tmp_path / "f2f"))
    await store.connect()
    try:
        author_id = await store.save_author(
            _alice_author().model_copy(update={"name": "alice smith"})
        )
        await store.save_paper(_alice_paper())  # byline "Alice Smith"
        assert await store.get_author_papers(author_id) == ["p-alice"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_o_f2_json_unmatched_name_keeps_pseudo_key(tmp_path: Path) -> None:
    """F2: a byline name with no matching Author record keeps the ``~name``
    pseudo-key fallback (no resolution, no crash)."""
    store = JSONStorage(str(tmp_path / "f2d"))
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                Paper(
                    id="p-ghost",
                    title="Ghost",
                    authors=[AuthorRef(name="Nobody Else", position=1)],
                    evidence_list=[_ev(SourceType.PUBMED, 0.92, "pm-g")],
                )
            ]
        )
        assert await store.get_author_papers("~Nobody Else") == ["p-ghost"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fix_o_f2_backends_consistent_name_only_linking(tmp_path: Path) -> None:
    """F2: sqlite and json backends re-key the same name-only bylines to
    their own Author record ids — ``get_author_papers`` answers identically
    and the ``~name`` pseudo-key is gone in both."""
    sqlite_store = SQLiteStorage(str(tmp_path / "f2c.db"))
    json_store = JSONStorage(str(tmp_path / "f2c-json"))
    await sqlite_store.connect()
    await json_store.connect()
    try:
        for store in (sqlite_store, json_store):
            ids = await store.save_batch(authors=[_alice_author()], papers=[_alice_paper()])
            author_id = ids["authors"][0]
            assert author_id
            assert await store.get_author_papers(author_id) == ["p-alice"]
            assert await store.get_author_papers("~Alice Smith") == []
    finally:
        await sqlite_store.close()
        await json_store.close()
