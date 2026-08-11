"""Unit tests for the WP6 author identity resolver (identity/).

Covers every branch of :class:`Resolver` with a fake fetcher and a real
SQLite backend (the identity tables are storage-backed):

- resolve branch A (byline authority id → source profile) and its stale-id
  fallback;
- resolve branch B (disambiguated candidate comparison): paper-match
  identity evidence, feature scoring >= 0.85 判同 / 0.60..0.85 ambiguous
  (D3: never hard-merged) / < 0.60 不同人, empty search;
- I8 cross-paper reuse: confirm writes the global identity, a second
  resolve of the same name returns it directly (no source fetch);
- profile (representative papers sorted by citations) / search
  (disambiguated ordering) / confirm error paths;
- candidate-id parsing and storage round-trips.

All tests are offline (fake fetcher), matching the project convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from academic_intelligence.core.models import AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.identity import Resolver, parse_candidate_id
from academic_intelligence.identity.exceptions import (
    AuthorNotFoundError,
    PaperNotFoundError,
)
from academic_intelligence.identity.fetcher import WorksContext
from academic_intelligence.identity.models import (
    AuthorCandidate,
    AuthorProfile,
    RepresentativePaper,
)
from academic_intelligence.storage.sqlite_store import SQLiteStorage

OPENALEX_ID = "A5110986785"
ORCID = "0000-0001-2345-6789"


def _evidence() -> Evidence:
    return Evidence(source=SourceType.ARXIV, source_url="https://arxiv.org/abs/x")


def _paper(
    paper_id: str = "2403.05525",
    name: str = "Haoyu Lu",
    *,
    author_id: str | None = None,
    coauthors: list[str] | None = None,
    affiliation: str | None = None,
    venue: str | None = None,
    title: str = "DeepSeek-VL",
) -> Paper:
    byline = [name] + (coauthors or [])
    return Paper(
        id=paper_id,
        title=title,
        year=2024,
        arxiv_id=paper_id,
        venue=venue,
        authors=[
            AuthorRef(
                name=author,
                author_id=author_id if author == name else None,
                affiliation=affiliation if author == name else None,
                position=i,
            )
            for i, author in enumerate(byline, 1)
        ],
        evidence_list=[_evidence()],
    )


def _profile(author_id: str = OPENALEX_ID, name: str = "Haoyu Lu") -> AuthorProfile:
    return AuthorProfile(
        name=name,
        author_id=author_id,
        source="openalex",
        affiliation="DeepSeek-AI",
        h_index=20,
        citations=500,
        interests=["multimodal learning"],
        profile_url=f"https://openalex.org/{author_id}",
        representative_papers=[
            RepresentativePaper(title="DeepSeek-VL", year=2024, cited_by_count=800),
            RepresentativePaper(title="DeepSeek LLM", year=2024, cited_by_count=120),
            RepresentativePaper(title="Kimi k1.5", year=2025, cited_by_count=11),
        ],
        evidence=[],
    )


def _candidate(
    candidate_id: str = "openalex:A1",
    name: str = "Haoyu Lu",
    *,
    affiliation: str | None = "DeepSeek-AI",
    interests: list[str] | None = None,
    coauthors: list[str] | None = None,
    citations: int = 0,
) -> AuthorCandidate:
    return AuthorCandidate(
        candidate_id=candidate_id,
        source=candidate_id.split(":", 1)[0],
        name=name,
        affiliation=affiliation,
        interests=interests or [],
        coauthors=coauthors or [],
        citations=citations,
    )


class FakeFetcher:
    """Deterministic fetcher stub for offline resolver tests."""

    def __init__(
        self,
        *,
        profiles: dict[tuple[str, str], AuthorProfile | None] | None = None,
        orcid_profiles: dict[str, AuthorProfile | None] | None = None,
        searches: dict[str, list[AuthorCandidate]] | None = None,
        contexts: dict[tuple[str, str], WorksContext] | None = None,
    ) -> None:
        self.profiles = profiles or {}
        self.orcid_profiles = orcid_profiles or {}
        self.searches = searches or {}
        self.contexts = contexts or {}
        self.profile_calls = 0
        self.search_calls = 0
        self.context_calls = 0

    async def fetch_profile(self, author_id: str, source: str) -> AuthorProfile | None:
        self.profile_calls += 1
        return self.profiles.get((author_id, source))

    async def fetch_by_orcid(self, orcid: str) -> AuthorProfile | None:
        self.profile_calls += 1
        return self.orcid_profiles.get(orcid)

    async def search(self, name: str, source: str, limit: int = 25) -> list[AuthorCandidate]:
        self.search_calls += 1
        return list(self.searches.get(source, []))

    async def works_context(
        self, author_id: str, source: str, limit: int = 25
    ) -> WorksContext:
        self.context_calls += 1
        return self.contexts.get((author_id, source), WorksContext())


@pytest.fixture
async def store(tmp_path: Path):
    db = SQLiteStorage(str(tmp_path / "identity.db"))
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def make_resolver(store):
    def _make(fetcher: FakeFetcher) -> Resolver:
        return Resolver(store, fetcher=fetcher)

    return _make


# ---------------------------------------------------------------------------
# Branch A: byline authority id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_branch_a_id_linked_profile(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper(author_id=OPENALEX_ID))
    fetcher = FakeFetcher(profiles={(OPENALEX_ID, "openalex"): _profile()})
    resolver = make_resolver(fetcher)

    result = await resolver.resolve("2403.05525", "Haoyu Lu")

    assert result.match == "id_linked"
    assert result.profile is not None
    assert result.profile.affiliation == "DeepSeek-AI"
    assert result.profile.author_id == OPENALEX_ID
    assert fetcher.profile_calls == 1
    assert result.evidence_chain  # 证据链非空


@pytest.mark.asyncio
async def test_resolve_branch_a_stale_id_falls_back_to_disambiguation(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper(author_id=OPENALEX_ID))
    candidate = _candidate(coauthors=["Wen Liu", "Bo Zhang"])
    fetcher = FakeFetcher(
        profiles={(OPENALEX_ID, "openalex"): None},
        searches={"openalex": [candidate]},
        contexts={("A1", "openalex"): WorksContext(coauthors=["Wen Liu", "Bo Zhang"])},
    )
    resolver = make_resolver(fetcher)

    result = await resolver.resolve("2403.05525", "Haoyu Lu")

    # stale id → 回退分支 B：两个源都搜索过，并给出候选结论
    assert fetcher.search_calls == 2
    assert result.candidates
    assert result.match in {"auto", "ambiguous", "different"}


# ---------------------------------------------------------------------------
# Branch B: disambiguation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_branch_b_paper_match_identity_evidence(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper(author_id=None))
    candidate = _candidate(candidate_id="openalex:A5110986785", coauthors=["Wen Liu"])
    context = WorksContext(
        coauthors=["Wen Liu", "Bo Zhang"],
        active_years=[2024],
        titles=["DeepSeek-VL", "DeepSeek LLM"],
        arxiv_ids=["2403.05525"],
    )
    fetcher = FakeFetcher(
        searches={"openalex": [candidate], "s2": []},
        contexts={("A5110986785", "openalex"): context},
    )
    resolver = make_resolver(fetcher)

    result = await resolver.resolve("2403.05525", "Haoyu Lu")

    assert result.match == "auto"
    top = result.candidates[0]
    assert top.candidate_id == "openalex:A5110986785"
    assert top.paper_match is True
    assert top.score == pytest.approx(1.0)
    assert top.verdict == "same"
    # 证据链：著作命中说明
    assert any("著作列表包含本论文" in str(e.get("detail")) for e in result.evidence_chain)


@pytest.mark.asyncio
async def test_resolve_branch_b_feature_scoring_auto_same(
    store: SQLiteStorage, make_resolver
) -> None:
    """论文侧携带机构/合著者/年份/venue 上下文 → 特征分 >= 0.85 判同."""
    await store.save_paper(
        _paper(
            name="Wei Zhang",
            coauthors=["Alice", "Bob"],
            affiliation="Tsinghua University",
            venue="NeurIPS",
            title="Some Paper",
        )
    )
    candidate = _candidate(
        candidate_id="openalex:A9",
        name="Wei Zhang",
        affiliation="Tsinghua University",
        interests=[],  # 无方向数据 → topic 中性，总分 0.925 稳过 0.85
        coauthors=["Alice", "Bob"],
    )
    context = WorksContext(
        coauthors=["Alice", "Bob"], active_years=[2024], venues=["NeurIPS"]
    )
    fetcher = FakeFetcher(
        searches={"openalex": [candidate]},
        contexts={("A9", "openalex"): context},
    )
    resolver = make_resolver(fetcher)

    result = await resolver.resolve("2403.05525", "Wei Zhang")

    assert result.match == "auto"
    assert result.candidates[0].verdict == "same"
    assert result.candidates[0].score >= 0.85


@pytest.mark.asyncio
async def test_resolve_branch_b_ambiguous_never_hard_merges(
    store: SQLiteStorage, make_resolver
) -> None:
    """D3: 同名不同领域候选 → ambiguous 候选表，不硬合并、不写身份表."""
    await store.save_paper(
        _paper(name="J. Wang", affiliation="Tsinghua University", coauthors=["Alice"])
    )
    ambiguous_candidate = _candidate(
        candidate_id="openalex:A10",
        name="J. Wang",
        affiliation="Tsinghua University",
        interests=["medicine"],  # 机构/合著者一致但方向不同 → 0.60-0.85 带
        coauthors=["Alice"],
    )
    fetcher = FakeFetcher(
        searches={"openalex": [ambiguous_candidate], "s2": []},
        contexts={("A10", "openalex"): WorksContext(coauthors=["Alice"])},
    )
    resolver = make_resolver(fetcher)

    result = await resolver.resolve("2403.05525", "J. Wang")

    assert result.match == "ambiguous"
    assert all(c.verdict == "ambiguous" for c in result.candidates)
    assert 0.60 <= result.candidates[0].score < 0.85
    # 不硬合并：没有写入任何身份表行
    assert await store.get_author_identities_for_name("J. Wang") == []
    assert await store.get_author_identity("2403.05525", "J. Wang") is None


@pytest.mark.asyncio
async def test_resolve_branch_b_different_people(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper(name="Wei Zhang", coauthors=["Alice"]))
    candidate = _candidate(
        candidate_id="openalex:A11",
        name="Wei Zhang",
        affiliation="Stanford University",
        interests=["biomedical imaging"],
        coauthors=["Carol"],
    )
    fetcher = FakeFetcher(
        searches={"openalex": [candidate]},
        contexts={("A11", "openalex"): WorksContext(coauthors=["Carol"])},
    )
    resolver = make_resolver(fetcher)

    result = await resolver.resolve("2403.05525", "Wei Zhang")

    assert result.match == "different"
    assert result.candidates[0].verdict == "different"
    assert result.candidates[0].score < 0.60


@pytest.mark.asyncio
async def test_resolve_branch_b_no_candidates(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper())
    resolver = make_resolver(FakeFetcher(searches={"openalex": [], "s2": []}))

    result = await resolver.resolve("2403.05525", "Haoyu Lu")

    assert result.match == "not_found"
    assert result.candidates == []


# ---------------------------------------------------------------------------
# I8: cross-paper identity reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_then_resolve_hits_confirmed_identity_without_fetch(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper(author_id=None))
    fetcher = FakeFetcher(
        profiles={(OPENALEX_ID, "openalex"): _profile()},
        searches={"openalex": [], "s2": []},
    )
    resolver = make_resolver(fetcher)

    confirmed = await resolver.confirm(
        f"openalex:{OPENALEX_ID}", "2403.05525", "Haoyu Lu", confirmed_by="test"
    )
    assert confirmed.status == "confirmed"
    assert confirmed.author_id == OPENALEX_ID

    result = await resolver.resolve("2403.05525", "Haoyu Lu")

    assert result.match == "confirmed"
    assert result.profile is not None
    assert result.profile.author_id == OPENALEX_ID
    # I8：直接命中全局身份——分支 B 的源搜索不再发生
    assert fetcher.search_calls == 0
    # 全局 + 论文级两表都已写
    rows = await store.get_author_identities_for_name("Haoyu Lu")
    assert len(rows) == 1 and rows[0]["status"] == "confirmed"
    assert await store.get_author_identity("2403.05525", "Haoyu Lu") is not None


@pytest.mark.asyncio
async def test_cross_paper_reuse_same_name_two_papers(
    store: SQLiteStorage, make_resolver
) -> None:
    """Q6: 两篇论文的 Daya Guo 经确认后指向同一身份."""
    await store.save_paper(
        _paper("2501.12948", "Daya Guo", coauthors=["Qihao Zhu"], title="DeepSeek-R1")
    )
    await store.save_paper(
        _paper("2401.02954", "Daya Guo", coauthors=["Xiao Bi"], title="DeepSeek LLM")
    )
    resolver = make_resolver(FakeFetcher(searches={"openalex": [], "s2": []}))

    await resolver.confirm(
        "openalex:A5060364305", "2501.12948", "Daya Guo", confirmed_by="test"
    )
    second = await resolver.resolve("2401.02954", "Daya Guo")

    assert second.match == "confirmed"
    assert second.profile is None or second.profile.author_id == "A5060364305"
    rows = await store.get_author_identities_for_name("Daya Guo")
    assert len(rows) == 1 and rows[0]["author_id"] == "A5060364305"
    # 证据链接：confirm 的论文有链接；resolve 只读，不另写
    assert await store.get_author_identity("2501.12948", "Daya Guo") is not None
    assert await store.get_author_identity("2401.02954", "Daya Guo") is None


@pytest.mark.asyncio
async def test_multiple_confirmed_identities_are_ambiguous(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper(name="Wei Zhang"))
    await store.save_author_identity_global(
        author_name="Wei Zhang",
        author_id="A1",
        source="openalex",
        status="confirmed",
        confidence=1.0,
        confirmed_by="test",
    )
    await store.save_author_identity_global(
        author_name="Wei Zhang",
        author_id="A2",
        source="openalex",
        status="confirmed",
        confidence=1.0,
        confirmed_by="test",
    )
    resolver = make_resolver(FakeFetcher(searches={"openalex": [], "s2": []}))

    result = await resolver.resolve("2403.05525", "Wei Zhang")

    assert result.match == "ambiguous"
    assert len(result.candidates) == 2
    assert all(c.verdict == "confirmed" for c in result.candidates)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_paper_not_found(store: SQLiteStorage, make_resolver) -> None:
    resolver = make_resolver(FakeFetcher())
    with pytest.raises(PaperNotFoundError):
        await resolver.resolve("nope", "Haoyu Lu")


@pytest.mark.asyncio
async def test_resolve_author_not_in_paper(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper())
    resolver = make_resolver(FakeFetcher())
    with pytest.raises(AuthorNotFoundError):
        await resolver.resolve("2403.05525", "Nobody Here")


# ---------------------------------------------------------------------------
# profile / search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_sorts_representative_papers_by_citations(
    store: SQLiteStorage, make_resolver
) -> None:
    resolver = make_resolver(
        FakeFetcher(profiles={(OPENALEX_ID, "openalex"): _profile()})
    )

    profile = await resolver.profile(OPENALEX_ID, "openalex")

    assert profile.author_id == OPENALEX_ID
    cited = [p.cited_by_count for p in profile.representative_papers]
    assert cited == sorted(cited, reverse=True)


@pytest.mark.asyncio
async def test_profile_author_not_found(store: SQLiteStorage, make_resolver) -> None:
    resolver = make_resolver(
        FakeFetcher(profiles={("A999", "openalex"): None})
    )
    with pytest.raises(AuthorNotFoundError):
        await resolver.profile("A999", "openalex")


@pytest.mark.asyncio
async def test_search_disambiguated_ordering(
    store: SQLiteStorage, make_resolver
) -> None:
    low = _candidate(
        candidate_id="openalex:A2", name="Haoyu Lu", affiliation="Peking University", citations=10
    )
    high = _candidate(
        candidate_id="openalex:A1", name="Haoyu Lu", affiliation="DeepSeek-AI", citations=500
    )
    resolver = make_resolver(
        FakeFetcher(searches={"openalex": [low, high], "s2": []})
    )

    scored = await resolver.search("Haoyu Lu", disambiguate=True, limit=5)

    # 同分（同名中性特征一致）→ 引用数更高的排前
    assert [c.candidate_id for c in scored] == ["openalex:A1", "openalex:A2"]
    assert all(c.score == pytest.approx(0.675) for c in scored)
    assert all(c.verdict == "ambiguous" for c in scored)


@pytest.mark.asyncio
async def test_search_without_disambiguate_keeps_source_order(
    store: SQLiteStorage, make_resolver
) -> None:
    first = _candidate(candidate_id="openalex:A2", name="Haoyu Lu")
    second = _candidate(candidate_id="s2:1", name="Haoyu Lu")
    resolver = make_resolver(
        FakeFetcher(searches={"openalex": [first], "s2": [second]})
    )

    results = await resolver.search("Haoyu Lu", disambiguate=False, limit=5)

    assert [c.candidate_id for c in results] == ["openalex:A2", "s2:1"]
    assert all(c.score is None for c in results)


@pytest.mark.asyncio
async def test_search_rejects_non_positive_limit(
    store: SQLiteStorage, make_resolver
) -> None:
    resolver = make_resolver(FakeFetcher())
    with pytest.raises(ValueError):
        await resolver.search("Haoyu Lu", limit=0)


# ---------------------------------------------------------------------------
# confirm error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_paper_not_found(store: SQLiteStorage, make_resolver) -> None:
    resolver = make_resolver(FakeFetcher())
    with pytest.raises(PaperNotFoundError):
        await resolver.confirm("openalex:A1", "nope", "Haoyu Lu")


@pytest.mark.asyncio
async def test_confirm_author_not_in_paper(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper())
    resolver = make_resolver(FakeFetcher())
    with pytest.raises(AuthorNotFoundError):
        await resolver.confirm("openalex:A1", "2403.05525", "Ghost")


@pytest.mark.asyncio
async def test_confirm_rejects_unknown_candidate_id_form(
    store: SQLiteStorage, make_resolver
) -> None:
    await store.save_paper(_paper())
    resolver = make_resolver(FakeFetcher())
    with pytest.raises(ValueError):
        await resolver.confirm("local:abc", "2403.05525", "Haoyu Lu")


# ---------------------------------------------------------------------------
# Candidate id parsing
# ---------------------------------------------------------------------------


def test_parse_candidate_id_forms() -> None:
    assert parse_candidate_id("openalex:A123") == ("openalex", "A123")
    assert parse_candidate_id("s2:12345") == ("s2", "12345")
    assert parse_candidate_id("orcid:0000-0001-2345-6789") == (
        "orcid",
        "0000-0001-2345-6789",
    )
    assert parse_candidate_id("https://openalex.org/A123/") == ("openalex", "A123")
    assert parse_candidate_id("A123") == ("openalex", "A123")


def test_parse_candidate_id_rejects_unknown() -> None:
    for bad in ("local:abc", "foo:1", "not-an-id"):
        with pytest.raises(ValueError):
            parse_candidate_id(bad)


# ---------------------------------------------------------------------------
# Storage round-trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_storage_roundtrip(store: SQLiteStorage) -> None:
    await store.save_author_identity_global(
        author_name="Haoyu Lu",
        author_id=OPENALEX_ID,
        source="openalex",
        status="confirmed",
        confidence=0.99,
        confirmed_by="test",
    )
    row = await store.get_author_identity_global("Haoyu Lu", OPENALEX_ID, "openalex")
    assert row is not None
    assert row["status"] == "confirmed"
    assert row["confidence"] == pytest.approx(0.99)

    # 幂等 upsert
    await store.save_author_identity_global(
        author_name="Haoyu Lu",
        author_id=OPENALEX_ID,
        source="openalex",
        status="confirmed",
        confidence=1.0,
        confirmed_by="test2",
    )
    row = await store.get_author_identity_global("Haoyu Lu", OPENALEX_ID, "openalex")
    assert row is not None and row["confidence"] == pytest.approx(1.0)

    await store.save_author_identity(
        paper_id="2403.05525",
        author_name="Haoyu Lu",
        author_id=OPENALEX_ID,
        source="openalex",
    )
    link = await store.get_author_identity("2403.05525", "Haoyu Lu")
    assert link is not None and link["author_id"] == OPENALEX_ID


@pytest.mark.asyncio
async def test_identity_tables_created_incrementally(tmp_path: Path) -> None:
    """迁移：新表由 create_all 增量创建，旧表不动（T10-m 语义）."""
    db_path = tmp_path / "migrate.db"
    store = SQLiteStorage(str(db_path))
    await store.connect()
    try:
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "author_identity_global" in tables
        assert "author_identity" in tables
        assert "papers" in tables  # 旧表仍在
    finally:
        await store.close()


def test_authority_classification() -> None:
    from academic_intelligence.identity.resolver import _classify_authority

    assert _classify_authority("A5110986785") == ("openalex", "A5110986785")
    assert _classify_authority("https://openalex.org/A5110986785") == (
        "openalex",
        "A5110986785",
    )
    assert _classify_authority("1234567") == ("s2", "1234567")
    assert _classify_authority(ORCID) == ("orcid", ORCID)
    assert _classify_authority("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5") is None
    assert _classify_authority(None) is None
