"""Unit tests for the trace-profiles primitive (``fetch_profiles``).

HTTP is mocked at the ``HTTPClient.get`` boundary with ``AsyncMock`` (project
convention — see ``tests/test_crossref_source.py``), so the whole suite runs
fully offline.  Cassettes are not needed: we only verify request building,
parsing and batching, not live OpenAlex payload drift.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from academic_intelligence.trace.profiles import AuthorRow, fetch_profiles
from academic_intelligence.utils.http import HTTPClient

AUTHOR_ID = "A5073802780"
AUTHOR_URL = f"https://api.openalex.org/authors/{AUTHOR_ID}"
WORKS_URL = (
    f"https://api.openalex.org/works?filter=author.id:{AUTHOR_ID}"
    "&sort=cited_by_count:desc&per-page=5"
)


def _row(name: str, author_id: str | None = None) -> AuthorRow:
    return AuthorRow(author_name=name, appears_in=["W1"], author_id=author_id)


def _response(status: int, *, json: Any | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=json,
        request=httpx.Request("GET", "https://api.openalex.org/authors/A1"),
    )


def _make_client(
    routes: dict[str, httpx.Response],
    order: list[str] | None = None,
) -> AsyncMock:
    """Build an ``AsyncMock(spec=HTTPClient)`` that routes by exact URL."""
    client = AsyncMock(spec=HTTPClient)

    async def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        if order is not None:
            order.append(url)
        if url not in routes:
            raise AssertionError(f"unexpected GET {url}")
        return routes[url]

    client.get.side_effect = fake_get
    return client


def _author_payload() -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/{AUTHOR_ID}",
        "display_name": "Xiangyu Zhang",
        "h_index": 112,
        "works_count": 509,
        "institutions": [{"display_name": "Peking University"}],
        "topics": [
            {"display_name": "Computer Vision"},
            {"display_name": "Object Detection"},
        ],
    }


def _works_payload(*counts: int) -> dict[str, Any]:
    """Works payload whose ``cited_by_count`` sequence follows *counts*."""
    return {
        "results": [
            {
                "title": f"paper-{i}",
                "publication_year": 2020 + i % 3,
                "cited_by_count": cited,
                "primary_location": {"source": {"display_name": f"venue-{i}"}},
            }
            for i, cited in enumerate(counts)
        ]
    }


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_profiles_maps_author_fields() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=_author_payload()),
            WORKS_URL: _response(200, json=_works_payload(7, 2, 9)),
        }
    )

    profiles = await fetch_profiles([_row("Xiangyu Zhang", AUTHOR_ID)], http=client)

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.author_name == "Xiangyu Zhang"
    assert profile.author_id == AUTHOR_ID
    assert profile.institution == "Peking University"
    assert profile.h_index == 112
    assert profile.works_count == 509
    assert profile.fields == ["Computer Vision", "Object Detection"]
    assert profile.errors == []
    # top_works sorted by cited_by_count desc
    assert [w["cited_by_count"] for w in profile.top_works] == [9, 7, 2]
    assert [w["title"] for w in profile.top_works] == ["paper-2", "paper-0", "paper-1"]


@pytest.mark.asyncio
async def test_real_openalex_schema_shape_is_parsed() -> None:
    """Real author docs use ``summary_stats.h_index`` + ``last_known_institutions``."""
    real_payload = {
        "id": f"https://openalex.org/{AUTHOR_ID}",
        "display_name": "Yann LeCun",
        "works_count": 484,
        "summary_stats": {"h_index": 121, "i10_index": 284},
        "last_known_institutions": [
            {"display_name": "New York University"},
            {"display_name": "Courant Institute of Mathematical Sciences"},
        ],
        "affiliations": [
            {"institution": {"display_name": "Supélec"}, "years": [1991]},
        ],
        "topics": [{"display_name": "Deep Learning"}],
    }
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=real_payload),
            WORKS_URL: _response(200, json=_works_payload(3)),
        }
    )

    profiles = await fetch_profiles([_row("Yann LeCun", AUTHOR_ID)], http=client)

    profile = profiles[0]
    assert profile.institution == "New York University"
    assert profile.h_index == 121
    assert profile.works_count == 484
    assert profile.fields == ["Deep Learning"]
    assert profile.errors == []


@pytest.mark.asyncio
async def test_top_works_sorted_and_capped_at_five() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=_author_payload()),
            WORKS_URL: _response(200, json=_works_payload(3, 50, 12, 8, 44, 2, 19)),
        }
    )

    profiles = await fetch_profiles([_row("A", AUTHOR_ID)], http=client)

    top = profiles[0].top_works
    assert len(top) == 5
    assert [w["cited_by_count"] for w in top] == [50, 44, 19, 12, 8]
    assert set(top[0]) == {"title", "venue", "year", "cited_by_count"}
    assert top[0]["title"] == "paper-1"
    assert top[0]["venue"] == "venue-1"
    assert top[0]["year"] == 2021


# ---------------------------------------------------------------------------
# No-ID rows (placeholder, no auto-search)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_author_id_returns_placeholder_without_http() -> None:
    client = _make_client({})

    profiles = await fetch_profiles([_row("No ID", None)], http=client)

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.author_name == "No ID"
    assert profile.author_id is None
    assert profile.institution is None
    assert profile.h_index is None
    assert profile.fields == []
    assert profile.works_count is None
    assert profile.top_works == []
    assert profile.errors == []
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_rows_with_and_without_id() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=_author_payload()),
            WORKS_URL: _response(200, json=_works_payload(4)),
        }
    )

    rows = [_row("Has ID", AUTHOR_ID), _row("No ID", None)]
    profiles = await fetch_profiles(rows, http=client)

    assert [p.author_id for p in profiles] == [AUTHOR_ID, None]
    assert profiles[0].institution == "Peking University"
    assert profiles[1].institution is None
    assert client.get.await_count == 2  # only the ID row triggers HTTP


# ---------------------------------------------------------------------------
# Failure tolerance (per-author, non-blocking)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_author_failure_does_not_block_batch() -> None:
    bad_url = "https://api.openalex.org/authors/A2"
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=_author_payload()),
            WORKS_URL: _response(200, json=_works_payload(4)),
            bad_url: _response(404, json={"error": "not found"}),
        }
    )

    rows = [_row("Good", AUTHOR_ID), _row("Bad", "A2")]
    profiles = await fetch_profiles(rows, http=client)

    assert len(profiles) == 2
    good, bad_profile = profiles
    assert good.errors == []
    assert good.institution == "Peking University"
    assert bad_profile.author_id == "A2"
    assert bad_profile.institution is None
    assert bad_profile.top_works == []
    assert len(bad_profile.errors) == 1
    assert "profile fetch failed" in bad_profile.errors[0]
    assert "404" in bad_profile.errors[0]


@pytest.mark.asyncio
async def test_works_fetch_failure_records_error_keeps_profile_fields() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=_author_payload()),
            WORKS_URL: _response(500, json={}),
        }
    )

    profiles = await fetch_profiles([_row("A", AUTHOR_ID)], http=client)

    profile = profiles[0]
    assert profile.institution == "Peking University"
    assert profile.h_index == 112
    assert profile.top_works == []
    assert len(profile.errors) == 1
    assert profile.errors[0].startswith("top works fetch failed: HTTPStatusError")


# ---------------------------------------------------------------------------
# Batching / pacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_size_one_is_sequentially_paced() -> None:
    ids = ["A1", "A2", "A3"]
    routes: dict[str, httpx.Response] = {}
    expected: list[str] = []
    for i, aid in enumerate(ids):
        routes[f"https://api.openalex.org/authors/{aid}"] = _response(
            200, json={**_author_payload(), "h_index": i}
        )
        works_url = (
            f"https://api.openalex.org/works?filter=author.id:{aid}"
            "&sort=cited_by_count:desc&per-page=5"
        )
        routes[works_url] = _response(200, json=_works_payload(i + 1))
        expected.append(f"https://api.openalex.org/authors/{aid}")
        expected.append(works_url)
    order: list[str] = []
    client = _make_client(routes, order=order)

    rows = [_row(f"author-{aid}", aid) for aid in ids]
    profiles = await fetch_profiles(rows, batch_size=1, http=client)

    assert [p.author_id for p in profiles] == ids
    assert [p.h_index for p in profiles] == [0, 1, 2]
    # batch_size=1 ⇒ strictly sequential: profile then works, author by author
    assert order == expected


@pytest.mark.asyncio
async def test_batch_size_two_fetches_all_authors() -> None:
    ids = [f"A{i}" for i in range(1, 6)]
    routes: dict[str, httpx.Response] = {}
    for aid in ids:
        routes[f"https://api.openalex.org/authors/{aid}"] = _response(200, json=_author_payload())
        routes[
            f"https://api.openalex.org/works?filter=author.id:{aid}"
            "&sort=cited_by_count:desc&per-page=5"
        ] = _response(200, json=_works_payload(3))
    client = _make_client(routes)

    rows = [_row(f"n-{aid}", aid) for aid in ids]
    profiles = await fetch_profiles(rows, batch_size=2, http=client)

    assert len(profiles) == 5
    assert all(p.errors == [] for p in profiles)
    assert client.get.await_count == 10  # 5 profiles + 5 works fetches


@pytest.mark.asyncio
async def test_batch_size_must_be_positive() -> None:
    client = _make_client({})

    with pytest.raises(ValueError, match="batch_size"):
        await fetch_profiles([_row("a", "A1")], batch_size=0, http=client)


# ---------------------------------------------------------------------------
# Duplicate author rows (same ID fetched once)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_author_id_fetched_once() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=_author_payload()),
            WORKS_URL: _response(200, json=_works_payload(5)),
        }
    )

    rows = [_row("Xiangyu Zhang", AUTHOR_ID), _row("Xiangyu Zhang", AUTHOR_ID)]
    profiles = await fetch_profiles(rows, http=client)

    assert len(profiles) == 2
    assert profiles[0].institution == profiles[1].institution == "Peking University"
    assert client.get.await_count == 2  # one profile + one works fetch, deduped


@pytest.mark.asyncio
async def test_full_url_author_id_normalized_for_fetch_preserved_in_profile() -> None:
    full_url = f"https://openalex.org/{AUTHOR_ID}"
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=_author_payload()),
            WORKS_URL: _response(200, json=_works_payload(5)),
        }
    )

    profiles = await fetch_profiles([_row("Xiangyu Zhang", full_url)], http=client)

    assert len(profiles) == 1
    assert profiles[0].author_id == full_url  # input preserved verbatim
    assert profiles[0].institution == "Peking University"
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_url_and_bare_forms_of_same_id_deduplicate() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=_author_payload()),
            WORKS_URL: _response(200, json=_works_payload(5)),
        }
    )

    rows = [
        _row("Xiangyu Zhang", AUTHOR_ID),
        _row("Xiangyu Zhang", f"https://openalex.org/{AUTHOR_ID}"),
    ]
    profiles = await fetch_profiles(rows, http=client)

    assert len(profiles) == 2
    assert client.get.await_count == 2  # deduped across url/bare spellings


# ---------------------------------------------------------------------------
# Semantic Scholar fallback (transient OpenAlex failure → S2 name match)
# ---------------------------------------------------------------------------

S2_SEARCH_URL = (
    "https://api.semanticscholar.org/graph/v1/author/search"
    "?query=Yann%20LeCun&limit=1&fields=authorId,name"
)
S2_AUTHOR_URL = (
    "https://api.semanticscholar.org/graph/v1/author/1688882"
    "?fields=name,affiliations,paperCount,hIndex,citationCount"
)


def _s2_search_payload() -> dict[str, Any]:
    return {
        "total": 1,
        "offset": 0,
        "next": None,
        "data": [{"authorId": "1688882", "name": "Yann LeCun"}],
    }


def _s2_author_payload() -> dict[str, Any]:
    return {
        "authorId": "1688882",
        "name": "Yann LeCun",
        "affiliations": ["New York University"],
        "paperCount": 484,
        "hIndex": 121,
        "citationCount": 194384,
    }


@pytest.mark.asyncio
async def test_openalex_429_falls_back_to_s2_author() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(429, json={}),
            S2_SEARCH_URL: _response(200, json=_s2_search_payload()),
            S2_AUTHOR_URL: _response(200, json=_s2_author_payload()),
        }
    )

    profiles = await fetch_profiles([_row("Yann LeCun", AUTHOR_ID)], http=client)

    profile = profiles[0]
    assert profile.source == "s2"
    assert profile.author_name == "Yann LeCun"
    assert profile.author_id == AUTHOR_ID  # input OpenAlex id preserved verbatim
    assert profile.institution == "New York University"
    assert profile.h_index == 121
    assert profile.works_count == 484
    assert profile.fields == []
    assert profile.top_works == []
    # errors records both the OpenAlex failure and the fallback situation.
    assert len(profile.errors) == 2
    assert "profile fetch failed" in profile.errors[0]
    assert "s2 fallback" in profile.errors[1]
    assert "disambiguation" in profile.errors[1]


@pytest.mark.asyncio
async def test_openalex_and_s2_failures_are_recorded() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(429, json={}),
            S2_SEARCH_URL: _response(500, json={}),
        }
    )

    profiles = await fetch_profiles([_row("Yann LeCun", AUTHOR_ID)], http=client)

    profile = profiles[0]
    assert profile.source == "openalex"
    assert profile.institution is None
    assert profile.h_index is None
    assert profile.works_count is None
    assert profile.top_works == []
    assert len(profile.errors) == 2
    assert "profile fetch failed" in profile.errors[0]
    assert "s2 fallback failed" in profile.errors[1]


@pytest.mark.asyncio
async def test_s2_search_no_match_records_fallback_failure() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(429, json={}),
            S2_SEARCH_URL: _response(200, json={"total": 0, "data": []}),
        }
    )

    profiles = await fetch_profiles([_row("Yann LeCun", AUTHOR_ID)], http=client)

    profile = profiles[0]
    assert profile.source == "openalex"
    assert profile.institution is None
    assert "s2 fallback failed: no Semantic Scholar author found" in profile.errors[1]


@pytest.mark.asyncio
async def test_s2_rate_limit_fails_soft_single_attempt() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(429, json={}),
            S2_SEARCH_URL: _response(429, json={}),
        }
    )

    profiles = await fetch_profiles([_row("Yann LeCun", AUTHOR_ID)], http=client)

    profile = profiles[0]
    assert profile.source == "openalex"
    assert len(profile.errors) == 2
    assert "s2 fallback failed" in profile.errors[1]
    # openalex profile + exactly one S2 search: the 429 is recorded, not retried.
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_permanent_openalex_failure_does_not_fall_back() -> None:
    # A 404 is a real answer (author not in OpenAlex), not a quota problem —
    # no S2 fallback is attempted.
    client = _make_client(
        {
            AUTHOR_URL: _response(404, json={}),
        }
    )

    profiles = await fetch_profiles([_row("Yann LeCun", AUTHOR_ID)], http=client)

    profile = profiles[0]
    assert profile.source == "openalex"
    assert len(profile.errors) == 1
    assert "profile fetch failed" in profile.errors[0]
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_openalex_success_marks_source_openalex() -> None:
    client = _make_client(
        {
            AUTHOR_URL: _response(200, json=_author_payload()),
            WORKS_URL: _response(200, json=_works_payload(5)),
        }
    )

    profiles = await fetch_profiles([_row("Xiangyu Zhang", AUTHOR_ID)], http=client)

    profile = profiles[0]
    assert profile.source == "openalex"
    assert profile.errors == []


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_input_returns_empty_list() -> None:
    client = _make_client({})

    assert await fetch_profiles([], http=client) == []
    client.get.assert_not_awaited()
