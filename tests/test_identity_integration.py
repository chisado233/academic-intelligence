"""Integration tests for WP6 author identity (cassette-replayed, offline).

The upgrade user-test-plan Q2 / Q3 / Q6 scenarios, driven by the real
recorded OpenAlex / Semantic Scholar responses in ``tests/cassettes/``:

- Q2: DeepSeek-VL 论文 2403.05525 的 "Haoyu Lu" → resolve（分支 B，
  候选著作命中本论文 → 判同 DeepSeek-AI 多模态研究员）；
- Q2 branch A: 论文 byline 携带 OpenAlex 作者 ID → 直连源档案；
- Q3: profile 返回完整档案，代表论文按引用数排序；
- Q6: 两篇 DeepSeek 论文的 Daya Guo → 确认后跨论文复用同一身份（I8）。

HTTP traffic is replayed from the JSON cassettes, so the suite runs fully
offline (project convention, technical-design §6 / user-test-plan §1.5).
"""

from __future__ import annotations

import pytest

from academic_intelligence.core.models import AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.identity import Resolver
from academic_intelligence.identity.fetcher import SourceFetcher
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from tests.cassette_replay import install_merged_cassettes

# OpenAlex author ids recorded in the cassettes.
HAOYU_LU_OPENALEX = "A5110986785"  # DeepSeek-VL 作者
DAYA_GUO_OPENALEX = "A5060364305"  # DeepSeek 作者


def _ev() -> Evidence:
    return Evidence(source=SourceType.ARXIV, source_url="https://arxiv.org/abs/x")


def _deepseek_vl_paper(author_id: str | None = None) -> Paper:
    """DeepSeek-VL 2403.05525（arxiv 来源 byline，无/有 author_id）."""
    byline = [
        "Haoyu Lu", "Wen Liu", "Bo Zhang", "Bingxuan Wang", "Kai Dong",
        "Bo Liu", "Jingxiang Sun", "Tongzheng Ren", "Zhuoshu Li", "Hao Yang",
        "Yaofeng Sun", "Chengqi Deng", "Hanwei Xu", "Zhenda Xie", "Chong Ruan",
    ]
    return Paper(
        id="2403.05525",
        title="DeepSeek-VL: Towards Real-World Vision-Language Understanding",
        year=2024,
        arxiv_id="2403.05525",
        authors=[
            AuthorRef(
                name=name,
                author_id=author_id if name == "Haoyu Lu" else None,
                position=i,
            )
            for i, name in enumerate(byline, 1)
        ],
        evidence_list=[_ev()],
    )


def _deepseek_r1_paper(paper_id: str) -> Paper:
    """DeepSeek-R1 2501.12948（Q6 用，Daya Guo byline）."""
    return Paper(
        id=paper_id,
        title="DeepSeek-R1 incentivizes reasoning in LLMs through reinforcement learning",
        year=2025,
        arxiv_id=paper_id,
        authors=[
            AuthorRef(name="Daya Guo", position=1),
            AuthorRef(name="Qihao Zhu", position=2),
            AuthorRef(name="Dejian Yang", position=3),
        ],
        evidence_list=[_ev()],
    )


async def _seed(db_path: str, *papers: Paper) -> None:
    store = SQLiteStorage(db_path)
    await store.connect()
    try:
        for paper in papers:
            await store.save_paper(paper)
    finally:
        await store.close()


def _make_resolver(db_path: str) -> Resolver:
    return Resolver(SQLiteStorage(db_path), fetcher=SourceFetcher())


# ---------------------------------------------------------------------------
# Q2: 2403.05525 "Haoyu Lu"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_q2_haoyu_lu_resolve_branch_b_paper_match(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """arxiv 入库（无 author_id）→ 分支 B：候选著作命中本论文 → 判同."""
    db = str(tmp_path / "q2.db")
    await _seed(db, _deepseek_vl_paper(author_id=None))
    install_merged_cassettes(
        monkeypatch,
        [
            "openalex_author_search",
            "openalex_author_works",
            "s2_author_search",
        ],
    )
    store = SQLiteStorage(db)
    await store.connect()
    try:
        resolver = Resolver(store, fetcher=SourceFetcher())
        result = await resolver.resolve("2403.05525", "Haoyu Lu")
    finally:
        await store.close()

    assert result.match == "auto"
    top = result.candidates[0]
    assert top.candidate_id == f"openalex:{HAOYU_LU_OPENALEX}"
    assert top.paper_match is True
    assert top.score == pytest.approx(1.0)
    assert top.verdict == "same"
    # 正确候选带 DeepSeek 团队背景（合著者/年份/venue 已富化）
    assert "Wen Liu" in top.coauthors
    assert 2024 in top.active_years
    # 证据链含著作命中说明
    assert any("著作列表包含本论文" in str(e.get("detail")) for e in result.evidence_chain)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_q2_haoyu_lu_resolve_branch_a_id_linked(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenAlex 入库（byline 带 author_id）→ 分支 A：直连源档案."""
    db = str(tmp_path / "q2a.db")
    await _seed(db, _deepseek_vl_paper(author_id=HAOYU_LU_OPENALEX))
    install_merged_cassettes(
        monkeypatch,
        ["openalex_author_profile", "openalex_author_works"],
    )
    store = SQLiteStorage(db)
    await store.connect()
    try:
        resolver = Resolver(store, fetcher=SourceFetcher())
        result = await resolver.resolve("2403.05525", "Haoyu Lu")
    finally:
        await store.close()

    assert result.match == "id_linked"
    assert result.profile is not None
    assert result.profile.author_id == HAOYU_LU_OPENALEX
    assert result.profile.name == "Haoyu Lu"
    # 代表论文按引用数降序
    cited = [p.cited_by_count for p in result.profile.representative_papers]
    assert cited == sorted(cited, reverse=True)
    assert result.profile.representative_papers  # 非空
    assert result.evidence_chain


# ---------------------------------------------------------------------------
# Q3: profile 完整档案
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_q3_profile_with_representative_papers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "q3.db")
    await _seed(db, _deepseek_vl_paper())
    install_merged_cassettes(
        monkeypatch,
        ["openalex_author_profile", "openalex_author_works"],
    )
    store = SQLiteStorage(db)
    await store.connect()
    try:
        resolver = Resolver(store, fetcher=SourceFetcher())
        profile = await resolver.profile(HAOYU_LU_OPENALEX, "openalex")
    finally:
        await store.close()

    assert profile.name == "Haoyu Lu"
    assert profile.author_id == HAOYU_LU_OPENALEX
    assert profile.source == "openalex"
    assert profile.h_index is not None
    assert profile.citations is not None
    assert profile.representative_papers
    # 代表作按引用数排序（Q3）
    cited = [p.cited_by_count for p in profile.representative_papers]
    assert cited == sorted(cited, reverse=True)
    assert profile.representative_papers[0].cited_by_count == max(cited)


# ---------------------------------------------------------------------------
# Q6: 两篇 DeepSeek 论文的 Daya Guo 同一人（confirm → 跨论文复用）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_q6_daya_guo_same_person_across_papers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "q6.db")
    await _seed(db, _deepseek_r1_paper("2501.12948"), _deepseek_r1_paper("2410.13353"))
    install_merged_cassettes(
        monkeypatch,
        [
            "openalex_author_search_daya",
            "openalex_author_profile_daya",  # 含 profile + works 两个交互
        ],
    )
    store = SQLiteStorage(db)
    await store.connect()
    try:
        resolver = Resolver(store, fetcher=SourceFetcher())
        first = await resolver.resolve("2501.12948", "Daya Guo")
        # OpenAlex 只有 1 个 Daya Guo → 著作命中 → 判同
        assert first.match == "auto"
        assert first.candidates[0].candidate_id == f"openalex:{DAYA_GUO_OPENALEX}"
        assert first.candidates[0].paper_match is True

        await resolver.confirm(
            f"openalex:{DAYA_GUO_OPENALEX}", "2501.12948", "Daya Guo",
            confirmed_by="test",
        )
        # 第二篇论文直接命中已确认身份（I8，跨论文复用，无需再拉源）
        second = await resolver.resolve("2410.13353", "Daya Guo")
    finally:
        await store.close()

    assert second.match == "confirmed"
    assert second.profile is None or second.profile.author_id == DAYA_GUO_OPENALEX
    rows = await _identity_rows(db, "Daya Guo")
    assert len(rows) == 1 and rows[0]["author_id"] == DAYA_GUO_OPENALEX
    assert rows[0]["status"] == "confirmed"


async def _identity_rows(db_path: str, name: str) -> list[dict]:
    store = SQLiteStorage(db_path)
    await store.connect()
    try:
        return await store.get_author_identities_for_name(name)
    finally:
        await store.close()
