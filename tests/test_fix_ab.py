"""FIX-AB: parse-throughput (AB-3) and query-path (AB-4) optimizations.

Behavior-pinning tests for the optimized paths:

- ``core.models.normalize_doi`` / ``normalize_pmid``: the lightweight
  field-level validators the source parsers use instead of validating a
  whole ``Paper`` per DOI / PMID (AB-3) — the ``Paper`` model validators
  delegate to them, so the model-level behavior is unchanged.
- pubmed / arxiv parsing still softens malformed DOIs / PMIDs (the previous
  full-``Paper.model_validate`` guard had the same effect).
- SQLite: ``get_paper`` keeps the legacy ``papers.evidence`` column fallback
  when the evidence table has no rows for the paper (single-trip read path,
  AB-4), and the keyword query is served through the FTS5 paper-text index
  with the exact same match semantics as the LIKE path it replaces.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from academic_intelligence.core.models import (
    Paper,
    normalize_doi,
    normalize_pmid,
)
from academic_intelligence.sources.arxiv import ArxivSource
from academic_intelligence.sources.pubmed import PubMedSource
from academic_intelligence.storage.sqlite_store import SQLiteStorage

# ---------------------------------------------------------------------------
# AB-3: lightweight DOI / PMID normalization helpers
# ---------------------------------------------------------------------------


def test_normalize_doi_keeps_bare_doi() -> None:
    assert normalize_doi("10.1000/syn.1") == "10.1000/syn.1"


@pytest.mark.parametrize(
    "raw",
    [
        "https://doi.org/10.48550/arXiv.1706.03762",
        "http://doi.org/10.48550/arXiv.1706.03762",
        "doi:10.48550/arXiv.1706.03762",
        "DOI:10.48550/arXiv.1706.03762",
    ],
)
def test_normalize_doi_strips_prefixes(raw: str) -> None:
    assert normalize_doi(raw) == "10.48550/arXiv.1706.03762"


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "not-a-doi", "https://example.com/10.1000/x"],
)
def test_normalize_doi_rejects_invalid(raw: str | None) -> None:
    assert normalize_doi(raw) is None


def test_normalize_doi_roundtrips_with_model() -> None:
    """The Paper model validator must agree with the helper on every input:
    valid values normalize identically; invalid values fail the model
    (ValidationError) and return ``None`` from the parser-side helper."""
    for raw in [
        "10.1000/syn.1",
        "https://doi.org/10.48550/arXiv.1706.03762",
        None,
        "not-a-doi",
        "",
    ]:
        if raw is None or raw == "":
            assert Paper(title="x", doi=raw, evidence_list=[]).doi == normalize_doi(raw)
        elif normalize_doi(raw) is None:
            with pytest.raises(ValidationError):
                Paper(title="x", doi=raw, evidence_list=[])
        else:
            assert Paper(title="x", doi=raw, evidence_list=[]).doi == normalize_doi(raw)


def test_normalize_pmid_keeps_valid() -> None:
    assert normalize_pmid("12345678") == "12345678"
    assert normalize_pmid(" 12345678 ") == "12345678"


@pytest.mark.parametrize(
    "raw",
    [None, "", "abc", "123456789", "1234567a", "0x1F"],
)
def test_normalize_pmid_rejects_invalid(raw: str | None) -> None:
    assert normalize_pmid(raw) is None


def test_normalize_pmid_roundtrips_with_model() -> None:
    for raw in ["12345678", "123", None, "abcdef", "123456789"]:
        try:
            model_value = Paper(title="x", pmid=raw).pmid
        except Exception:
            model_value = None
        assert model_value == normalize_pmid(raw)


# ---------------------------------------------------------------------------
# AB-3: parsing still softens malformed DOIs / PMIDs
# ---------------------------------------------------------------------------

_SAMPLE_PUBMED_EFETCH = """<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">12345678</PMID>
      <Article>
        <Journal>
          <Title>Nature</Title>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Deep learning for medical imaging</ArticleTitle>
        <Abstract><AbstractText>This paper reviews deep learning applications.</AbstractText></Abstract>
        <AuthorList>
          <Author><LastName>Smith</LastName><ForeName>Jane</ForeName></Author>
        </AuthorList>
        <ELocationID EIdType="doi">https://doi.org/10.1038/s41586-020-0001-1</ELocationID>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">12345678</ArticleId>
        <ArticleId IdType="doi">https://doi.org/10.1038/s41586-020-0001-1</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">not-a-pmid</PMID>
      <Article>
        <Journal><Title>Lancet</Title></Journal>
        <ArticleTitle>Malformed DOI paper</ArticleTitle>
        <ELocationID EIdType="doi">not-a-doi</ELocationID>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


def test_pubmed_parse_normalizes_and_softens_doi_pmid() -> None:
    papers = PubMedSource()._parse_efetch_xml(_SAMPLE_PUBMED_EFETCH)
    assert len(papers) == 2
    assert papers[0].doi == "10.1038/s41586-020-0001-1"
    assert papers[0].pmid == "12345678"
    # malformed PMID -> no id, malformed DOI -> None (article still parses)
    assert papers[1].pmid is None
    assert papers[1].doi is None


_SAMPLE_ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T00:00:00Z</published>
    <title>Attention Is All You Need</title>
    <summary>The dominant sequence transduction models are based on complex recurrent networks.</summary>
    <author><name>Ashish Vaswani</name></author>
    <arxiv:doi>https://doi.org/10.48550/arXiv.1706.03762</arxiv:doi>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/1810.04805v2</id>
    <published>2018-10-11T00:00:00Z</published>
    <title>BERT Paper</title>
    <summary>We introduce a new language representation model.</summary>
    <arxiv:doi>not-a-doi</arxiv:doi>
  </entry>
</feed>
"""


def test_arxiv_parse_normalizes_and_softens_doi() -> None:
    papers = ArxivSource()._parse_feed(_SAMPLE_ARXIV_FEED)
    assert len(papers) == 2
    assert papers[0].doi == "10.48550/arXiv.1706.03762"
    assert papers[1].doi is None


# ---------------------------------------------------------------------------
# AB-4: SQLite get_paper legacy evidence-column fallback (single-trip read)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paper_falls_back_to_legacy_evidence_column(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """A paper written through the legacy single-evidence path (evidence JSON
    column only, no evidence rows) must still load its evidence after the
    single-trip read-path change."""
    from academic_intelligence.core.types import SourceType

    store = SQLiteStorage(str(tmp_path / "legacy.db"))
    await store.connect()
    try:
        # Insert a paper whose evidence lives ONLY in the legacy JSON column
        # (bypass the ORM evidence-row path, as old databases did).
        from sqlalchemy import text

        async with store._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO papers (id, title, authors, keywords, "
                    "fields_of_study, evidence) VALUES "
                    "(:id, :title, :authors, :keywords, :fos, :evidence)"
                ),
                {
                    "id": "legacy-1",
                    "title": "Legacy Evidence Paper",
                    "authors": "[]",
                    "keywords": "[]",
                    "fos": "[]",
                    "evidence": (
                        '[{"source": "openalex", "source_url": "https://e.com", '
                        '"confidence": 0.9, "collected_at": '
                        '"2025-01-01T00:00:00Z"}]'
                    ),
                },
            )
        paper = await store.get_paper("legacy-1")
        assert paper is not None
        assert paper.title == "Legacy Evidence Paper"
        assert len(paper.evidence_list) == 1
        assert paper.evidence_list[0].source == SourceType.OPENALEX
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# AB-4: keyword query through the FTS5 paper-text index
# ---------------------------------------------------------------------------


def _kw_paper(pid: str, title: str, abstract: str | None = None) -> Paper:
    from academic_intelligence.core.models import AuthorRef, Evidence
    from academic_intelligence.core.types import SourceType

    return Paper(
        id=pid,
        title=title,
        authors=[AuthorRef(name="A. Author", position=1)],
        abstract=abstract,
        evidence=Evidence(source=SourceType.OPENALEX, source_url="https://e.com"),
    )


@pytest.mark.asyncio
async def test_keyword_matches_title_and_abstract_via_fts(
    tmp_path: pytest.TempPathFactory,
) -> None:
    store = SQLiteStorage(str(tmp_path / "kw.db"))
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _kw_paper("k1", "Deep Learning For Vision", "A long abstract about transformers."),
                _kw_paper("k2", "Unrelated Title", "This abstract studies deep learning too."),
                _kw_paper("k3", "Nothing In Common", "Plain text."),
            ]
        )
        # title match (case-insensitive, as before)
        assert [p.id for p in await store.query_papers(keyword="vision")] == ["k1"]
        assert [p.id for p in await store.query_papers(keyword="VISION")] == ["k1"]
        # abstract match
        assert [p.id for p in await store.query_papers(keyword="transformers")] == ["k1"]
        assert [p.id for p in await store.query_papers(keyword="deep")] == ["k1", "k2"]
        # no match
        assert await store.query_papers(keyword="zyzzyva") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_keyword_fts_updated_on_single_save_and_update(
    tmp_path: pytest.TempPathFactory,
) -> None:
    store = SQLiteStorage(str(tmp_path / "kw2.db"))
    await store.connect()
    try:
        paper = _kw_paper("u1", "Original Title Here")
        await store.save_paper(paper)
        assert [p.id for p in await store.query_papers(keyword="original")] == ["u1"]

        updated = paper.model_copy(update={"title": "Completely New Heading"})
        await store.update_paper("u1", updated)
        assert await store.query_papers(keyword="original") == []
        assert [p.id for p in await store.query_papers(keyword="heading")] == ["u1"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_keyword_fts_dropped_on_delete(tmp_path: pytest.TempPathFactory) -> None:
    store = SQLiteStorage(str(tmp_path / "kw3.db"))
    await store.connect()
    try:
        await store.save_paper(_kw_paper("d1", "Delete Me Please"))
        assert [p.id for p in await store.query_papers(keyword="delete")] == ["d1"]
        await store.delete_paper("d1")
        assert await store.query_papers(keyword="delete") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_keyword_literal_wildcards_via_fts(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """I-1 semantics survive the FTS prefilter: ``100%`` matches a literal
    ``%`` only, and ``under_score`` a literal underscore."""
    store = SQLiteStorage(str(tmp_path / "kw4.db"))
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _kw_paper("p6", "100% Pure Machine Code"),
                _kw_paper("p7", "Under_score and dash-test"),
                _kw_paper("p10", "100x Speedup Report"),
            ]
        )
        assert [p.id for p in await store.query_papers(keyword="100%")] == ["p6"]
        assert [p.id for p in await store.query_papers(keyword="under_score")] == ["p7"]
        assert [p.id for p in await store.query_papers(keyword="dash-test")] == ["p7"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_keyword_fts_backfilled_on_connect(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """A database written by an older binary (papers exist, no FTS rows)
    gets the paper-text FTS table backfilled on connect, so keyword queries
    find the pre-existing papers."""
    db = tmp_path / "kw5.db"
    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        # Write papers bypassing the ORM index maintenance (old-binary shape).
        from sqlalchemy import text

        async with store._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO papers (id, title, authors, keywords, "
                    "fields_of_study) VALUES (:id, :title, '[]', '[]', '[]')"
                ),
                [
                    {"id": "old-1", "title": "Backfilled Legacy Title"},
                    {"id": "old-2", "title": "Another Old Paper"},
                ],
            )
    finally:
        await store.close()

    store2 = SQLiteStorage(str(db))
    await store2.connect()
    try:
        assert [p.id for p in await store2.query_papers(keyword="backfilled")] == [
            "old-1"
        ]
        assert [p.id for p in await store2.query_papers(keyword="legacy")] == ["old-1"]
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_keyword_dense_matches_still_correct(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """A keyword matching many rows still returns correct, insertion-ordered
    results through the FTS join (the degenerate high-selectivity case is
    served by the trigram walk + rowid sort)."""
    store = SQLiteStorage(str(tmp_path / "kw6.db"))
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _kw_paper(
                    f"cap-{i:05d}",
                    f"Common Term Paper {i}",
                    "every row carries the common term",
                )
                for i in range(300)
            ]
        )
        result = await store.query_papers(keyword="common", limit=10)
        assert len(result) == 10
        assert [p.id for p in result] == [f"cap-{i:05d}" for i in range(10)]
        # pagination through the join
        page2 = await store.query_papers(keyword="common", limit=5, offset=10)
        assert [p.id for p in page2] == [f"cap-{i:05d}" for i in range(10, 15)]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_keyword_fts_combined_with_year_and_venue(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """The FTS join re-applies year / venue conditions, so combined keyword
    queries keep AND semantics."""
    store = SQLiteStorage(str(tmp_path / "kw7.db"))
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                _kw_paper("c1", "Deep Learning Paper", "transformer abstract").model_copy(
                    update={"year": 2020}
                ),
                _kw_paper("c2", "Deep Learning Paper", "transformer abstract").model_copy(
                    update={"year": 2021}
                ),
                _kw_paper("c3", "Deep Learning Paper", "cnn abstract").model_copy(
                    update={"year": 2021}
                ),
            ]
        )
        # keyword + year
        assert [
            p.id for p in await store.query_papers(keyword="deep", year=2021)
        ] == ["c2", "c3"]
        # keyword + year_from/year_to
        assert [
            p.id
            for p in await store.query_papers(
                keyword="transformer", year_from=2020, year_to=2021
            )
        ] == ["c1", "c2"]
        # keyword + venue
        papers = await store.query_papers(keyword="deep", venue="nonexistent")
        assert papers == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_keyword_fts_combined_with_author(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """author + keyword keeps AND semantics (the author branch reuses the
    keyword-narrowed statement)."""
    from academic_intelligence.core.models import AuthorRef, Evidence
    from academic_intelligence.core.types import SourceType

    store = SQLiteStorage(str(tmp_path / "kw8.db"))
    await store.connect()
    try:
        await store.save_batch(
            papers=[
                Paper(
                    id="a1",
                    title="Deep Learning Paper",
                    authors=[AuthorRef(name="Alice Green", position=1)],
                    evidence_list=[
                        Evidence(source=SourceType.OPENALEX, source_url="https://e.com")
                    ],
                ),
                Paper(
                    id="a2",
                    title="Deep Learning Paper",
                    authors=[AuthorRef(name="Bob Blue", position=1)],
                    evidence_list=[
                        Evidence(source=SourceType.OPENALEX, source_url="https://e.com")
                    ],
                ),
            ]
        )
        assert [
            p.id for p in await store.query_papers(author="Alice", keyword="deep")
        ] == ["a1"]
        assert [
            p.id for p in await store.query_papers(author="Bob", keyword="paper")
        ] == ["a2"]
    finally:
        await store.close()
