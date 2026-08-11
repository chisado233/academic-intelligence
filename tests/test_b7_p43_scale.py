"""B7-P43: dedup bucketing (V1) and author-name query index (V2) at scale.

Covers:
- V1: the bucketed ``deduplicate_papers`` path is partition-identical to the
  original O(n^2) loop on randomized mixed-ID inputs, keeps every merge /
  guard (exact ID, arXiv<->DOI cross-ID, empty-class title merge, different-ID
  and venue-type guards), and the ≥1024-record dispatch makes the 10k
  same-DOI case fast.
- V2: the materialized author-name index (``paper_author_tokens`` + FTS5
  trigram table) keeps ``query_papers(author=...)`` semantics identical to the
  JSON backend (CJK / Cyrillic / middle-initial / substring / wildcards),
  auto-builds on old databases, and answers on a 100k library in ms.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from pathlib import Path

import pytest

from academic_intelligence.core.models import AuthorRef, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.deduplicator import (
    BUCKET_DEDUP_THRESHOLD,
    Deduplicator,
)
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(source: SourceType = SourceType.OPENALEX, conf: float = 0.8) -> Evidence:
    return Evidence(source=source, source_url="https://e.com", confidence=conf)


def _paper(id_: str | None, title: str, **kw: object) -> Paper:
    defaults: dict[str, object] = {"year": 2020, "evidence": _ev()}
    defaults.update(kw)
    return Paper(id=id_, title=title, **defaults)  # type: ignore[arg-type]


def _paper_with_authors(id_: str, title: str, authors: list[str], **kw: object) -> Paper:
    kw.setdefault("evidence", _ev())
    return Paper(
        id=id_,
        title=title,
        authors=[
            AuthorRef(name=name, position=i + 1) for i, name in enumerate(authors)
        ],
        **kw,
    )


def _record_signature(result) -> list[tuple]:
    return sorted(
        (
            r.id,
            r.title,
            r.doi,
            r.arxiv_id,
            r.pmid,
            r.venue_type,
            r.year,
            len(r.evidence_list),
        )
        for r in result
    )


# ---------------------------------------------------------------------------
# V1: bucketed dedup
# ---------------------------------------------------------------------------


_TITLE_POOL = [
    "Attention Is All You Need",
    "Deep Learning for Image Recognition",
    "A Survey of Neural Network Architectures",
    "Transformer Models in NLP",
    "Graph Neural Networks Review",
    "Reinforcement Learning from Human Feedback",
    "Medical Image Analysis with Deep Learning",
    "Quantum Computing and Machine Learning",
    "Federated Learning for Privacy-Preserving AI",
    "Diffusion Models for Generative Tasks",
    "The Mathematics of Deep Learning",
    "Sparse Attention Mechanisms",
    "Adversarial Robustness of Neural Networks",
    "Neural Architecture Search",
    "Knowledge Distillation Techniques",
    "Self-Supervised Representation Learning",
    "Time Series Forecasting with LSTMs",
    "Explainable Artificial Intelligence",
    "A",
]


def _gen_mixed(rng: random.Random, n: int) -> list[Paper]:
    papers: list[Paper] = []
    for i in range(n):
        kw: dict[str, object] = {}
        r = rng.random()
        title = rng.choice(_TITLE_POOL)
        kw["year"] = rng.choice([None, 2015, 2016, 2017, 2018, 2019, 2020])
        kw["venue_type"] = rng.choice([None, "journal", "book", "conference"])
        kw["authors"] = [f"Author {rng.randint(1, 8)}"]
        if r < 0.30:
            kw["doi"] = f"10.1000/{rng.randint(1, 200)}.{rng.randint(1, 9)}"
        if 0.20 < r < 0.55:
            kw["arxiv_id"] = rng.choice(
                [
                    f"{rng.randint(19, 23):02d}{rng.randint(1, 12):02d}.{rng.randint(1, 99999):05d}",
                    f"arxiv:{rng.randint(19, 23):02d}{rng.randint(1, 12):02d}.{rng.randint(1, 99999):05d}",
                ]
            )
        if 0.45 < r < 0.70:
            kw["pmid"] = f"{rng.randint(10000000, 39999999)}"
        if 0.65 < r < 0.80:
            kw["url"] = f"https://example.com/paper/{rng.randint(1, 300)}"
        papers.append(_paper(f"p{i}", title, **kw))
    return papers


def test_bucketed_dedup_partition_identical_to_original() -> None:
    """V1: bucketed and original O(n^2) dedup produce the identical cluster
    partition (by record identity signature) on randomized mixed-ID inputs —
    including papers exercising every conflict class and the arXiv<->DOI
    cross-ID overlap cases."""
    for n in (10, 100, 200):
        for seed in range(3):
            papers = _gen_mixed(random.Random(seed), n)
            original = Deduplicator().deduplicate_papers(papers)
            bucketed = Deduplicator().deduplicate_papers_bucketed(papers)
            assert _record_signature(original) == _record_signature(bucketed), (
                f"partition differs at n={n} seed={seed}"
            )


def test_bucketed_dedup_guards_and_cross_id_quirks() -> None:
    """V1: every merge rule and guard survives the bucketed path — same-DOI
    merge, arXiv<->DOI cross-ID merge (including the case where the DOI record
    ALSO carries a conflicting arXiv ID, which the original merges because the
    cross-ID rule runs before the id-conflict guard), no-ID title merge,
    different-DOI guard, book-vs-periodical guard, and arXiv-prefix
    normalization."""
    papers = [
        _paper("a1", "Same DOI Work", year=2020, doi="10.1000/c1.1"),
        _paper("a2", "Same DOI Work", year=2020, doi="10.1000/c1.1"),
        _paper("b1", "Cross ID Work", year=2019, arxiv_id="1901.00001"),
        _paper("b2", "Cross ID Work", year=2019, doi="10.1000/c2.2"),
        _paper("c1", "Med Image Anal of Brains", year=2018),
        _paper("c2", "Med Image Anal of Brains", year=2018),
        _paper("e1", "Conflicting DOI Work", year=2016, doi="10.1000/e.1"),
        _paper("e2", "Conflicting DOI Work", year=2016, doi="10.1000/e.2"),
        _paper("f1", "Lonely Work", year=2015),
        _paper("g1", "Handbook of X", year=2014, venue_type="book"),
        _paper("g2", "Handbook of X", year=2014, venue_type="journal_article"),
        _paper("h1", "Prefixed Arxiv", year=2013, arxiv_id="2301.00001"),
        _paper("h2", "Prefixed Arxiv", year=2013, arxiv_id="arxiv:2301.00001"),
        # cross-ID fires before the id-conflict guard -> merges despite the
        # conflicting arXiv IDs; the bucketed path must reproduce this.
        _paper("x1", "Overlap CrossID Case", year=2012, arxiv_id="2201.00001"),
        _paper(
            "x2",
            "Overlap CrossID Case",
            year=2012,
            arxiv_id="2201.99999",
            doi="10.1000/x.9",
        ),
        # same class {arxiv, doi}: every pair is cross-ID potential -> merges.
        _paper(
            "y1",
            "Same Class Both IDs Case",
            year=2011,
            arxiv_id="2101.00001",
            doi="10.1000/y.1",
        ),
        _paper(
            "y2",
            "Same Class Both IDs Case",
            year=2011,
            arxiv_id="2101.00002",
            doi="10.1000/y.2",
        ),
    ]
    original = Deduplicator().deduplicate_papers(papers)
    bucketed = Deduplicator().deduplicate_papers_bucketed(papers)
    assert _record_signature(original) == _record_signature(bucketed)
    survivors = {r.id for r in original}
    expected = {"a1", "b1", "c1", "e1", "e2", "f1", "g1", "g2", "h1", "x1", "y1"}
    assert survivors == expected, f"unexpected survivors: {survivors ^ expected}"


@pytest.mark.performance
@pytest.mark.slow
def test_dedup_dispatch_10k_same_doi_is_fast_and_correct() -> None:
    """V1: ``deduplicate_papers`` dispatches 10k same-DOI records to the
    bucketed path (n >= threshold) — 200 clusters instead of 71-93s of O(n^2)
    comparisons."""
    assert BUCKET_DEDUP_THRESHOLD <= 10_000
    papers = [
        _paper(
            None,
            f"Group {g} Unique Title",
            year=2000 + (g % 20),
            doi=f"10.1000/group{g}",
            evidence=_ev(SourceType.OPENALEX if i % 2 else SourceType.SEMANTIC_SCHOLAR),
        )
        for g in range(200)
        for i in range(50)
    ]
    dedup = Deduplicator()
    start = time.perf_counter()
    result = dedup.deduplicate_papers(papers)
    elapsed = time.perf_counter() - start
    stats = dedup.get_stats()
    assert len(result) == 200
    assert stats["merged"] == 9800
    assert stats["clusters"] == 200
    # ~750x faster than the 71-93s O(n^2) baseline; leave CI headroom.
    assert elapsed < 5.0, f"10k same-DOI dedup took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# V2: author-name query index
# ---------------------------------------------------------------------------


def _paperset() -> list[Paper]:
    return [
        _paper_with_authors("p1", "Deep Learning", ["Geoffrey E. Hinton"]),
        _paper_with_authors("cn-1", "深度学习研究", ["张三·Zhang San"]),
        _paper_with_authors("cn-2", "自然语言处理", ["田中太郎"]),
        _paper_with_authors("ru-1", "Neural Nets", ["Иван Петров"]),
        _paper_with_authors("ru-2", "Optimization", ["Анна Иванова"]),
        _paper_with_authors("w1", "Underscore", ["a_b"]),
        _paper_with_authors("w2", "Expanded", ["acb"]),
        _paper_with_authors("w3", "Percent", ["100% Club"]),
        _paper_with_authors("multi1", "Multi Auth", ["Bob Alice", "Bob Carol"]),
        _paper_with_authors("multi2", "Other Bob", ["Bob"]),
        _paper_with_authors("jose1", "Jose Paper", ["José Silva"]),
        _paper_with_authors("empty1", "No Authors", []),
    ]


@pytest.mark.asyncio
async def test_author_index_semantics_backends_consistent(tmp_path: Path) -> None:
    """V2: author queries on the indexed sqlite backend agree with the JSON
    backend on CJK / Cyrillic / middle-initial / substring / wildcard /
    no-match cases, and keep insertion-order pagination."""
    papers = _paperset()
    sqlite_store = SQLiteStorage(str(tmp_path / "ai.db"))
    json_store = JSONStorage(str(tmp_path / "ai"))
    await sqlite_store.connect()
    await json_store.connect()
    try:
        await sqlite_store.save_batch(papers=papers)
        await json_store.save_batch(papers=papers)

        async def ids(store: SQLiteStorage | JSONStorage, author: str, **kw: object):
            return [p.id for p in await store.query_papers(author=author, **kw)]

        cases = [
            ("Geoffrey Hinton", ["p1"]),
            ("Hinton", ["p1"]),
            ("张三", ["cn-1"]),
            ("Zhang", ["cn-1"]),
            ("张三·Zhang San", ["cn-1"]),
            ("田中", ["cn-2"]),
            ("Иван", ["ru-1", "ru-2"]),
            ("Иванова", ["ru-2"]),
            ("Ив", ["ru-1", "ru-2"]),
            ("José", ["jose1"]),
            ("jose", []),
            ("a_b", ["w1"]),
            ("acb", ["w2"]),
            ("100%", ["w3"]),
            ("Bob", ["multi1", "multi2"]),
            ("Bob Alice", ["multi1"]),
            ("李四", []),
            ("Ada Lovelace", []),
        ]
        for query, expected in cases:
            s = await ids(sqlite_store, query)
            j = await ids(json_store, query)
            assert s == j == expected, f"author={query!r}: sqlite={s} json={j}"

        # pagination applies after the exact match, in insertion order
        assert await ids(sqlite_store, "Bob", limit=1, offset=0) == ["multi1"]
        assert await ids(sqlite_store, "Bob", limit=1, offset=1) == ["multi2"]
        # combined filters still compose
        assert await ids(sqlite_store, "Bob", year=2020) == await ids(
            json_store, "Bob", year=2020
        )
        # decomposed (NFD) spelling hits the composed (NFC) stored name
        import unicodedata

        nfd = unicodedata.normalize("NFD", "José")
        assert await ids(sqlite_store, nfd) == ["jose1"]
    finally:
        await sqlite_store.close()
        await json_store.close()


@pytest.mark.asyncio
async def test_author_index_old_db_migration(tmp_path: Path) -> None:
    """V2: a pre-index database (old papers schema, no index tables) gets the
    index auto-built on connect and answers author queries; subsequent
    update/delete keep it in sync."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE papers ("
        "id VARCHAR(64) PRIMARY KEY, title TEXT NOT NULL, authors JSON, "
        "year INTEGER, venue TEXT, venue_type VARCHAR(32), abstract TEXT, "
        "doi VARCHAR(255), arxiv_id VARCHAR(255), pmid VARCHAR(64), url TEXT, "
        "pdf_url TEXT, citations INTEGER, reference_count INTEGER, "
        "keywords JSON, fields_of_study JSON, "
        '"references" JSON, citations_list JSON, evidence JSON NOT NULL)'
    )
    for pid, title, names in [
        ("m1", "Old DB Paper", ["Geoffrey E. Hinton"]),
        ("m2", "老库论文", ["张三·Zhang San"]),
        ("m3", "Cyrillic Old", ["Иван Петров"]),
    ]:
        conn.execute(
            "INSERT INTO papers (id, title, authors, evidence) VALUES (?,?,?,?)",
            (
                pid,
                title,
                json.dumps(
                    [{"name": n, "position": i + 1} for i, n in enumerate(names)]
                ),
                json.dumps(
                    [
                        {
                            "source": "openalex",
                            "source_url": "https://e.com",
                            "confidence": 0.9,
                        }
                    ]
                ),
            ),
        )
    conn.commit()
    conn.close()

    store = SQLiteStorage(str(db_path))
    await store.connect()
    try:
        for query, expected in [
            ("Hinton", ["m1"]),
            ("Geoffrey Hinton", ["m1"]),
            ("张三", ["m2"]),
            ("Ив", ["m3"]),
        ]:
            got = [p.id for p in await store.query_papers(author=query)]
            assert got == expected, f"migrated DB author={query!r}: {got}"
        # update/delete keep the index in sync after migration
        await store.update_paper(
            "m1", _paper_with_authors("m1", "Old DB Paper", ["Hinton Geoffrey"])
        )
        assert [p.id for p in await store.query_papers(author="Hinton")] == ["m1"]
        await store.delete_paper("m2")
        assert await store.query_papers(author="张三") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_author_index_boundary_cases(tmp_path: Path) -> None:
    """V2: empty library, no-author papers, and deleted papers never surface
    through author queries."""
    store = SQLiteStorage(str(tmp_path / "b.db"))
    await store.connect()
    try:
        assert await store.query_papers(author="Hinton") == []
        await store.save_batch(
            papers=[
                _paper_with_authors("n1", "No Authors", []),
                _paper_with_authors("y1", "With Author", ["Hinton"]),
            ]
        )
        assert [p.id for p in await store.query_papers(author="Hinton")] == ["y1"]
        await store.delete_paper("y1")
        assert await store.query_papers(author="Hinton") == []
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.performance
@pytest.mark.slow
async def test_author_index_100k_latency(tmp_path: Path) -> None:
    """V2 (performance): on a 100k-paper library the indexed author filter is
    sub-300ms (the pre-index FIX-G F1 raw-JSON scan measured 718-829ms at the
    same size in P39)."""
    store = SQLiteStorage(str(tmp_path / "perf.db"))
    await store.connect()
    try:
        pool = [f"Author {i} Surname{i % 97}" for i in range(2000)] + [
            "张三",
            "田中太郎",
            "Иван Петров",
            "Geoffrey E. Hinton",
        ]
        papers = [
            _paper_with_authors(
                f"perf-{i:06d}",
                f"Perf Paper {i % 5000}",
                [pool[(i + k * 997) % len(pool)] for k in range(1 + i % 3)],
                year=1990 + (i % 30),
                evidence=_ev(
                    SourceType.OPENALEX if i % 2 else SourceType.SEMANTIC_SCHOLAR,
                    0.8,
                ),
            )
            for i in range(100_000)
        ]
        await store.save_batch(papers=papers)
        times: list[float] = []
        for _ in range(3):
            start = time.perf_counter()
            await store.query_papers(author="Geoffrey E. Hinton", limit=10)
            times.append((time.perf_counter() - start) * 1000)
        assert min(times) < 300, f"author filter too slow: {times}"
    finally:
        await store.close()
