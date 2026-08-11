"""CLI tests for the WP6 author identity commands (paper author ...).

Offline: a fake fetcher is injected into :func:`cli_author._build_resolver`
and the SQLite backend is a real temp database, so the CLI exercises the
full storage + resolver path without any network.

Covers: command-tree registration, resolve/profile/search/confirm happy
paths, the I8 confirm→resolve reuse flow, and the exit-2 error contract.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

# rich's Console picks up COLUMNS at creation time; the author CLI's console
# is created at import, so the env must be set before importing it (a wide
# table otherwise gets truncated to the 80-column CliRunner default).
os.environ.setdefault("COLUMNS", "240")

from academic_intelligence.cli import app  # noqa: E402
from academic_intelligence.cli_author import _build_resolver  # noqa: E402
from academic_intelligence.core.models import AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.identity import Resolver
from academic_intelligence.identity.fetcher import WorksContext
from academic_intelligence.identity.models import (
    AuthorCandidate,
    AuthorProfile,
    RepresentativePaper,
)
from academic_intelligence.storage.sqlite_store import SQLiteStorage

runner = CliRunner()

OPENALEX_ID = "A5110986785"


def _ev() -> Evidence:
    return Evidence(source=SourceType.ARXIV, source_url="https://arxiv.org/abs/x")


def _paper(
    paper_id: str = "2403.05525",
    name: str = "Haoyu Lu",
    *,
    author_id: str | None = None,
    coauthors: list[str] | None = None,
) -> Paper:
    byline = [name] + (coauthors or [])
    return Paper(
        id=paper_id,
        title="DeepSeek-VL",
        year=2024,
        arxiv_id=paper_id,
        authors=[
            AuthorRef(
                name=author,
                author_id=author_id if author == name else None,
                position=i,
            )
            for i, author in enumerate(byline, 1)
        ],
        evidence_list=[_ev()],
    )


def _profile() -> AuthorProfile:
    return AuthorProfile(
        name="Haoyu Lu",
        author_id=OPENALEX_ID,
        source="openalex",
        affiliation="DeepSeek-AI",
        h_index=20,
        interests=["multimodal learning"],
        profile_url=f"https://openalex.org/{OPENALEX_ID}",
        representative_papers=[
            RepresentativePaper(title="DeepSeek-VL", year=2024, cited_by_count=800),
            RepresentativePaper(title="DeepSeek LLM", year=2024, cited_by_count=120),
        ],
        evidence=[],
    )


class _FakeFetcher:
    def __init__(self) -> None:
        self.profiles: dict[tuple[str, str], AuthorProfile | None] = {}
        self.searches: dict[str, list[AuthorCandidate]] = {}
        self.contexts: dict[tuple[str, str], WorksContext] = {}

    async def fetch_profile(self, author_id: str, source: str) -> AuthorProfile | None:
        return self.profiles.get((author_id, source))

    async def fetch_by_orcid(self, orcid: str) -> AuthorProfile | None:
        return None

    async def search(self, name: str, source: str, limit: int = 25) -> list[AuthorCandidate]:
        return list(self.searches.get(source, []))

    async def works_context(
        self, author_id: str, source: str, limit: int = 25
    ) -> WorksContext:
        return self.contexts.get((author_id, source), WorksContext())


@pytest.fixture
def fetcher() -> _FakeFetcher:
    return _FakeFetcher()


def _seed(db: Path, *papers: Paper) -> None:
    """Seed papers into the temp database (own connection, then closed)."""

    async def _run() -> None:
        store = SQLiteStorage(str(db))
        await store.connect()
        try:
            for paper in papers:
                await store.save_paper(paper)
        finally:
            await store.close()

    import asyncio

    asyncio.run(_run())


def _install_fake(monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher) -> None:
    """Route the CLI's resolver builder to a fake-fetcher resolver."""

    def fake_build(ai: object) -> Resolver:
        storage = ai.storage  # type: ignore[attr-defined]
        return Resolver(storage, fetcher=fetcher)

    monkeypatch.setattr(_build_resolver.__module__ + "._build_resolver", fake_build)


# ---------------------------------------------------------------------------
# Command tree
# ---------------------------------------------------------------------------


def test_author_group_registered() -> None:
    result = runner.invoke(app, ["author", "--help"])
    assert result.exit_code == 0
    for command in ("resolve", "profile", "search", "confirm"):
        assert command in result.stdout


def test_root_help_lists_author() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "author" in result.stdout


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_id_linked_outputs_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper(author_id=OPENALEX_ID))
    fetcher.profiles[(OPENALEX_ID, "openalex")] = _profile()
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app, ["author", "resolve", "2403.05525", "Haoyu Lu", "--storage-path", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert "ID 直连源档案" in result.output
    assert "DeepSeek-AI" in result.output
    assert "代表论文" in result.output
    assert "openalex:A5110986785" in result.output or "A5110986785" in result.output


def test_resolve_paper_not_found_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper())
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app, ["author", "resolve", "nope", "Haoyu Lu", "--storage-path", str(db)]
    )

    assert result.exit_code == 2
    assert "未找到论文" in result.output


def test_resolve_not_found_no_candidates_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper())
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app, ["author", "resolve", "2403.05525", "Haoyu Lu", "--storage-path", str(db)]
    )

    assert result.exit_code == 2
    assert "未找到候选" in result.output


def test_resolve_ambiguous_outputs_candidate_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(
        db,
        Paper(
            id="2403.05525",
            title="Some Paper",
            year=2024,
            authors=[
                AuthorRef(name="J. Wang", affiliation="Tsinghua University", position=1),
                AuthorRef(name="Alice", position=2),
            ],
            evidence_list=[_ev()],
        ),
    )
    fetcher.searches["openalex"] = [
        AuthorCandidate(
            candidate_id="openalex:A10",
            source="openalex",
            name="J. Wang",
            affiliation="Tsinghua University",
            interests=["medicine"],
            coauthors=["Alice"],
            citations=100,
        )
    ]
    fetcher.contexts[("A10", "openalex")] = WorksContext(
        coauthors=["Alice"], active_years=[2024]
    )
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app, ["author", "resolve", "2403.05525", "J. Wang", "--storage-path", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert "候选对比表" in result.output
    assert "待确认" in result.output
    assert "openalex:A10" in result.output


def test_resolve_outputs_json_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    out = tmp_path / "res.json"
    _seed(db, _paper(author_id=OPENALEX_ID))
    fetcher.profiles[(OPENALEX_ID, "openalex")] = _profile()
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app,
        [
            "author", "resolve", "2403.05525", "Haoyu Lu",
            "--storage-path", str(db), "--output", str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    assert out.exists()
    import json

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["match"] == "id_linked"
    assert payload["author_name"] == "Haoyu Lu"


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


def test_profile_outputs_representative_papers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper())
    fetcher.profiles[(OPENALEX_ID, "openalex")] = _profile()
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app, ["author", "profile", OPENALEX_ID, "--storage-path", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert "DeepSeek-AI" in result.output
    assert "DeepSeek-VL" in result.output
    assert "800" in result.output  # 引用数排序首篇


def test_profile_author_not_found_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper())
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app, ["author", "profile", "A999", "--storage-path", str(db)]
    )

    assert result.exit_code == 2
    assert "未找到" in result.output


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_disambiguate_outputs_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper())
    fetcher.searches["openalex"] = [
        AuthorCandidate(
            candidate_id="openalex:A1",
            source="openalex",
            name="Haoyu Lu",
            affiliation="DeepSeek-AI",
            citations=500,
        ),
        AuthorCandidate(
            candidate_id="openalex:A2",
            source="openalex",
            name="Haoyu Lu",
            affiliation="Peking University",
            citations=10,
        ),
    ]
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app,
        [
            "author", "search", "Haoyu Lu", "--disambiguate",
            "--storage-path", str(db),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "候选对比表" in result.output
    assert "DeepSeek-AI" in result.output
    assert "综合分" in result.output


def test_search_no_results_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper())
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app, ["author", "search", "Nobody", "--storage-path", str(db)]
    )

    assert result.exit_code == 2
    assert "未找到" in result.output


# ---------------------------------------------------------------------------
# confirm → resolve reuse (I8)
# ---------------------------------------------------------------------------


def test_confirm_then_resolve_hits_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper(author_id=None))
    fetcher.profiles[(OPENALEX_ID, "openalex")] = _profile()
    _install_fake(monkeypatch, fetcher)

    confirm = runner.invoke(
        app,
        [
            "author", "confirm", f"openalex:{OPENALEX_ID}",
            "--for", "2403.05525", "--name", "Haoyu Lu",
            "--storage-path", str(db),
        ],
    )
    assert confirm.exit_code == 0, confirm.output
    assert "已确认" in confirm.output
    assert OPENALEX_ID in confirm.output

    resolve = runner.invoke(
        app, ["author", "resolve", "2403.05525", "Haoyu Lu", "--storage-path", str(db)]
    )
    assert resolve.exit_code == 0, resolve.output
    assert "已确认身份" in resolve.output
    assert OPENALEX_ID in resolve.output


def test_confirm_requires_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper())
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app, ["author", "confirm", f"openalex:{OPENALEX_ID}", "--storage-path", str(db)]
    )

    assert result.exit_code != 0
    assert "--for" in result.output or "Missing option" in result.output


def test_confirm_rejects_local_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher
) -> None:
    db = tmp_path / "t.db"
    _seed(db, _paper())
    _install_fake(monkeypatch, fetcher)

    result = runner.invoke(
        app,
        [
            "author", "confirm", "local:abc",
            "--for", "2403.05525", "--name", "Haoyu Lu",
            "--storage-path", str(db),
        ],
    )

    assert result.exit_code == 2
    assert "未知来源" in result.output
