"""FIX-Y tests.

Y-1  arXiv DOI queries return nothing: ``collect_paper("10.48550/arXiv.
     <id>", sources=["arxiv"])`` resolves via the API's ``doi:"..."`` field
     search, which never matches arXiv-native DOIs even though the Atom
     metadata carries them. The fix routes ``10.48550/arXiv.``-prefixed DOIs
     (case-insensitive) to the ``id_list`` lookup.
Y-2  HTTP cache is not persisted across sessions: ``Cache`` supports
     ``persistent=True + persist_path`` but ``AcademicIntelligence.connect()``
     only handed in ``Cache(ttl=...)``. The fix adds ``cache_persistent`` /
     ``cache_path`` to ``Config`` and wires them through ``connect()``;
     default behaviour (in-memory only) is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from academic_intelligence import AcademicIntelligence
from academic_intelligence.core.types import AntiCrawlStrategy, Config
from academic_intelligence.sources.arxiv import ArxivSource

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <updated>2023-08-02T00:00:00Z</updated>
    <published>2017-06-12T00:00:00Z</published>
    <title>Attention Is All You Need</title>
    <summary>Sequence transduction models are based on attention.</summary>
    <author><name>Ashish Vaswani</name></author>
    <link href="http://arxiv.org/abs/1706.03762v7" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/1706.03762v7" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
  </entry>
</feed>
"""

_EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>
"""


def _mock_response(*, status_code: int = 200, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {}
    resp.json = MagicMock(side_effect=ValueError("no json"))
    return resp


def _fake_source() -> tuple[ArxivSource, MagicMock]:
    http = MagicMock()
    http.get = AsyncMock(return_value=_mock_response(text=_ARXIV_ATOM))
    return ArxivSource(http_client=http, min_interval_seconds=0.01), http


# ---------------------------------------------------------------------------
# F1 (Y-1): arXiv-native DOI resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arxiv_doi_routes_to_id_list_lookup() -> None:
    """``10.48550/arXiv.<id>`` must resolve via the ``id_list`` lookup
    instead of the ``doi:"..."`` field search (Y-1)."""
    source, http = _fake_source()
    paper = await source.get_paper_by_doi("10.48550/arXiv.1706.03762")
    assert paper is not None
    assert paper.title == "Attention Is All You Need"
    params = http.get.await_args.kwargs["params"]
    assert params.get("id_list") == "1706.03762"
    assert "search_query" not in params


@pytest.mark.asyncio
async def test_arxiv_doi_prefix_case_insensitive() -> None:
    """The ``10.48550/arXiv.`` prefix check is case-insensitive."""
    source, http = _fake_source()
    paper = await source.get_paper_by_doi("10.48550/ARXIV.1706.03762")
    assert paper is not None
    assert paper.title == "Attention Is All You Need"
    params = http.get.await_args.kwargs["params"]
    assert params.get("id_list") == "1706.03762"


@pytest.mark.asyncio
async def test_arxiv_doi_versioned_id_preserved() -> None:
    """A versioned arXiv DOI keeps its version in the ``id_list`` lookup."""
    source, http = _fake_source()
    paper = await source.get_paper_by_doi("10.48550/arXiv.1706.03762v7")
    assert paper is not None
    params = http.get.await_args.kwargs["params"]
    assert params.get("id_list") == "1706.03762v7"


@pytest.mark.asyncio
async def test_arxiv_doi_returns_paper_when_field_search_would_miss() -> None:
    """Reproduction for Y-1: the arXiv API's ``doi:"..."`` search returns no
    entries for arXiv-native DOIs; the paper must still resolve through the
    ``id_list`` path."""
    async def _get(url: str, **kwargs: Any) -> MagicMock:
        params = kwargs.get("params") or {}
        if "search_query" in params:
            return _mock_response(text=_EMPTY_FEED)
        return _mock_response(text=_ARXIV_ATOM)

    http = MagicMock()
    http.get = AsyncMock(side_effect=_get)
    source = ArxivSource(http_client=http, min_interval_seconds=0.01)

    paper = await source.get_paper_by_doi("10.48550/arXiv.1706.03762")
    # The field search returns an empty feed for arXiv-native DOIs, so the
    # only way this paper resolves is through the id_list route.
    assert paper is not None
    assert paper.title == "Attention Is All You Need"


@pytest.mark.asyncio
async def test_arxiv_non_arxiv_doi_keeps_field_search() -> None:
    """A non-arXiv DOI must keep the original ``doi:"..."`` field search."""
    source, http = _fake_source()
    paper = await source.get_paper_by_doi("10.1038/s41586-020-0001-1")
    params = http.get.await_args.kwargs["params"]
    assert params.get("search_query") == 'doi:"10.1038/s41586-020-0001-1"'
    # The mock feed has no matching DOI, so the loose papers[0] fallback is
    # exercised — behavior unchanged from before the fix.
    assert paper is not None


@pytest.mark.asyncio
async def test_arxiv_bare_id_path_unchanged() -> None:
    """``get_paper_by_arxiv_id`` on a bare arXiv ID is untouched."""
    source, http = _fake_source()
    paper = await source.get_paper_by_arxiv_id("1706.03762")
    assert paper is not None
    assert paper.id == "1706.03762v7"
    params = http.get.await_args.kwargs["params"]
    assert params.get("id_list") == "1706.03762"


@pytest.mark.asyncio
async def test_arxiv_doi_empty_input_returns_none() -> None:
    source, _http = _fake_source()
    assert await source.get_paper_by_doi("   ") is None


# ---------------------------------------------------------------------------
# F2 (Y-2): cache persistence wiring
# ---------------------------------------------------------------------------


def test_config_cache_persistence_fields_default() -> None:
    """Default config keeps the current behaviour: no disk persistence."""
    cfg = Config()
    assert cfg.cache_persistent is False
    assert cfg.cache_path is None


def test_config_cache_persistence_fields_roundtrip() -> None:
    cfg = Config(cache_persistent=True, cache_path="/tmp/ai_cache.json")
    restored = Config.from_dict(cfg.to_dict())
    assert restored.cache_persistent is True
    assert restored.cache_path == "/tmp/ai_cache.json"


@pytest.mark.asyncio
async def test_connect_cache_not_persistent_by_default(tmp_path: Path) -> None:
    """connect() keeps handing in an in-memory Cache unless configured."""
    ai = AcademicIntelligence(
        Config(cache_enabled=True, storage_path=str(tmp_path / "s.db"))
    )
    await ai.connect()
    try:
        assert ai._http is not None
        assert ai._http._cache is not None
        assert ai._http._cache.persistent is False
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_connect_cache_persistent_when_enabled(tmp_path: Path) -> None:
    """cache_persistent=True + cache_path are threaded into the Cache."""
    cache_file = tmp_path / "cache.json"
    ai = AcademicIntelligence(
        Config(
            cache_enabled=True,
            cache_persistent=True,
            cache_path=str(cache_file),
            storage_path=str(tmp_path / "s.db"),
        )
    )
    await ai.connect()
    try:
        assert ai._http is not None
        assert ai._http._cache is not None
        assert ai._http._cache.persistent is True
        assert Path(ai._http._cache.persist_path) == cache_file
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_persistent_cache_hits_disk_across_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With persistence enabled, a repeated query in a brand-new
    connect() is served from the on-disk cache (net=0)."""
    cache_file = tmp_path / "cache.json"
    store_path = tmp_path / "store.json"
    calls = {"net": 0}

    def _config() -> Config:
        return Config(
            sources=["arxiv"],
            cache_enabled=True,
            cache_persistent=True,
            cache_path=str(cache_file),
            storage_type="json",
            storage_path=str(store_path),
            anti_crawl=AntiCrawlStrategy(
                base_delay=0.0,
                adaptive_delay=False,
                jitter=False,
                max_retries=0,
            ),
            timeout=10.0,
        )

    async def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["net"] += 1
        return httpx.Response(200, text=_ARXIV_ATOM, request=httpx.Request(method, url))

    # Session 1: cold cache -> one network request, persisted to disk.
    ai1 = AcademicIntelligence(_config())
    await ai1.connect()
    assert ai1._http is not None and ai1._http._client is not None
    monkeypatch.setattr(ai1._http._client, "request", _request)
    try:
        result = await ai1.collect_paper("transformer", sources=["arxiv"])
        assert len(result.papers) > 0
        assert calls["net"] == 1
        assert cache_file.exists()
    finally:
        await ai1.close()

    # Session 2: new instance, same cache file -> disk hit, no network.
    ai2 = AcademicIntelligence(_config())
    await ai2.connect()
    assert ai2._http is not None and ai2._http._client is not None
    monkeypatch.setattr(ai2._http._client, "request", _request)
    try:
        result2 = await ai2.collect_paper("transformer", sources=["arxiv"])
        assert len(result2.papers) > 0
        assert calls["net"] == 1  # unchanged: served from disk cache
    finally:
        await ai2.close()
