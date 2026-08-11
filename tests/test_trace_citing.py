"""Unit tests for the trace-citing primitive (``academic_intelligence.trace.citing``).

Covers, fully offline via a fake HTTP client:

- OpenAlex reverse-citation pagination (two pages merged, first-page cursor
  must be ``*``, ``per-page=200``);
- OpenCitations (COCI) path keyed by DOI;
- cross-source merge deduped by ``citing_paper_id``;
- single-source failure fail-soft (``CitingResult.errors``);
- ``limit`` truncation with a resumable ``resume_cursor``;
- ``paper_id`` normalization (W-id / DOI / arXiv);
- skipping OpenCitations when no DOI is available;
- empty results and malformed inputs.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from academic_intelligence.trace.citing import (
    CitingPaper,
    CitingResult,
    fetch_citing_papers,
)

CITING_DOI = "10.1038/s41586-025-09422-z"
CITING_ARXIV = "1707.06347"
CITING_W_ID = "W2257979135"


def _oa_item(
    work_id: str,
    *,
    title: str = "A citing paper",
    year: int | None = 2024,
    doi: str | None = None,
    venue: str | None = "Nature",
    authors: list[str] | None = None,
) -> dict[str, Any]:
    """Build a realistic OpenAlex ``/works`` result item."""
    authorships: list[dict[str, Any]] = []
    for i, name in enumerate(authors or ["Some Author"]):
        authorships.append(
            {
                "author": {
                    "id": f"https://openalex.org/A{i + 1}",
                    "display_name": name,
                },
                "institutions": [{"display_name": f"Inst {i + 1}"}],
            }
        )
    item: dict[str, Any] = {
        "id": f"https://openalex.org/{work_id}",
        "title": title,
        "publication_year": year,
        "authorships": authorships,
        "primary_location": {"source": {"display_name": venue}},
    }
    if doi is not None:
        item["doi"] = f"https://doi.org/{doi}"
    return item


def _page(*items: dict[str, Any], next_cursor: str | None) -> dict[str, Any]:
    return {"results": list(items), "meta": {"next_cursor": next_cursor}}


def _edge(citing: str, cited: str = CITING_DOI) -> dict[str, str]:
    return {
        "oci": f"06000000000-{abs(hash(citing)):012d}",
        "citing": citing,
        "cited": cited,
        "creation": "2025-01-01",
        "timespan": "P0Y",
        "journal_sc": "no",
        "author_sc": "no",
    }


class FakeHTTP:
    """In-memory HTTPClient stand-in dispatching on URL / filter params.

    Records every call for cursor/param assertions.  ``pages`` maps the
    OpenAlex cursor value to a page payload (an ``Exception`` instance
    makes the call raise, for fail-soft tests); ``lookup`` maps the
    ``filter=`` value to a lookup payload; ``coci`` is the edge array.
    """

    def __init__(
        self,
        *,
        pages: dict[str, Any] | None = None,
        lookup: dict[str, Any] | None = None,
        coci: list[Any] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.lookup = lookup or {}
        self.coci = coci or []
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append((url, params))
        if "opencitations.net" in url:
            if isinstance(self.coci, BaseException):
                raise self.coci
            return self.coci
        filter_val = (params or {}).get("filter", "")
        if filter_val.startswith("cites:"):
            cursor = (params or {}).get("cursor")
            if cursor not in self.pages:
                raise AssertionError(f"unexpected OpenAlex cursor {cursor!r}")
            payload = self.pages[cursor]
            if isinstance(payload, BaseException):
                raise payload
            return payload
        return self.lookup.get(filter_val, {"results": []})


# ---------------------------------------------------------------------------
# OpenAlex pagination + cursor contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openalex_two_page_merge() -> None:
    http = FakeHTTP(
        pages={
            "*": _page(_oa_item("W1"), _oa_item("W2"), next_cursor="cursor-2"),
            "cursor-2": _page(_oa_item("W3"), _oa_item("W4"), next_cursor=None),
        }
    )

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http)

    assert [p.citing_paper_id for p in result.papers] == ["W1", "W2", "W3", "W4"]
    assert result.resume_cursor is None
    assert result.source_stats == {"openalex": 4}
    assert result.errors == []

    # First page must use cursor "*"; second page follows meta.next_cursor.
    cite_calls = [c for c in http.calls if "cites:" in (c[1] or {}).get("filter", "")]
    assert cite_calls[0][1]["cursor"] == "*"
    assert cite_calls[0][1]["per-page"] == 200
    assert cite_calls[1][1]["cursor"] == "cursor-2"
    assert cite_calls[1][1]["filter"] == f"cites:{CITING_W_ID}"


@pytest.mark.asyncio
async def test_openalex_first_page_cursor_is_star() -> None:
    http = FakeHTTP(pages={"*": _page(next_cursor=None)})

    await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http)

    assert http.calls[0][1]["cursor"] == "*"


@pytest.mark.asyncio
async def test_openalex_handles_empty_next_cursor() -> None:
    http = FakeHTTP(pages={"*": _page(_oa_item("W1"), next_cursor="")})

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http)

    assert len(result.papers) == 1
    assert result.resume_cursor is None


# ---------------------------------------------------------------------------
# OpenCitations path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opencitations_path_keyed_by_doi() -> None:
    http = FakeHTTP(
        pages={"*": _page(_oa_item("W1"), next_cursor=None)},
        lookup={"doi:" + CITING_DOI: {"results": [_oa_item("W500")]}},
        coci=[_edge("10.1109/iros60139.2025.11246595"), _edge("10.1038/d41586-025-02703-7")],
    )

    result = await fetch_citing_papers(CITING_DOI, http=http)

    ids = [p.citing_paper_id for p in result.papers]
    assert "W1" in ids
    assert "10.1109/iros60139.2025.11246595" in ids
    assert "10.1038/d41586-025-02703-7" in ids
    assert result.source_stats == {"openalex": 1, "opencitations": 2}

    # COCI must be queried via the /citations/ endpoint with the DOI.
    coci_calls = [c for c in http.calls if "opencitations.net" in c[0]]
    assert len(coci_calls) == 1
    assert "index/coci/api/v1/citations/10.1038%2Fs41586-025-09422-z" in coci_calls[0][0]


@pytest.mark.asyncio
async def test_opencitations_skips_malformed_and_self_edges() -> None:
    http = FakeHTTP(
        pages={"*": _page(next_cursor=None)},
        lookup={"doi:" + CITING_DOI: {"results": [_oa_item("W500")]}},
        coci=[
            _edge("10.1109/iros60139.2025.11246595"),
            _edge(CITING_DOI),  # self-citation — must be skipped
            {"oci": "x", "cited": CITING_DOI},  # missing citing DOI — skipped
            _edge("10.1038/d41586-025-02703-7"),
        ],
    )

    result = await fetch_citing_papers(CITING_DOI, http=http)

    coci_ids = [p.citing_paper_id for p in result.papers if p.doi is not None and p.title is None]
    assert coci_ids == ["10.1109/iros60139.2025.11246595", "10.1038/d41586-025-02703-7"]


# ---------------------------------------------------------------------------
# Cross-source merge / dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_dedup_by_citing_paper_id_across_pages() -> None:
    # W2 appears on both pages (cursor pagination overlap) — deduped.
    http = FakeHTTP(
        pages={
            "*": _page(_oa_item("W1"), _oa_item("W2"), next_cursor="cursor-2"),
            "cursor-2": _page(_oa_item("W2"), _oa_item("W3"), next_cursor=None),
        }
    )

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http)

    assert [p.citing_paper_id for p in result.papers] == ["W1", "W2", "W3"]
    assert result.source_stats == {"openalex": 3}


@pytest.mark.asyncio
async def test_merge_dedup_shared_seen_set_both_sources() -> None:
    # A single shared dedup set backs both sources, so a paper id produced
    # by both (only possible in the DOI id-space for COCI edges repeated
    # across the merge) survives once.
    http = FakeHTTP(
        pages={"*": _page(_oa_item("W1"), next_cursor=None)},
        lookup={"doi:" + CITING_DOI: {"results": [_oa_item("W500")]}},
        coci=[
            _edge("10.1038/d41586-025-02703-7"),
            _edge("10.1038/d41586-025-02703-7"),
        ],
    )

    result = await fetch_citing_papers(CITING_DOI, http=http)

    assert result.source_stats == {"openalex": 1, "opencitations": 1}
    assert len(result.papers) == 2


@pytest.mark.asyncio
async def test_merge_dedup_across_id_spaces_by_doi() -> None:
    # I-3: the same paper surfaced as an OpenAlex W-id (with a DOI) and as a
    # COCI DOI edge must collapse into one row — the dedup key is
    # ``doi if doi else citing_paper_id``.
    http = FakeHTTP(
        pages={"*": _page(_oa_item("W1", doi="10.1000/1"), next_cursor=None)},
        lookup={"doi:" + CITING_DOI: {"results": [_oa_item("W500")]}},
        coci=[_edge("10.1000/1"), _edge("10.2222/bbb")],
    )

    result = await fetch_citing_papers(CITING_DOI, http=http)

    assert [p.citing_paper_id for p in result.papers] == ["W1", "10.2222/bbb"]
    # The W-id row (richer metadata) was inserted first and is credited to
    # openalex; the COCI edge for the same DOI contributed nothing new.
    assert result.source_stats == {"openalex": 1, "opencitations": 2}
    assert result.written_stats == {"openalex": 1, "opencitations": 1}


# ---------------------------------------------------------------------------
# Fail-soft behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_source_failure_is_fail_soft() -> None:
    http = FakeHTTP(
        pages={"*": RuntimeError("connection refused")},
        lookup={"doi:" + CITING_DOI: {"results": [_oa_item("W500")]}},
        coci=[_edge("10.1038/d41586-025-02703-7")],
    )

    result = await fetch_citing_papers(CITING_DOI, http=http)

    assert [p.citing_paper_id for p in result.papers] == ["10.1038/d41586-025-02703-7"]
    assert result.source_stats == {"opencitations": 1}
    assert len(result.errors) == 1
    failure = result.errors[0]
    assert failure.source == "openalex"
    assert failure.transient is False
    assert "connection refused" in failure.message


@pytest.mark.asyncio
async def test_http_status_error_maps_http_status() -> None:
    request = httpx.Request("GET", "https://opencitations.net/index/coci/api/v1/citations/x")
    response = httpx.Response(500, request=request)
    http = FakeHTTP(
        pages={"*": _page(_oa_item("W1"), next_cursor=None)},
        lookup={"doi:" + CITING_DOI: {"results": [_oa_item("W500")]}},
        coci=httpx.HTTPStatusError("HTTP 500", request=request, response=response),
    )

    result = await fetch_citing_papers(CITING_DOI, http=http)

    assert len(result.errors) == 1
    failure = result.errors[0]
    assert failure.source == "opencitations"
    assert failure.http_status == 500
    assert failure.permanent is True
    # The OpenAlex side still succeeded.
    assert result.source_stats == {"openalex": 1}


# ---------------------------------------------------------------------------
# limit truncation + resume cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_truncation_mid_page_resumes_at_next_cursor() -> None:
    http = FakeHTTP(
        pages={
            "*": _page(_oa_item("W1"), _oa_item("W2"), next_cursor="cursor-2"),
            "cursor-2": _page(_oa_item("W3"), _oa_item("W4"), next_cursor="cursor-3"),
        }
    )

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http, limit=3)

    assert [p.citing_paper_id for p in result.papers] == ["W1", "W2", "W3"]
    # Cut lands mid-page-2: resume continues at the next page (the
    # unconsumed tail W4 of the cut page is dropped by design).
    assert result.resume_cursor == "cursor-3"


@pytest.mark.asyncio
async def test_limit_mid_page_cut_on_final_page_resume_none() -> None:
    http = FakeHTTP(
        pages={
            "*": _page(_oa_item("W1"), _oa_item("W2"), next_cursor="cursor-2"),
            "cursor-2": _page(_oa_item("W3"), _oa_item("W4"), next_cursor=None),
        }
    )

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http, limit=3)

    assert [p.citing_paper_id for p in result.papers] == ["W1", "W2", "W3"]
    # Final page cut mid-way: no further page exists, so the run reports
    # exhaustion and W4 is dropped (documented safety-cap trade-off).
    assert result.resume_cursor is None


@pytest.mark.asyncio
async def test_limit_exact_page_boundary_resumes_at_next_cursor() -> None:
    http = FakeHTTP(
        pages={
            "*": _page(_oa_item("W1"), _oa_item("W2"), next_cursor="cursor-2"),
            "cursor-2": _page(_oa_item("W3"), next_cursor=None),
        }
    )

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http, limit=2)

    assert [p.citing_paper_id for p in result.papers] == ["W1", "W2"]
    assert result.resume_cursor == "cursor-2"


@pytest.mark.asyncio
async def test_limit_resume_from_starts_at_given_cursor() -> None:
    http = FakeHTTP(
        pages={
            "cursor-9": _page(_oa_item("W5"), _oa_item("W6"), next_cursor=None),
        }
    )

    result = await fetch_citing_papers(
        CITING_W_ID,
        sources=["openalex"],
        http=http,
        limit=10,
        resume_from="cursor-9",
    )

    assert [p.citing_paper_id for p in result.papers] == ["W5", "W6"]
    assert result.resume_cursor is None
    assert http.calls[0][1]["cursor"] == "cursor-9"


@pytest.mark.asyncio
async def test_limit_zero_returns_empty_without_requests() -> None:
    http = FakeHTTP()

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http, limit=0)

    assert result.papers == []
    assert result.resume_cursor is None
    assert result.errors == []
    assert http.calls == []


@pytest.mark.asyncio
async def test_limit_filled_by_openalex_still_queries_opencitations() -> None:
    # I-2: when OpenAlex already fills the --limit quota, OpenCitations must
    # still run (it is a single unpaginated response) instead of being
    # silently skipped — the final truncation applies to the merged result.
    http = FakeHTTP(
        pages={
            "*": _page(
                _oa_item("W1", doi="10.1000/1"),
                _oa_item("W2", doi="10.1000/2"),
                next_cursor=None,
            )
        },
        lookup={"doi:" + CITING_DOI: {"results": [_oa_item("W500")]}},
        coci=[_edge("10.1111/aaa"), _edge("10.2222/bbb")],
    )

    result = await fetch_citing_papers(CITING_DOI, http=http, limit=2)

    # OpenCitations was queried (not skipped) and its pulls are reported...
    assert any("opencitations.net" in c[0] for c in http.calls)
    assert result.source_stats == {"openalex": 2, "opencitations": 2}
    # ...but the merged output is truncated to the limit; openalex rows (rich
    # metadata) fill it, so opencitations contributed 0 written rows (absent
    # from written_stats).
    assert [p.citing_paper_id for p in result.papers] == ["W1", "W2"]
    assert result.written_stats == {"openalex": 2}
    assert result.resume_cursor is None


# ---------------------------------------------------------------------------
# paper_id normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_id_w_id_uses_directly() -> None:
    http = FakeHTTP(pages={"*": _page(_oa_item("W1"), next_cursor=None)})

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http)

    cite_calls = [c for c in http.calls if "cites:" in (c[1] or {}).get("filter", "")]
    assert cite_calls[0][1]["filter"] == f"cites:{CITING_W_ID}"
    # No lookup call for a direct W-id.
    assert not any("doi:" in (c[1] or {}).get("filter", "") for c in http.calls)
    assert not any("ids.arxiv:" in (c[1] or {}).get("filter", "") for c in http.calls)
    assert len(result.papers) == 1


@pytest.mark.asyncio
async def test_paper_id_doi_lookup_and_strip_prefix() -> None:
    http = FakeHTTP(
        pages={"*": _page(_oa_item("W500"), next_cursor=None)},
        lookup={"doi:" + CITING_DOI: {"results": [_oa_item("W500")]}},
    )

    result = await fetch_citing_papers(
        f"https://doi.org/{CITING_DOI}", sources=["openalex"], http=http
    )

    lookup_calls = [c for c in http.calls if "doi:" in (c[1] or {}).get("filter", "")]
    assert lookup_calls[0][1]["filter"] == f"doi:{CITING_DOI}"
    cite_calls = [c for c in http.calls if "cites:" in (c[1] or {}).get("filter", "")]
    assert cite_calls[0][1]["filter"] == "cites:W500"
    assert len(result.papers) == 1


@pytest.mark.asyncio
async def test_paper_id_arxiv_lookup() -> None:
    http = FakeHTTP(
        pages={"*": _page(_oa_item("W500"), next_cursor=None)},
        lookup={"ids.arxiv:" + CITING_ARXIV: {"results": [_oa_item("W500")]}},
    )

    result = await fetch_citing_papers(CITING_ARXIV, sources=["openalex"], http=http)

    arxiv_calls = [c for c in http.calls if "ids.arxiv:" in (c[1] or {}).get("filter", "")]
    assert arxiv_calls[0][1]["filter"] == f"ids.arxiv:{CITING_ARXIV}"
    cite_calls = [c for c in http.calls if "cites:" in (c[1] or {}).get("filter", "")]
    assert cite_calls[0][1]["filter"] == "cites:W500"
    assert len(result.papers) == 1


@pytest.mark.asyncio
async def test_paper_id_arxiv_url_and_version_normalized() -> None:
    http = FakeHTTP(
        pages={"*": _page(_oa_item("W500"), next_cursor=None)},
        lookup={"ids.arxiv:2106.09685": {"results": [_oa_item("W500")]}},
    )

    result = await fetch_citing_papers(
        "https://arxiv.org/abs/2106.09685v2", sources=["openalex"], http=http
    )

    arxiv_calls = [c for c in http.calls if "ids.arxiv:" in (c[1] or {}).get("filter", "")]
    assert arxiv_calls[0][1]["filter"] == "ids.arxiv:2106.09685"
    assert len(result.papers) == 1


@pytest.mark.asyncio
async def test_paper_id_doi_not_found_fails_openalex_soft() -> None:
    http = FakeHTTP(
        lookup={"doi:" + CITING_DOI: {"results": []}},
        coci=[_edge("10.1038/d41586-025-02703-7")],
    )

    result = await fetch_citing_papers(CITING_DOI, http=http)

    assert [p.citing_paper_id for p in result.papers] == ["10.1038/d41586-025-02703-7"]
    assert result.source_stats == {"opencitations": 1}
    assert len(result.errors) == 1
    assert result.errors[0].source == "openalex"
    assert "no OpenAlex work found" in result.errors[0].message


@pytest.mark.asyncio
async def test_paper_id_unrecognized_fails_both_soft() -> None:
    http = FakeHTTP()

    result = await fetch_citing_papers("garbage-id", http=http)

    assert result.papers == []
    assert len(result.errors) == 2
    assert {e.source for e in result.errors} == {"openalex", "opencitations"}
    assert all(e.permanent is True for e in result.errors)
    assert http.calls == []


@pytest.mark.asyncio
async def test_legacy_arxiv_id_gets_targeted_error() -> None:
    # M-1: legacy pre-2007 arXiv ids (hep-th/9901001) are rejected locally
    # with a hint naming the supported new-style format, not the generic
    # "cannot resolve" message — and no request ever reaches a server.
    http = FakeHTTP()

    result = await fetch_citing_papers("hep-th/9901001", http=http)

    assert result.papers == []
    assert http.calls == []
    openalex_error = next(e for e in result.errors if e.source == "openalex")
    assert "legacy arXiv id" in openalex_error.message
    assert "YYYY.NNNNN" in openalex_error.message
    # The generic path keeps the new-style hint too.
    garbage_error = next(
        e
        for e in (await fetch_citing_papers("garbage-id", http=FakeHTTP())).errors
        if e.source == "openalex"
    )
    assert "YYYY.NNNNN" in garbage_error.message


# ---------------------------------------------------------------------------
# Skipping OpenCitations without a DOI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_w_id_input_skips_opencitations() -> None:
    http = FakeHTTP(pages={"*": _page(_oa_item("W1"), next_cursor=None)})

    result = await fetch_citing_papers(CITING_W_ID, http=http)

    assert [p.citing_paper_id for p in result.papers] == ["W1"]
    assert result.source_stats == {"openalex": 1}
    assert "opencitations" not in result.source_stats
    assert len(result.errors) == 1
    assert result.errors[0].source == "opencitations"
    assert "DOI" in result.errors[0].message
    assert not any("opencitations.net" in c[0] for c in http.calls)


# ---------------------------------------------------------------------------
# Empty results + unknown source names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_openalex_result() -> None:
    http = FakeHTTP(pages={"*": _page(next_cursor=None)})

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http)

    assert result.papers == []
    assert result.resume_cursor is None
    assert result.source_stats == {"openalex": 0}
    assert result.errors == []


@pytest.mark.asyncio
async def test_empty_coci_result() -> None:
    http = FakeHTTP(
        pages={"*": _page(next_cursor=None)},
        lookup={"doi:" + CITING_DOI: {"results": [_oa_item("W500")]}},
        coci=[],
    )

    result = await fetch_citing_papers(CITING_DOI, http=http)

    assert result.papers == []
    assert result.source_stats == {"openalex": 0, "opencitations": 0}


@pytest.mark.asyncio
async def test_unknown_source_name_fails_soft() -> None:
    http = FakeHTTP(pages={"*": _page(_oa_item("W1"), next_cursor=None)})

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex", "bogus-source"], http=http)

    assert len(result.papers) == 1
    assert len(result.errors) == 1
    assert result.errors[0].source == "bogus-source"
    assert "unknown source" in result.errors[0].message


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openalex_field_mapping() -> None:
    authors = ["Vaswani, Ashish", "Shazeer, Noam"]
    item = _oa_item(
        "W7",
        title="Attention is all you need",
        year=2017,
        doi="10.48550/arXiv.1706.03762",
        venue="NeurIPS",
        authors=authors,
    )
    http = FakeHTTP(pages={"*": _page(item, next_cursor=None)})

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http)

    paper = result.papers[0]
    assert paper.citing_paper_id == "W7"
    assert paper.doi == "10.48550/arXiv.1706.03762"  # https://doi.org/ stripped
    assert paper.title == "Attention is all you need"
    assert paper.year == 2017
    assert paper.venue == "NeurIPS"
    assert paper.authors_raw == authors
    assert len(paper.authors_detail) == 2
    assert paper.authors_detail[0]["author"]["id"] == "https://openalex.org/A1"
    assert paper.authors_detail[0]["institutions"][0]["display_name"] == "Inst 1"


@pytest.mark.asyncio
async def test_openalex_missing_fields_map_to_none() -> None:
    http = FakeHTTP(pages={"*": _page({"id": "https://openalex.org/W9"}, next_cursor=None)})

    result = await fetch_citing_papers(CITING_W_ID, sources=["openalex"], http=http)

    paper = result.papers[0]
    assert paper.citing_paper_id == "W9"
    assert paper.doi is None
    assert paper.title is None
    assert paper.year is None
    assert paper.venue is None
    assert paper.authors_raw == []
    assert paper.authors_detail == []


# ---------------------------------------------------------------------------
# Result dataclass defaults
# ---------------------------------------------------------------------------


def test_result_defaults() -> None:
    result = CitingResult()
    assert result.papers == []
    assert result.resume_cursor is None
    assert result.source_stats == {}
    assert result.errors == []


def test_citing_paper_defaults() -> None:
    paper = CitingPaper(citing_paper_id="W1")
    assert paper.doi is None
    assert paper.title is None
    assert paper.year is None
    assert paper.venue is None
    assert paper.authors_raw == []
    assert paper.authors_detail == []
