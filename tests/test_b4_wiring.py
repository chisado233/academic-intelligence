"""B4: source adapter wiring + IEEE graceful degradation + top-level exports.

Verifies that the arXiv / PubMed / IEEE adapters are registered by
``AcademicIntelligence._build_sources`` (3A v2 §5.3), that the default
source set is unchanged, and that IEEE degrades gracefully without a key.
"""

from __future__ import annotations

import logging

import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.exceptions import AllSourcesFailedError
from academic_intelligence.core.types import Config

_QUERY_METHODS = (
    "search_papers",
    "get_paper_by_doi",
    "get_author_papers",
    "get_author_profile",
    "get_citations",
)


def test_build_sources_registers_arxiv_pubmed_ieee_openalex() -> None:
    ai = AcademicIntelligence()
    sources = ai._build_sources(["arxiv", "pubmed", "ieee", "openalex"])
    assert set(sources) == {"arxiv", "pubmed", "ieee", "openalex"}
    for source in sources.values():
        for method in _QUERY_METHODS:
            assert callable(getattr(source, method, None)), method


def test_default_google_scholar_source_is_disabled_by_flag() -> None:
    ai = AcademicIntelligence()
    assert ai.config.sources == ["semantic_scholar", "openalex", "google_scholar"]
    sources = ai._build_sources(ai.config.sources)
    assert set(sources) == {"semantic_scholar", "openalex"}


def test_source_aliases_expanded() -> None:
    ai = AcademicIntelligence(Config(enable_google_scholar=True))
    sources = ai._build_sources(["ss", "oa", "gs", "arxiv", "pubmed", "ieee"])
    assert set(sources) == {
        "semantic_scholar",
        "openalex",
        "google_scholar",
        "arxiv",
        "pubmed",
        "ieee",
    }


def test_ieee_without_key_registered_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    ai = AcademicIntelligence()
    with caplog.at_level(logging.WARNING, logger="academic_intelligence"):
        sources = ai._build_sources(["ieee"])
    assert "ieee" in sources
    assert any("IEEE" in r.message for r in caplog.records), "missing IEEE key warning"


def test_config_ieee_api_key_field() -> None:
    assert Config().ieee_api_key is None
    assert Config(ieee_api_key="k-123").ieee_api_key.get_secret_value() == "k-123"


def test_ieee_api_key_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IEEE_API_KEY", "env-ieee-key")
    ai = AcademicIntelligence(Config(sources=["ieee"]))
    assert ai.config.ieee_api_key.get_secret_value() == "env-ieee-key"


def test_ieee_explicit_key_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="academic_intelligence"):
        ai = AcademicIntelligence(Config(ieee_api_key="x"))
        ai._build_sources(["ieee"])
    assert not any("IEEE" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_ieee_no_key_degrades_at_query_time(tmp_path) -> None:
    """IEEE without a key: construction/connect do not crash; a query fails
    with the per-source reason collected by the orchestration layer."""
    ai = AcademicIntelligence(
        Config(
            sources=["ieee"],
            storage_type="json",
            storage_path=str(tmp_path / "ieee"),
            cache_enabled=False,
        )
    )
    await ai.connect()
    try:
        with pytest.raises(AllSourcesFailedError) as excinfo:
            await ai.collect_paper("transformer", sources=["ieee"])
        message = str(excinfo.value)
        assert "ieee" in message.lower()
        assert "API key" in message
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_connect_with_ieee_no_key_ok(tmp_path) -> None:
    ai = AcademicIntelligence(
        Config(
            sources=["ieee", "arxiv"],
            storage_type="json",
            storage_path=str(tmp_path / "ieee2"),
            cache_enabled=False,
        )
    )
    await ai.connect()  # must not raise
    try:
        assert set(ai._sources) == {"ieee", "arxiv"}
    finally:
        await ai.close()


def test_top_level_exports_disambiguator() -> None:
    from academic_intelligence import AuthorDisambiguator

    assert AuthorDisambiguator is not None


def test_import_academic_intelligence_ok() -> None:
    from academic_intelligence import AcademicIntelligence as AI

    assert AI is AcademicIntelligence
