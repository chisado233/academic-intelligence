"""Tests for the cross-entity affiliation-conflict detection (2026-08-13).

Motivation (A/B experiment finding): OpenAlex can misattribute a work to
the wrong author entity — MBLLEN (W2893333553) carries ``Feng Lu`` with
byline institution *Beihang University*, yet OpenAlex links that byline
to A5101480749 (an entity whose home institutions are CMA / CAS, a GIS
scholar).  ``paper author profile A5050527785`` (the real Beihang Lu)
therefore never sees MBLLEN, and "most-cited paper" conclusions built on
the single entity are wrong.

The fix: ``SourceFetcher.find_sibling_entities`` scans same-name OpenAlex
entities and flags works whose *byline institution of that same author*
contains the primary entity's institution — a reliable, automatic signal
of misattribution (the byline institution disagrees with the entity's
home institutions).  Flags are advisory only; nothing is auto-merged
(the 2026-08-12 decision keeps merging manual via ``author confirm``).

Coverage:

- models: ``EntityFlag`` shape / ``AuthorProfile.entity_flags`` default +
  ``to_dict`` compatibility;
- fetcher: hit (affiliation conflict), miss, fail-soft on candidate
  errors, empty-candidate case;
- resolver: ``profile()`` (openalex branch) attaches flags; s2 branch
  does not call the sibling scan;
- CLI: ``author profile`` renders the warning block (and stays clean
  when there are no flags).

All tests are offline (fake HTTP client / fake fetcher).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from academic_intelligence.cli import app as author_app
from academic_intelligence.cli_author import _build_resolver
from academic_intelligence.core.types import Config, SourceType
from academic_intelligence.identity import Resolver
from academic_intelligence.identity.fetcher import SourceFetcher
from academic_intelligence.identity.models import (
    AuthorProfile,
    EntityFlag,
    RepresentativePaper,
)
from academic_intelligence.storage.sqlite_store import SQLiteStorage

OPENALEX_ID = "A5050527785"      # the real Beihang Feng Lu
SIBLING_ID = "A5101480749"       # same-name entity (CMA/CAS GIS scholar)
HOME_INST = "Beihang University"

runner = CliRunner()


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def test_entity_flag_shape() -> None:
    flag = EntityFlag(
        entity_id=SIBLING_ID,
        entity_affiliation="Chinese Academy of Sciences",
        reason="affiliation_conflict",
        flagged_papers=[
            RepresentativePaper(
                title="MBLLEN", year=2018, cited_by_count=316, work_id="W2893333553"
            )
        ],
    )
    assert flag.entity_id == SIBLING_ID
    assert flag.reason == "affiliation_conflict"
    assert flag.flagged_papers[0].work_id == "W2893333553"


def test_author_profile_entity_flags_default_and_roundtrip() -> None:
    profile = AuthorProfile(name="Feng Lu", author_id=OPENALEX_ID, source="openalex")
    assert profile.entity_flags == []
    data = profile.to_dict()
    assert "entity_flags" in data
    assert data["entity_flags"] == []

    flag = EntityFlag(
        entity_id=SIBLING_ID,
        entity_affiliation="Chinese Academy of Sciences",
        reason="affiliation_conflict",
        flagged_papers=[
            RepresentativePaper(title="MBLLEN", year=2018, cited_by_count=316)
        ],
    )
    profile.entity_flags = [flag]
    data = profile.to_dict()
    assert data["entity_flags"][0]["entity_id"] == SIBLING_ID
    assert data["entity_flags"][0]["flagged_papers"][0]["title"] == "MBLLEN"


# ---------------------------------------------------------------------------
# fetcher: fake HTTP client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self) -> object:
        return self._payload


class _FakeHTTPClient:
    """Route OpenAlex URLs to canned responses for the sibling scan."""

    def __init__(self) -> None:
        self.authors_search: dict[str, object] = {}
        self.works_by_author: dict[str, object] = {}
        self.calls: list[tuple[str, dict | None, dict | None]] = []
        self.fail_works_for: set[str] = set()

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get(
        self, url: str, *, headers: dict | None = None, params: dict | None = None
    ) -> _FakeResponse:
        self.calls.append((url, headers, params))
        if "/authors" in url and "display_name.search" in str(params or {}):
            return _FakeResponse(self.authors_search)
        if "/works" in url and params:
            author_id = str(params.get("filter", "")).replace("author.id:", "")
            if author_id in self.fail_works_for:
                return _FakeResponse({"error": "boom"}, status_code=500)
            return _FakeResponse(self.works_by_author.get(author_id, {"results": []}))
        return _FakeResponse({"results": []})


def _home_entity() -> dict:
    return {
        "id": f"https://openalex.org/{OPENALEX_ID}",
        "display_name": "Feng Lu",
        "orcid": "https://orcid.org/0000-0001-9064-7964",
        "last_known_institutions": [{"display_name": HOME_INST}],
        "works_count": 274,
        "cited_by_count": 7796,
        "summary_stats": {"h_index": 45},
        "homepage": None,
    }


def _sibling_entity() -> dict:
    return {
        "id": f"https://openalex.org/{SIBLING_ID}",
        "display_name": "Feng Lu",
        "orcid": "https://orcid.org/0000-0001-6573-2550",
        "last_known_institutions": [
            {"display_name": "Chinese Academy of Sciences"},
            {"display_name": "China Meteorological Administration"},
        ],
        "works_count": 382,
        "cited_by_count": 7786,
        "summary_stats": {"h_index": 38},
        "homepage": None,
    }


def _sibling_work(
    title: str, cited: int, year: int, *, byline_inst: str
) -> dict:
    # A work by the *sibling* author whose byline institution may disagree
    # with the sibling entity's home institutions (the misattribution
    # signal we scan for).
    return {
        "id": f"https://openalex.org/W{abs(hash(title)) % 10**9}",
        "title": title,
        "publication_year": year,
        "cited_by_count": cited,
        "primary_location": {"source": {"display_name": "Somewhere"}},
        "doi": None,
        "authorships": [
            {
                "author": {"display_name": "Feng Lu", "id": f"https://openalex.org/{SIBLING_ID}"},
                "institutions": [{"display_name": byline_inst}] if byline_inst else [],
                "raw_affiliation_strings": [byline_inst] if byline_inst else [],
            },
            {
                "author": {"display_name": "Someone Else", "id": None},
                "institutions": [],
                "raw_affiliation_strings": [],
            },
        ],
    }


def _make_fetcher(client: _FakeHTTPClient) -> SourceFetcher:
    return SourceFetcher(http_client=client)


def _profile_with_home_entity() -> AuthorProfile:
    return AuthorProfile(
        name="Feng Lu",
        author_id=OPENALEX_ID,
        source="openalex",
        affiliation=HOME_INST,
        h_index=45,
        citations=7796,
        paper_count=274,
        profile_url=f"https://openalex.org/{OPENALEX_ID}",
        representative_papers=[
            RepresentativePaper(
                title="Understanding adversarial attacks...",
                year=2020,
                cited_by_count=540,
                work_id="W3021182036",
            )
        ],
    )


async def _run_find(profile: AuthorProfile, client: _FakeHTTPClient) -> list[EntityFlag]:
    fetcher = _make_fetcher(client)
    return await fetcher.find_sibling_entities(profile)


@pytest.mark.asyncio
async def test_find_sibling_hit_on_affiliation_conflict() -> None:
    client = _FakeHTTPClient()
    client.authors_search = {"results": [_sibling_entity(), _home_entity()]}
    client.works_by_author[SIBLING_ID] = {
        "results": [
            _sibling_work("MBLLEN", 316, 2018, byline_inst=HOME_INST),
            _sibling_work("GIS paper", 900, 2017, byline_inst="Institute of Geographic Sciences"),
        ]
    }
    flags = await _run_find(_profile_with_home_entity(), client)
    assert len(flags) == 1
    flag = flags[0]
    assert flag.entity_id == SIBLING_ID
    assert flag.reason == "affiliation_conflict"
    # MBLLEN byline == home institution → flagged; GIS paper (CAS) not.
    titles = [p.title for p in flag.flagged_papers]
    assert "MBLLEN" in titles
    assert "GIS paper" not in titles
    assert flag.flagged_papers[0].work_id is not None


@pytest.mark.asyncio
async def test_find_sibling_sorted_by_citations() -> None:
    client = _FakeHTTPClient()
    client.authors_search = {"results": [_sibling_entity(), _home_entity()]}
    client.works_by_author[SIBLING_ID] = {
        "results": [
            _sibling_work("Low impact", 10, 2019, byline_inst=HOME_INST),
            _sibling_work("High impact", 500, 2016, byline_inst=HOME_INST),
        ]
    }
    flags = await _run_find(_profile_with_home_entity(), client)
    assert flags[0].flagged_papers[0].title == "High impact"


@pytest.mark.asyncio
async def test_find_sibling_no_match_when_byline_differs() -> None:
    client = _FakeHTTPClient()
    client.authors_search = {"results": [_sibling_entity(), _home_entity()]}
    client.works_by_author[SIBLING_ID] = {
        "results": [
            _sibling_work("CAS paper", 900, 2017, byline_inst="Chinese Academy of Sciences")
        ]
    }
    flags = await _run_find(_profile_with_home_entity(), client)
    assert flags == []


@pytest.mark.asyncio
async def test_find_sibling_no_same_name_candidates() -> None:
    client = _FakeHTTPClient()
    client.authors_search = {"results": [_home_entity()]}  # only itself
    flags = await _run_find(_profile_with_home_entity(), client)
    assert flags == []


@pytest.mark.asyncio
async def test_find_sibling_fail_soft_on_candidate_works_error() -> None:
    client = _FakeHTTPClient()
    client.authors_search = {"results": [_sibling_entity(), _home_entity()]}
    client.fail_works_for.add(SIBLING_ID)
    flags = await _run_find(_profile_with_home_entity(), client)
    assert flags == []  # candidate failed → skipped, scan still completes


@pytest.mark.asyncio
async def test_find_sibling_fail_soft_on_search_error() -> None:
    class _BoomClient(_FakeHTTPClient):
        async def get(self, url: str, *, headers=None, params=None) -> _FakeResponse:
            return _FakeResponse({"error": "rate limited"}, status_code=429)

    flags = await _run_find(_profile_with_home_entity(), _BoomClient())
    assert flags == []


@pytest.mark.asyncio
async def test_find_sibling_skips_self_entity() -> None:
    client = _FakeHTTPClient()
    client.authors_search = {"results": [_home_entity()]}
    flags = await _run_find(_profile_with_home_entity(), client)
    assert flags == []


# ---------------------------------------------------------------------------
# resolver wiring
# ---------------------------------------------------------------------------


class _ResolverFakeFetcher:
    """Fake fetcher exposing the sibling scan for resolver wiring tests."""

    def __init__(self) -> None:
        self.profiles: dict[tuple[str, str], AuthorProfile | None] = {}
        self.sibling_flags: list[EntityFlag] = []
        self.sibling_called_for: list[str] = []

    async def fetch_profile(self, author_id: str, source: str) -> AuthorProfile | None:
        return self.profiles.get((author_id, source))

    async def fetch_by_orcid(self, orcid: str) -> AuthorProfile | None:
        return None

    async def search(self, name: str, source: str, limit: int = 25) -> list:
        return []

    async def works_context(
        self, author_id: str, source: str, limit: int = 25
    ) -> object:
        return type("WC", (), {"coauthors": [], "active_years": [], "venues": [], "arxiv_ids": [], "dois": [], "titles": []})()

    async def find_sibling_entities(self, profile: AuthorProfile) -> list[EntityFlag]:
        self.sibling_called_for.append(profile.author_id)
        return list(self.sibling_flags)


@pytest.fixture
async def store(tmp_path: Path):
    db = SQLiteStorage(str(tmp_path / "flags.db"))
    await db.connect()
    yield db


@pytest.mark.asyncio
async def test_resolver_profile_attaches_sibling_flags(
    store: SQLiteStorage,
) -> None:
    fetcher = _ResolverFakeFetcher()
    profile = _profile_with_home_entity()
    fetcher.profiles[(OPENALEX_ID, "openalex")] = profile
    flag = EntityFlag(
        entity_id=SIBLING_ID,
        entity_affiliation="Chinese Academy of Sciences",
        reason="affiliation_conflict",
        flagged_papers=[RepresentativePaper(title="MBLLEN", year=2018, cited_by_count=316)],
    )
    fetcher.sibling_flags = [flag]
    resolver = Resolver(store, fetcher=fetcher)
    async with resolver:
        result = await resolver.profile(OPENALEX_ID, "openalex")
    assert result.entity_flags == [flag]
    assert fetcher.sibling_called_for == [OPENALEX_ID]


@pytest.mark.asyncio
async def test_resolver_profile_s2_skips_sibling_scan(
    store: SQLiteStorage,
) -> None:
    fetcher = _ResolverFakeFetcher()
    fetcher.profiles[("12345", "s2")] = AuthorProfile(
        name="Feng Lu", author_id="12345", source="s2", affiliation=HOME_INST
    )
    resolver = Resolver(store, fetcher=fetcher)
    async with resolver:
        result = await resolver.profile("12345", "s2")
    assert result.entity_flags == []
    assert fetcher.sibling_called_for == []


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def _cli_profile(*, with_flags: bool) -> AuthorProfile:
    profile = _profile_with_home_entity()
    if with_flags:
        profile.entity_flags = [
            EntityFlag(
                entity_id=SIBLING_ID,
                entity_affiliation="Chinese Academy of Sciences",
                reason="affiliation_conflict",
                flagged_papers=[
                    RepresentativePaper(
                        title="MBLLEN: Low-Light Image/Video Enhancement Using CNNs",
                        year=2018,
                        cited_by_count=316,
                        work_id="W2893333553",
                    )
                ],
            )
        ]
    return profile


class _CliFakeFetcher(_ResolverFakeFetcher):
    pass


def _install_cli_fake(monkeypatch: pytest.MonkeyPatch, fetcher: _CliFakeFetcher) -> None:
    def fake_build(ai: object) -> Resolver:
        storage = ai.storage  # type: ignore[attr-defined]
        return Resolver(storage, fetcher=fetcher)

    monkeypatch.setattr(_build_resolver.__module__ + "._build_resolver", fake_build)


def _run_profile_cli(tmp_path: Path, fetcher: _CliFakeFetcher) -> str:
    from typer.testing import CliRunner as _Runner

    local_runner = _Runner()
    result = local_runner.invoke(
        author_app,
        ["author", "profile", OPENALEX_ID, "--storage-path", str(tmp_path / "cli.db")],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    return result.stdout


def test_cli_profile_renders_flag_warning_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _CliFakeFetcher()
    fetcher.profiles[(OPENALEX_ID, "openalex")] = _cli_profile(with_flags=True)
    fetcher.sibling_flags = _cli_profile(with_flags=True).entity_flags
    _install_cli_fake(monkeypatch, fetcher)
    out = _run_profile_cli(tmp_path, fetcher)
    assert "疑似归属" in out
    assert SIBLING_ID in out
    assert "MBLLEN" in out


def test_cli_profile_clean_without_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _CliFakeFetcher()
    fetcher.profiles[(OPENALEX_ID, "openalex")] = _cli_profile(with_flags=False)
    fetcher.sibling_flags = []
    _install_cli_fake(monkeypatch, fetcher)
    out = _run_profile_cli(tmp_path, fetcher)
    assert "疑似归属" not in out
    assert "Understanding adversarial" in out
