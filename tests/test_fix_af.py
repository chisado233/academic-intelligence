"""FIX-AF (P51 round-33 privacy findings): AF-1 error-path key redaction,
AF-2 delete cascade, AF-3 proxy credential masking.

Covers:
- AF-1 (high): API keys passed as query params (SerpAPI ``api_key``, IEEE
  ``apikey``) must never surface in transport-error messages, retry logs or
  source errors.  The URL keeps its shape; only the secret value becomes
  ``***``.
- AF-2 (privacy gap): ``delete_paper`` / ``delete_author`` cascade-clean every
  row that references the deleted record (evidence, citations, authorships,
  coauthorships, hashes, entity_sync, FTS/author index).
- AF-3 (medium): proxy URLs carrying ``user:pass@`` credentials are masked in
  ``Config.to_dict()`` / ``model_dump()`` and in ``ProxyPool`` health logs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    Citation,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import (
    AntiCrawlStrategy,
    Config,
    SourceType,
    mask_proxy_userinfo,
)
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage
from academic_intelligence.utils.http import HTTPClient, redact_url_secrets
from academic_intelligence.utils.proxy import ProxyPool


def _ev() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W1",
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# AF-1: error-path key redaction
# ---------------------------------------------------------------------------


def test_redact_url_secrets_masks_sensitive_query_params() -> None:
    url = (
        "https://serpapi.com/search?engine=google_scholar&q=llm&num=10"
        "&api_key=sk-END2END-LEAK-7777&token=abc123&sig=xyz789"
    )
    redacted = redact_url_secrets(url)
    assert "sk-END2END-LEAK-7777" not in redacted
    assert "abc123" not in redacted
    assert "xyz789" not in redacted
    assert "api_key=***" in redacted
    assert "token=***" in redacted
    assert "sig=***" in redacted
    # The rest of the URL survives.
    assert redacted.startswith(
        "https://serpapi.com/search?engine=google_scholar&q=llm&num=10&"
    )


def test_redact_url_secrets_ieee_and_case_insensitive() -> None:
    message = (
        "IEEE Xplore request failed: ConnectError: "
        "https://ieeexploreapi.ieee.org/api/v1/search/articles"
        "?apikey=ieee-SECRET&querytext=llm"
    )
    redacted = redact_url_secrets(message)
    assert "ieee-SECRET" not in redacted
    assert "apikey=***" in redacted
    assert "querytext=llm" in redacted
    assert (
        redact_url_secrets("http://h/?API_KEY=Up&q=keep")
        == "http://h/?API_KEY=***&q=keep"
    )
    assert redact_url_secrets("http://h/path?q=no-secret") == "http://h/path?q=no-secret"


@pytest.mark.asyncio
async def test_transport_failure_message_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AF-1: a transport failure embedding ``?api_key=...`` must not leak it."""
    leak_url = (
        "https://serpapi.com/search?engine=google_scholar&q=llm&num=10"
        "&api_key=sk-END2END-LEAK-7777"
    )

    async def _fail(*args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError(f"All connection attempts failed for {leak_url}")

    strategy = AntiCrawlStrategy(max_retries=0, base_delay=0.0)
    client = HTTPClient(strategy=strategy, timeout=1.0, enable_cache=False)
    await client.connect()
    try:
        assert client._client is not None
        monkeypatch.setattr(client._client, "request", _fail)
        with pytest.raises(httpx.ConnectError) as excinfo:
            await client.get(leak_url)
        msg = str(excinfo.value)
        assert "sk-END2END-LEAK-7777" not in msg
        assert "api_key=***" in msg
        assert "engine=google_scholar" in msg
        assert "q=llm" in msg
        assert "num=10" in msg
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_retry_log_has_no_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AF-1: the RetryHandler warning must not contain the leaked key."""
    leak_url = (
        "https://serpapi.com/search?engine=google_scholar&q=llm&num=10"
        "&api_key=sk-RETRY-LEAK-4242"
    )

    async def _fail(*args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed: {leak_url}")

    strategy = AntiCrawlStrategy(
        max_retries=1, base_delay=0.0, retry_backoff=1.0, jitter=False
    )
    client = HTTPClient(strategy=strategy, timeout=1.0, enable_cache=False)
    await client.connect()
    try:
        assert client._client is not None
        monkeypatch.setattr(client._client, "request", _fail)
        with caplog.at_level(logging.WARNING, logger="academic_intelligence.utils.retry"):
            with pytest.raises(httpx.ConnectError):
                await client.get(leak_url)
    finally:
        await client.close()
    assert "sk-RETRY-LEAK-4242" not in caplog.text
    assert "api_key=***" in caplog.text


# ---------------------------------------------------------------------------
# AF-2: delete cascade
# ---------------------------------------------------------------------------


async def _sql_count(store: SQLiteStorage, sql: str, *params: Any) -> int:
    for i in range(len(params)):
        sql = sql.replace("?", f":p{i}", 1)
    binds = {f"p{i}": p for i, p in enumerate(params)}
    async with store._session() as session:
        result = await session.execute(text(sql), binds)
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_sqlite_delete_paper_cascades(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "cascade.db"))
    await store.connect()
    try:
        ev = _ev()
        aid1 = await store.save_author(Author(name="Alice", evidence=ev))
        aid2 = await store.save_author(Author(name="Bob", evidence=ev))
        pid = await store.save_paper(
            Paper(
                title="Cascade Paper",
                authors=[
                    AuthorRef(author_id=aid1, name="Alice", position=1),
                    AuthorRef(author_id=aid2, name="Bob", position=2),
                ],
                year=2021,
                evidence=ev,
            )
        )
        await store.save_citation(
            Citation(citing_paper_id=pid, cited_paper_id="other", evidence=ev)
        )
        await store.save_citation(
            Citation(citing_paper_id="other", cited_paper_id=pid, evidence=ev)
        )
        await store.save_paper_hash(pid, "h" * 16)
        await store.save_entity_sync(
            "paper", pid, "openalex", datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        # Seed sanity checks.
        assert len(await store.get_citations_by_paper(pid, direction="outgoing")) == 1
        assert len(await store.get_citations_by_paper(pid, direction="incoming")) == 1
        assert await store.get_paper_hash(pid) is not None
        assert await store.get_entity_sync("paper", pid, "openalex") is not None

        assert await store.delete_paper(pid) is True

        assert await store.get_paper(pid) is None
        assert await store.get_citations_by_paper(pid, direction="outgoing") == []
        assert await store.get_citations_by_paper(pid, direction="incoming") == []
        assert await store.get_paper_hash(pid) is None
        assert await store.get_entity_sync("paper", pid, "openalex") is None
        # The other citation endpoint must no longer resolve the edge.
        assert await store.get_references("other") == []
        assert await store.get_citations("other") == []
        # Authorship edges and the author-name/paper-text indexes are gone.
        assert pid not in await store.get_author_papers(aid1)
        assert pid not in await store.get_author_papers(aid2)
        assert await store.query_papers(author="Alice") == []
        assert await store.query_papers(keyword="cascade") == []
        # Raw tables hold no orphan rows.
        assert (
            await _sql_count(
                store,
                "SELECT COUNT(*) FROM evidence WHERE entity_type='paper' AND entity_id=?",
                pid,
            )
            == 0
        )
        assert (
            await _sql_count(
                store,
                "SELECT COUNT(*) FROM citations "
                "WHERE citing_paper_id=? OR cited_paper_id=?",
                pid,
                pid,
            )
            == 0
        )
        assert (
            await _sql_count(store, "SELECT COUNT(*) FROM authorships WHERE paper_id=?", pid)
            == 0
        )
        assert (
            await _sql_count(store, "SELECT COUNT(*) FROM paper_hashes WHERE paper_id=?", pid)
            == 0
        )
        assert (
            await _sql_count(
                store,
                "SELECT COUNT(*) FROM entity_sync "
                "WHERE entity_type='paper' AND entity_id=?",
                pid,
            )
            == 0
        )
        # The authors themselves are untouched.
        assert await store.get_author(aid1) is not None
        assert await store.get_author(aid2) is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_delete_author_cascades(tmp_path: Path) -> None:
    store = SQLiteStorage(str(tmp_path / "author-cascade.db"))
    await store.connect()
    try:
        ev = _ev()
        aid1 = await store.save_author(Author(name="Alice", evidence=ev))
        aid2 = await store.save_author(Author(name="Bob", evidence=ev))
        pid = await store.save_paper(
            Paper(
                title="Author Cascade",
                authors=[
                    AuthorRef(author_id=aid1, name="Alice", position=1),
                    AuthorRef(author_id=aid2, name="Bob", position=2),
                ],
                year=2021,
                evidence=ev,
            )
        )
        await store.save_entity_sync(
            "author", aid1, "semantic_scholar", datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        # The two-author paper seeded the (aid1, aid2) coauthorship pair.
        assert aid2 in await store.get_coauthors(aid1)

        assert await store.delete_author(aid1) is True

        assert await store.get_author(aid1) is None
        assert await store.get_author(aid2) is not None
        assert await store.get_entity_sync("author", aid1, "semantic_scholar") is None
        # The paper survives; only the author's edges are removed.
        assert await store.get_paper(pid) is not None
        assert await store.get_author_papers(aid1) == []
        assert aid1 not in await store.get_coauthors(aid2)
        assert await store.get_coauthors(aid1) == []
        # Raw tables hold no orphan rows for the deleted author.
        assert (
            await _sql_count(
                store,
                "SELECT COUNT(*) FROM evidence WHERE entity_type='author' AND entity_id=?",
                aid1,
            )
            == 0
        )
        assert (
            await _sql_count(
                store, "SELECT COUNT(*) FROM authorships WHERE author_id=?", aid1
            )
            == 0
        )
        assert (
            await _sql_count(
                store,
                "SELECT COUNT(*) FROM coauthorships "
                "WHERE author_a_id=? OR author_b_id=?",
                aid1,
                aid1,
            )
            == 0
        )
        assert (
            await _sql_count(
                store,
                "SELECT COUNT(*) FROM entity_sync "
                "WHERE entity_type='author' AND entity_id=?",
                aid1,
            )
            == 0
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_delete_paper_cascades(tmp_path: Path) -> None:
    store = JSONStorage(str(tmp_path / "jstore"))
    await store.connect()
    try:
        ev = _ev()
        aid1 = await store.save_author(Author(name="Alice", evidence=ev))
        aid2 = await store.save_author(Author(name="Bob", evidence=ev))
        pid = await store.save_paper(
            Paper(
                title="J Cascade",
                authors=[
                    AuthorRef(author_id=aid1, name="Alice", position=1),
                    AuthorRef(author_id=aid2, name="Bob", position=2),
                ],
                year=2021,
                evidence=ev,
            )
        )
        await store.save_citation(
            Citation(citing_paper_id=pid, cited_paper_id="other", evidence=ev)
        )
        await store.save_citation(
            Citation(citing_paper_id="other", cited_paper_id=pid, evidence=ev)
        )
        await store.save_paper_hash(pid, "h" * 16)
        await store.save_entity_sync(
            "paper", pid, "openalex", datetime(2026, 1, 1, tzinfo=timezone.utc)
        )

        assert await store.delete_paper(pid) is True

        assert await store.get_paper(pid) is None
        assert await store.get_paper_hash(pid) is None
        assert await store.get_entity_sync("paper", pid, "openalex") is None
        assert await store.get_citations_by_paper(pid, direction="outgoing") == []
        assert await store.get_citations_by_paper(pid, direction="incoming") == []
        assert await store.get_references("other") == []
        assert await store.get_citations("other") == []
        assert pid not in await store.get_author_papers(aid1)
        # In-memory stores hold no orphan rows.
        assert f"paper:{pid}" not in store._evidence
        assert pid not in store._authorships
        assert all(
            data.get("citing_paper_id") != pid and data.get("cited_paper_id") != pid
            for data in store._citations.values()
        )
        assert pid not in store._hashes
        assert not any(k.startswith(f"paper|{pid}|") for k in store._entity_sync)
        assert await store.get_author(aid1) is not None
        assert await store.get_author(aid2) is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_delete_author_cascades(tmp_path: Path) -> None:
    store = JSONStorage(str(tmp_path / "jstore2"))
    await store.connect()
    try:
        ev = _ev()
        aid1 = await store.save_author(Author(name="Alice", evidence=ev))
        aid2 = await store.save_author(Author(name="Bob", evidence=ev))
        pid = await store.save_paper(
            Paper(
                title="J Author Cascade",
                authors=[
                    AuthorRef(author_id=aid1, name="Alice", position=1),
                    AuthorRef(author_id=aid2, name="Bob", position=2),
                ],
                year=2021,
                evidence=ev,
            )
        )
        await store.save_entity_sync(
            "author", aid1, "semantic_scholar", datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        assert aid2 in await store.get_coauthors(aid1)

        assert await store.delete_author(aid1) is True

        assert await store.get_author(aid1) is None
        assert await store.get_author(aid2) is not None
        assert await store.get_entity_sync("author", aid1, "semantic_scholar") is None
        assert await store.get_paper(pid) is not None
        assert await store.get_author_papers(aid1) == []
        assert aid1 not in await store.get_coauthors(aid2)
        # In-memory stores hold no orphan rows for the deleted author.
        assert f"author:{aid1}" not in store._evidence
        assert all(
            ref.get("author_id") != aid1
            for refs in store._authorships.values()
            for ref in refs
        )
        assert not any(
            key.startswith(f"{aid1}|") or key.endswith(f"|{aid1}")
            for key in store._coauthorships
        )
        assert not any(k.startswith(f"author|{aid1}|") for k in store._entity_sync)
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# AF-3: proxy credential masking
# ---------------------------------------------------------------------------


def test_mask_proxy_userinfo() -> None:
    assert mask_proxy_userinfo("http://user:pass@host:8080") == "http://***@host:8080"
    assert mask_proxy_userinfo("http://host:8080") == "http://host:8080"
    assert mask_proxy_userinfo("socks5://u:p@h:1080") == "socks5://***@h:1080"
    assert mask_proxy_userinfo("") == ""


def test_config_serialization_masks_proxy_credentials() -> None:
    cfg = Config(
        proxy="http://user:secret@proxy.example:8080",
        proxies=["http://u2:p2@h2:8080"],
        anti_crawl=AntiCrawlStrategy(proxy_pool=["socks5://u3:p3@h3:1080"]),
    )
    dumped = cfg.to_dict()
    assert "user:secret" not in str(dumped)
    assert "u2:p2" not in str(dumped)
    assert "u3:p3" not in str(dumped)
    assert dumped["proxy"] == "http://***@proxy.example:8080"
    assert dumped["proxies"] == ["http://***@h2:8080"]
    assert dumped["anti_crawl"]["proxy_pool"] == ["socks5://***@h3:1080"]
    assert "user:secret" not in str(cfg.model_dump())
    # Live values (what adapters / HTTPClient actually use) are untouched.
    assert cfg.proxy == "http://user:secret@proxy.example:8080"
    assert cfg.anti_crawl.proxy_pool == ["socks5://u3:p3@h3:1080"]
    assert cfg.proxy_list() == [
        "http://user:secret@proxy.example:8080",
        "http://u2:p2@h2:8080",
        "socks5://u3:p3@h3:1080",
    ]


def test_proxy_pool_logs_hide_userinfo(caplog: pytest.LogCaptureFixture) -> None:
    pool = ProxyPool(["http://user:secret@h1:8080"])
    with caplog.at_level(logging.WARNING, logger="academic_intelligence.utils.proxy"):
        pool.mark_unhealthy("http://user:secret@h1:8080")
    assert "user:secret" not in caplog.text
    assert "h1:8080" in caplog.text


def test_proxy_pool_healthy_log_hides_userinfo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = ProxyPool([])
    pool.add("http://u:p@h2:8080")
    with caplog.at_level(logging.INFO, logger="academic_intelligence.utils.proxy"):
        pool.mark_unhealthy("http://u:p@h2:8080")
        pool.mark_healthy("http://u:p@h2:8080")
    assert "u:p" not in caplog.text
    assert "h2:8080" in caplog.text
