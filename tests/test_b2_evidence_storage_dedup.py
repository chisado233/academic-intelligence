"""B2 ticket tests: evidence multi-source, storage edge tables, dedup fusion
upgrade and confidence scorer (3A v2 design §6 / §8).

Covers:
- ``evidence`` -> ``evidence_list`` migration (Paper/Author), ``primary_evidence``
- Dedup: 3-source merge with mixed ID formats, arXiv↔DOI cross-ID, SequenceMatcher
- ConfidenceScorer: single-source baseline, multi-source bonus cap, DOI/PDF/stale
- Storage: authorships / coauthorships / evidence tables + graph queries on
  both the SQLite and JSON backends, plus legacy single-evidence column compat.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    Citation,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.processors.scorer import (
    DOI_EXACT_MATCH_BONUS,
    MULTI_SOURCE_BONUS,
    PDF_LINK_BONUS,
    SOURCE_BASELINE_CONFIDENCE,
    STALE_PENALTY,
    ConfidenceScorer,
)
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage


def _ev(
    source: SourceType = SourceType.OPENALEX,
    conf: float = 0.8,
    sid: str | None = None,
    collected_at: datetime | None = None,
) -> Evidence:
    return Evidence(
        source=source,
        source_url=f"https://{source.value}/record",
        confidence=conf,
        source_id=sid,
        collected_at=collected_at or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# evidence -> evidence_list migration
# ---------------------------------------------------------------------------


def test_paper_evidence_list_and_primary_evidence() -> None:
    low = _ev(SourceType.OPENALEX, 0.7, "doi-1")
    high = _ev(SourceType.ARXIV, 0.95, "arxiv-1")
    paper = Paper(title="T", evidence_list=[low, high])
    assert len(paper.evidence_list) == 2
    assert paper.primary_evidence is not None
    assert paper.primary_evidence.confidence == 0.95
    assert paper.primary_evidence.source == SourceType.ARXIV


def test_author_evidence_list_and_primary_evidence() -> None:
    low = _ev(SourceType.OPENALEX, 0.7, "a-1")
    high = _ev(SourceType.SEMANTIC_SCHOLAR, 0.9, "s2-1")
    author = Author(name="Ada", evidence_list=[low, high])
    assert len(author.evidence_list) == 2
    assert author.primary_evidence is not None
    assert author.primary_evidence.source == SourceType.SEMANTIC_SCHOLAR


def test_legacy_evidence_constructor_folds_into_evidence_list() -> None:
    ev = _ev()
    paper = Paper(title="T", evidence=ev)
    assert len(paper.evidence_list) == 1
    assert paper.evidence_list[0] == ev
    author = Author(name="A", evidence=ev)
    assert len(author.evidence_list) == 1


def test_evidence_list_roundtrip_via_dict() -> None:
    paper = Paper(
        title="T",
        evidence_list=[_ev(SourceType.OPENALEX, 0.7, "d1"), _ev(SourceType.ARXIV, 0.9, "a1")],
    )
    restored = Paper.from_dict(paper.to_dict())
    assert len(restored.evidence_list) == 2
    assert restored.primary_evidence is not None
    assert restored.primary_evidence.confidence == 0.9


def test_no_evidence_yields_none_primary() -> None:
    paper = Paper(title="T")
    assert paper.evidence_list == []
    assert paper.primary_evidence is None


def test_primary_evidence_picks_highest_confidence() -> None:
    paper = Paper(
        title="T",
        evidence_list=[
            _ev(SourceType.OPENALEX, 0.55, "x1"),
            _ev(SourceType.IEEE, 0.85, "ieee1"),
            _ev(SourceType.ARXIV, 0.80, "arx1"),
        ],
    )
    assert paper.primary_evidence is not None
    assert paper.primary_evidence.source == SourceType.IEEE


def test_primary_evidence_ignores_stale_legacy_value() -> None:
    """M1: with both ``evidence`` and ``evidence_list`` provided, a stale
    legacy ``evidence`` value must not shadow the list's highest confidence."""
    stale = _ev(SourceType.OPENALEX, 0.4, "oa-stale")
    best = _ev(SourceType.ARXIV, 0.95, "arx-best")
    paper = Paper(title="T", evidence=stale, evidence_list=[best])
    assert paper.primary_evidence is not None
    assert paper.primary_evidence.confidence == 0.95
    author = Author(name="A", evidence=stale, evidence_list=[best])
    assert author.primary_evidence is not None
    assert author.primary_evidence.confidence == 0.95


# ---------------------------------------------------------------------------
# Dedup fusion upgrade (3A v2 §6.1)
# ---------------------------------------------------------------------------


def test_dedup_three_sources_merge_into_one() -> None:
    """Same paper from 3 sources with different ID formats merges to 1."""
    title = "Attention Is All You Need"
    doi = "10.5555/3295222.3295349"
    arxiv_id = "1706.03762"
    doi_only = Paper(
        title=title,
        authors=["Vaswani"],
        year=2017,
        doi=doi,
        evidence_list=[_ev(SourceType.OPENALEX, 0.88, doi)],
    )
    arxiv_only = Paper(
        title=title,
        authors=["Ashish Vaswani"],
        year=2017,
        arxiv_id=arxiv_id,
        evidence_list=[_ev(SourceType.ARXIV, 0.92, arxiv_id)],
    )
    doi_arxiv = Paper(
        title=title,
        authors=["Vaswani"],
        year=2017,
        doi=doi,
        arxiv_id=arxiv_id,
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.90, "s2-paper")],
    )
    merged = Deduplicator().deduplicate_papers([doi_only, arxiv_only, doi_arxiv])
    assert len(merged) == 1
    m = merged[0]
    # all three sources' evidence preserved
    assert len(m.evidence_list) == 3
    sources = {e.source for e in m.evidence_list}
    assert sources == {
        SourceType.OPENALEX,
        SourceType.ARXIV,
        SourceType.SEMANTIC_SCHOLAR,
    }
    # I2: fusion scores through score_paper (multi-source bonus + field-level
    # adjustments). Expected value:
    #   base = max(0.90, 0.95, 0.88) = 0.95; n_sources = 3
    #   multi-source = min(1.0, 0.95 + 0.05 * (3 - 1)) = 1.0
    #   DOI exact match +0.05 -> capped at 1.0
    base = max(SOURCE_BASELINE_CONFIDENCE[s] for s in sources)
    expected = min(
        1.0,
        min(1.0, base + MULTI_SOURCE_BONUS * (len(sources) - 1))
        + DOI_EXACT_MATCH_BONUS,
    )
    assert m.primary_evidence is not None
    assert m.primary_evidence.confidence == pytest.approx(expected)
    # Explicit assertion that the field-adjusted scoring path produced the
    # value: re-scoring the merged record is a no-op (score_paper idempotent).
    rescored = ConfidenceScorer().score_paper(m)
    assert rescored.primary_evidence is not None
    assert rescored.primary_evidence.confidence == pytest.approx(
        m.primary_evidence.confidence
    )
    # field fusion keeps ids from the highest-confidence sources
    assert m.doi == doi
    assert m.arxiv_id == arxiv_id


def test_dedup_merge_applies_field_level_adjustments() -> None:
    """I2: dedup fusion goes through ``score_paper`` — DOI bonus observable.

    Uses a 2-source merge (IEEE + Google Scholar) without DOI so the
    multi-source bonus stays below the 1.0 cap and the field-level DOI
    adjustment can be verified exactly (formula shape, not just a capped
    number).
    """
    ieee = _ev(SourceType.IEEE, 0.85, "ieee-1")
    gs = _ev(SourceType.GOOGLE_SCHOLAR, 0.75, "gs-1")
    base = max(
        SOURCE_BASELINE_CONFIDENCE[SourceType.IEEE],
        SOURCE_BASELINE_CONFIDENCE[SourceType.GOOGLE_SCHOLAR],
    )
    merged_no_doi = Deduplicator().deduplicate_papers(
        [Paper(title="T", evidence_list=[ieee]), Paper(title="T", evidence_list=[gs])]
    )[0]
    expected_no_doi = min(1.0, base + MULTI_SOURCE_BONUS * (2 - 1))
    assert merged_no_doi.primary_evidence is not None
    assert merged_no_doi.primary_evidence.confidence == pytest.approx(expected_no_doi)

    merged_doi = Deduplicator().deduplicate_papers(
        [
            Paper(title="T", doi="10.5555/abc.def", evidence_list=[ieee]),
            Paper(title="T", doi="10.5555/abc.def", evidence_list=[gs]),
        ]
    )[0]
    expected_doi = min(
        1.0,
        min(1.0, base + MULTI_SOURCE_BONUS * (2 - 1)) + DOI_EXACT_MATCH_BONUS,
    )
    assert merged_doi.primary_evidence is not None
    assert merged_doi.primary_evidence.confidence == pytest.approx(expected_doi)
    # the field-level DOI adjustment was applied on the fusion path
    assert merged_doi.primary_evidence.confidence - merged_no_doi.primary_evidence.confidence == pytest.approx(
        DOI_EXACT_MATCH_BONUS
    )


def test_dedup_arxiv_doi_cross_id() -> None:
    """arXiv-only and DOI-only records with highly similar titles merge."""
    arxiv_paper = Paper(
        title="Deep Learning",
        authors=["LeCun"],
        year=2015,
        arxiv_id="1404.1234",
        evidence_list=[_ev(SourceType.ARXIV, 0.92, "1404.1234")],
    )
    doi_paper = Paper(
        title="Deep Learning",
        authors=["Y. LeCun"],
        year=2015,
        doi="10.1038/nature14539",
        evidence_list=[_ev(SourceType.OPENALEX, 0.90, "10.1038/nature14539")],
    )
    merged = Deduplicator().deduplicate_papers([arxiv_paper, doi_paper])
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 2


def test_dedup_sequence_matcher_title() -> None:
    """Near-identical titles merge via SequenceMatcher even without shared IDs."""
    a = Paper(
        title="The Attention Mechanism Explained",
        authors=["A"],
        year=2020,
        evidence_list=[_ev(SourceType.OPENALEX, 0.8, "oa-1")],
    )
    b = Paper(
        title="The Attention Mechanism Explained",
        authors=["B"],
        year=2020,
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.8, "s2-1")],
    )
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 2


def test_dedup_same_title_journal_vs_book_not_merged() -> None:
    """I-9: same-title journal paper and book are distinct works — no merge.

    "Deep Learning" by LeCun et al. (Nature, 2015) and the Goodfellow et al.
    book (MIT Press, 2016) share a title but are different publications. The
    title-similarity rule must be gated by year and venue-type compatibility
    so the two never fuse into one record (which would pollute the author
    list).
    """
    nature = Paper(
        title="Deep Learning",
        authors=["Yann LeCun", "Yoshua Bengio", "Geoffrey Hinton"],
        year=2015,
        venue="Nature",
        venue_type="journal",
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "10.1038/nature14539")],
    )
    book = Paper(
        title="Deep Learning",
        authors=["Ian Goodfellow", "Yoshua Bengio", "Aaron Courville"],
        year=2016,
        venue="MIT Press",
        venue_type="book",
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-book")],
    )
    merged = Deduplicator().deduplicate_papers([nature, book])
    assert len(merged) == 2


def test_dedup_seq_title_gated_by_year_gap() -> None:
    """I-9: identical titles with a >1 year gap must not merge by title only."""
    a = Paper(title="Deep Learning", authors=["A"], year=2000, evidence_list=[_ev()])
    b = Paper(title="Deep Learning", authors=["B"], year=2005, evidence_list=[_ev()])
    assert len(Deduplicator().deduplicate_papers([a, b])) == 2


def test_dedup_fuzzy_gated_by_venue_type_conflict() -> None:
    """I-9 (FIX-D-1): the venue-type guard must also apply to the fuzzy rule.

    Same title + same authors + same year, but one record is a journal article
    and the other a book: the weighted fuzzy score reaches 1.0 (≥ 0.85) and
    used to merge them, polluting the author list. The venue conflict must
    block the merge just like it does for the SequenceMatcher rule.
    """
    journal = Paper(
        title="Deep Learning",
        authors=["Yoshua Bengio", "Geoffrey Hinton"],
        year=2015,
        venue="Nature",
        venue_type="journal",
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-j")],
    )
    book = Paper(
        title="Deep Learning",
        authors=["Yoshua Bengio", "Geoffrey Hinton"],
        year=2015,
        venue="MIT Press",
        venue_type="book",
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-b")],
    )
    assert len(Deduplicator().deduplicate_papers([journal, book])) == 2


def test_dedup_fuzzy_gated_by_hard_year_gap() -> None:
    """I-9 (FIX-D-1): a >1 year gap must also block the fuzzy rule.

    Same title + same authors, 2000 vs 2005: fuzzy scores exactly 0.85
    (0.6·title + 0.25·author + 0.0·year), which is at the merge threshold and
    used to fuse two distinct works. The hard year guard must apply here too.
    """
    a = Paper(
        title="Deep Learning",
        authors=["Geoffrey Hinton"],
        year=2000,
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-a")],
    )
    b = Paper(
        title="Deep Learning",
        authors=["Geoffrey Hinton"],
        year=2005,
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-b")],
    )
    assert len(Deduplicator().deduplicate_papers([a, b])) == 2


def test_dedup_fuzzy_same_year_same_venue_still_merges() -> None:
    """I-9 (FIX-D-1): the guards must not break legitimate fuzzy merges.

    Same title + same authors + same year + same venue type: no conflict, the
    fuzzy rule still merges (no false negative from the new guards).
    """
    a = Paper(
        title="Attention Is All You Need",
        authors=["Ashish Vaswani"],
        year=2017,
        venue="NeurIPS",
        venue_type="conference",
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-a")],
    )
    b = Paper(
        title="Attention Is All You Need",
        authors=["Ashish Vaswani"],
        year=2017,
        venue="NeurIPS",
        venue_type="conference",
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-b")],
    )
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 2


def test_dedup_doi_exact_merge_unaffected_by_guards() -> None:
    """I-9 (FIX-D-1): an exact DOI match merges unconditionally.

    Even when venue type and year conflict (journal vs book, 2000 vs 2005),
    the same DOI means the same work — the ID rule must win over the guards.
    """
    a = Paper(
        title="Deep Learning",
        authors=["Geoffrey Hinton"],
        year=2000,
        venue_type="journal",
        doi="10.5555/deep.learning",
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-a")],
    )
    b = Paper(
        title="Deep Learning",
        authors=["Geoffrey Hinton"],
        year=2005,
        venue_type="book",
        doi="10.5555/deep.learning",
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-b")],
    )
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 1
    assert merged[0].doi == "10.5555/deep.learning"


def test_dedup_keeps_distinct_numeric_suffix_titles() -> None:
    """Titles differing only by a numeric suffix must NOT be over-merged."""
    a = Paper(title="Unique Paper 0", authors=["A"], year=2000, evidence_list=[_ev()])
    b = Paper(title="Unique Paper 20", authors=["A"], year=2000, evidence_list=[_ev()])
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 2


def test_dedup_exact_match_by_pmid() -> None:
    a = Paper(title="T", pmid="12345678", evidence_list=[_ev(SourceType.PUBMED, 0.92, "12345678")])
    b = Paper(title="T", pmid="12345678", evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-1")])
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 1


def test_dedup_fusion_merges_v2_fields() -> None:
    a = Paper(
        title="T",
        arxiv_id="1706.03762",
        venue_type="preprint",
        reference_count=10,
        fields_of_study=["AI"],
        references=["r1"],
        citations_list=["c1"],
        evidence_list=[_ev(SourceType.ARXIV, 0.95, "1706.03762")],
    )
    b = Paper(
        title="T",
        doi="10.5555/x.y",
        venue_type="conference",
        reference_count=12,
        fields_of_study=["NLP", "AI"],
        references=["r2", "r1"],
        citations_list=["c2"],
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "doi-x")],
    )
    merged = Deduplicator().deduplicate_papers([a, b])
    assert len(merged) == 1
    m = merged[0]
    assert m.arxiv_id == "1706.03762"
    assert m.doi == "10.5555/x.y"
    assert m.reference_count in (10, 12)
    assert m.fields_of_study == ["AI", "NLP"]
    assert m.references == ["r1", "r2"]
    assert m.citations_list == ["c1", "c2"]


def test_dedup_transitive_clustering_via_bridge() -> None:
    """I6: [A, B, C] with A~B (seq title) and B~C (same DOI) merge to 1.

    A and C share no direct match; the transitive closure through the bridge
    record B must bring them together. The seed-star algorithm absorbed B into
    A's cluster and left C split out — union-find clustering must not.
    """
    bridge_title = "Deep Learning for Neural Networks"
    other_title = "Attention Mechanisms in Transformers"
    doi = "10.5555/3295222.3295349"
    a = Paper(
        title=bridge_title,
        authors=["A"],
        year=2017,
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-a")],
    )
    b = Paper(
        title=bridge_title,
        authors=["B"],
        year=2017,
        doi=doi,
        evidence_list=[_ev(SourceType.ARXIV, 0.95, "arx-b")],
    )
    c = Paper(
        title=other_title,
        authors=["C"],
        year=2017,
        doi=doi,
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.9, "s2-c")],
    )
    # A vs C must not match directly (bridge scenario premise)
    assert len(Deduplicator().deduplicate_papers([a, c])) == 2

    merged = Deduplicator().deduplicate_papers([a, b, c])
    assert len(merged) == 1
    assert len(merged[0].evidence_list) == 3

    # order-independent: reversing the input yields the same cluster partition
    merged_rev = Deduplicator().deduplicate_papers([c, b, a])
    assert len(merged_rev) == 1
    assert len(merged_rev[0].evidence_list) == 3


def test_dedup_author_merge_keeps_identity_fields() -> None:
    """I4: author fusion keeps orcid / openalex_id / semantic_scholar_id,
    unions aliases (incl. non-canonical name variants) and applies the
    disambiguation_status rule."""
    a = Author(
        name="Ada Lovelace",
        orcid="0000-0001-2345-6789",
        openalex_id="A1",
        aliases=["Lady Ada"],
        evidence_list=[_ev(SourceType.OPENALEX, 0.9, "oa-1")],
    )
    b = Author(
        name="ada lovelace",  # non-canonical variant of the same person
        orcid="0000-0001-2345-6789",
        semantic_scholar_id="S2-1",
        aliases=["Countess of Lovelace"],
        evidence_list=[_ev(SourceType.SEMANTIC_SCHOLAR, 0.85, "s2-1")],
    )
    merged = Deduplicator().deduplicate_authors([a, b])
    assert len(merged) == 1
    m = merged[0]
    assert m.orcid == "0000-0001-2345-6789"
    assert m.openalex_id == "A1"
    assert m.semantic_scholar_id == "S2-1"
    assert "ada lovelace" in m.aliases  # name variant folded into aliases
    assert "Lady Ada" in m.aliases
    assert "Countess of Lovelace" in m.aliases
    assert m.disambiguation_status == "auto"

    # "confirmed" from any record wins
    c = Author(
        name="Ada Lovelace",
        orcid="0000-0001-2345-6789",
        disambiguation_status="confirmed",
        evidence_list=[_ev(SourceType.IEEE, 0.7, "ieee-1")],
    )
    merged_conf = Deduplicator().deduplicate_authors([a, c])
    assert len(merged_conf) == 1
    assert merged_conf[0].disambiguation_status == "confirmed"


# ---------------------------------------------------------------------------
# ConfidenceScorer (3A v2 §6.3)
# ---------------------------------------------------------------------------


def test_scorer_single_source_baselines() -> None:
    scorer = ConfidenceScorer()
    for source, baseline in SOURCE_BASELINE_CONFIDENCE.items():
        assert scorer.score([_ev(source, 0.5, f"{source.value}-1")]).confidence == pytest.approx(
            baseline
        )


def test_scorer_multi_source_bonus_capped() -> None:
    scorer = ConfidenceScorer()
    ieee = _ev(SourceType.IEEE, 0.85, "ieee-1")
    gs = _ev(SourceType.GOOGLE_SCHOLAR, 0.75, "gs-1")
    two_sources = scorer.score([ieee, gs])
    assert two_sources.confidence == pytest.approx(min(1.0, 0.85 + MULTI_SOURCE_BONUS))

    oa = _ev(SourceType.OPENALEX, 0.90, "oa-1")
    three_sources = scorer.score([ieee, gs, oa])
    expected = min(1.0, 0.90 + MULTI_SOURCE_BONUS * 2)
    assert three_sources.confidence == pytest.approx(expected)

    # arXiv (0.95) + DOI bonus would exceed 1.0 → capped
    arx = _ev(SourceType.ARXIV, 0.95, "arx-1")
    capped = ConfidenceScorer().score_paper(
        Paper(title="T", doi="10.1234/x.y", evidence_list=[arx, oa])
    )
    assert capped.primary_evidence is not None
    assert capped.primary_evidence.confidence == 1.0


def test_scorer_doi_and_pdf_adjustments() -> None:
    ev = _ev(SourceType.OPENALEX, 0.9, "10.1234/x.y")
    paper = Paper(
        title="T",
        doi="10.1234/x.y",
        pdf_url="https://example.com/paper.pdf",
        evidence_list=[ev],
    )
    scored = ConfidenceScorer().score_paper(paper)
    assert scored.primary_evidence is not None
    expected = min(1.0, 0.90 + DOI_EXACT_MATCH_BONUS + PDF_LINK_BONUS)
    assert scored.primary_evidence.confidence == pytest.approx(expected)


def test_scorer_stale_penalty() -> None:
    old = _ev(
        SourceType.OPENALEX,
        0.9,
        "oa-1",
        collected_at=datetime.now(timezone.utc) - timedelta(days=800),
    )
    paper = Paper(title="T", evidence_list=[old])
    scored = ConfidenceScorer().score_paper(paper)
    assert scored.primary_evidence is not None
    assert scored.primary_evidence.confidence == pytest.approx(0.90 - STALE_PENALTY)


def test_scorer_empty_evidence_list_raises() -> None:
    with pytest.raises(ValueError):
        ConfidenceScorer().score([])


def test_scorer_score_author() -> None:
    author = Author(
        name="Ada",
        evidence_list=[_ev(SourceType.IEEE, 0.85, "ieee-1"), _ev(SourceType.OPENALEX, 0.9, "oa-1")],
    )
    scored = ConfidenceScorer().score_author(author)
    assert scored.primary_evidence is not None
    assert scored.primary_evidence.confidence == pytest.approx(min(1.0, 0.90 + MULTI_SOURCE_BONUS))


# ---------------------------------------------------------------------------
# Storage: relationship edges + evidence table
# ---------------------------------------------------------------------------


async def _check_edge_interface(store) -> None:
    e1 = _ev(SourceType.OPENALEX, 0.8, "oa-1")
    e2 = _ev(SourceType.ARXIV, 0.95, "arx-1")
    paper = Paper(
        title="Edge Paper",
        authors=[
            AuthorRef(author_id="a1", name="Alice", position=1),
            AuthorRef(author_id="a2", name="Bob", position=2),
        ],
        year=2021,
        evidence_list=[e1, e2],
    )
    pid = await store.save_paper(paper)
    got = await store.get_paper(pid)
    assert got is not None
    assert len(got.evidence_list) == 2
    # I1: the synthetic confidence (multi-source bonus) is rebuilt on load via
    # score_paper; the loaded record does NOT fall back to the single-source
    # max (0.95):
    #   min(1.0, max(0.90, 0.95) + 0.05 * (2 - 1)) = 1.0
    assert got.primary_evidence is not None
    expected = min(
        1.0,
        SOURCE_BASELINE_CONFIDENCE[SourceType.ARXIV] + MULTI_SOURCE_BONUS * (2 - 1),
    )
    assert got.primary_evidence.confidence == pytest.approx(expected)

    # authorships
    assert await store.get_author_papers("a1") == [pid]
    # coauthorships
    assert await store.get_coauthors("a1") == ["a2"]
    assert await store.get_coauthors("a2") == ["a1"]

    # evidence table CRUD
    assert len(await store.get_evidence("paper", pid)) == 2
    await store.save_evidence("paper", pid, [e1])
    assert len(await store.get_evidence("paper", pid)) == 1

    # references / citations graph queries
    await store.save_citation(
        Citation(citing_paper_id=pid, cited_paper_id="ref-1", evidence=e1)
    )
    await store.save_citation(
        Citation(citing_paper_id="cit-1", cited_paper_id=pid, evidence=e1)
    )
    assert await store.get_references(pid) == ["ref-1"]
    assert await store.get_citations(pid) == ["cit-1"]

    # author evidence roundtrip
    author = Author(name="Alice", evidence_list=[e1, e2])
    aid = await store.save_author(author)
    ga = await store.get_author(aid)
    assert ga is not None
    assert len(ga.evidence_list) == 2
    assert len(await store.get_evidence("author", aid)) == 2


@pytest.mark.asyncio
async def test_sqlite_edge_tables_crud(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "edge.db"))
    await store.connect()
    try:
        await _check_edge_interface(store)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_edge_interface(tmp_path) -> None:
    store = JSONStorage(str(tmp_path / "data"))
    await store.connect()
    try:
        await _check_edge_interface(store)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_legacy_single_evidence_column_readable(tmp_path) -> None:
    """Old databases with a single-evidence JSON column stay readable."""
    from academic_intelligence.storage.sqlite_store import PaperRow

    store = SQLiteStorage(str(tmp_path / "legacy.db"))
    await store.connect()
    try:
        async with store._session() as session:
            session.add(
                PaperRow(
                    id="old-1",
                    title="Old Paper",
                    authors=[],
                    evidence={
                        "source": "openalex",
                        "source_url": "https://openalex.org/W1",
                        "confidence": 0.8,
                    },
                )
            )
            await session.commit()
        paper = await store.get_paper("old-1")
        assert paper is not None
        assert len(paper.evidence_list) == 1
        assert paper.primary_evidence is not None
        assert paper.primary_evidence.source == SourceType.OPENALEX
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_legacy_evidence_key_readable(tmp_path) -> None:
    """Old JSON stores that only carry a single ``evidence`` key load fine."""
    base = tmp_path / "data"
    base.mkdir()
    legacy = {
        "old-1": {
            "id": "old-1",
            "title": "Old Paper",
            "authors": [],
            "evidence": {
                "source": "openalex",
                "source_url": "https://openalex.org/W1",
                "confidence": 0.8,
            },
        }
    }
    (base / "papers.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )
    store = JSONStorage(str(base))
    await store.connect()
    try:
        paper = await store.get_paper("old-1")
        assert paper is not None
        assert len(paper.evidence_list) == 1
        assert paper.primary_evidence is not None
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# B2-FIX: I1 synthetic confidence rebuild / I3 v2 fields / M3 pseudo IDs
# ---------------------------------------------------------------------------


async def _check_synthetic_confidence_roundtrip(store) -> None:
    """I1: save->load must rebuild the synthetic composite confidence."""
    e1 = _ev(SourceType.OPENALEX, 0.8, "oa-1")
    e2 = _ev(SourceType.ARXIV, 0.95, "arx-1")
    paper = Paper(
        title="Scored Paper",
        doi="10.5555/abc.def",
        evidence_list=[e1, e2],
    )
    scored = ConfidenceScorer().score_paper(paper)
    assert scored.primary_evidence is not None
    in_memory = scored.primary_evidence.confidence
    # composite incl. DOI bonus: min(1.0, 0.95 + 0.05 + 0.05) = 1.0 > 0.95
    assert in_memory > 0.95

    pid = await store.save_paper(scored)
    got = await store.get_paper(pid)
    assert got is not None
    assert got.primary_evidence is not None
    assert got.primary_evidence.confidence == pytest.approx(in_memory)

    # query path rebuilds the same synthetic value
    q = await store.query_papers()
    assert len(q) == 1
    assert q[0].primary_evidence is not None
    assert q[0].primary_evidence.confidence == pytest.approx(in_memory)

    # authors are rebuilt with score_author as well
    author = Author(name="Ada", evidence_list=[e1, e2])
    scored_a = ConfidenceScorer().score_author(author)
    aid = await store.save_author(scored_a)
    ga = await store.get_author(aid)
    assert ga is not None
    assert ga.primary_evidence is not None
    assert ga.primary_evidence.confidence == pytest.approx(
        scored_a.primary_evidence.confidence
    )


@pytest.mark.asyncio
async def test_sqlite_synthetic_confidence_roundtrip(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "i1.db"))
    await store.connect()
    try:
        await _check_synthetic_confidence_roundtrip(store)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_synthetic_confidence_roundtrip(tmp_path) -> None:
    store = JSONStorage(str(tmp_path / "data"))
    await store.connect()
    try:
        await _check_synthetic_confidence_roundtrip(store)
    finally:
        await store.close()


async def _check_v2_fields_roundtrip(store) -> None:
    """I3: save->load keeps every v2 field (both backends agree)."""
    paper = Paper(
        id="v2-1",
        title="Attention Is All You Need",
        arxiv_id="1706.03762",
        pmid="12345678",
        venue_type="preprint",
        reference_count=10,
        fields_of_study=["cs.LG", "AI"],
        references=["ref-1", "ref-2"],
        citations_list=["cit-1"],
        evidence_list=[_ev(SourceType.ARXIV, 0.95, "1706.03762")],
    )
    await store.save_paper(paper)
    got = await store.get_paper("v2-1")
    assert got is not None
    assert got.arxiv_id == "1706.03762"
    assert got.pmid == "12345678"
    assert got.venue_type == "preprint"
    assert got.reference_count == 10
    assert got.fields_of_study == ["cs.LG", "AI"]
    assert got.references == ["ref-1", "ref-2"]
    assert got.citations_list == ["cit-1"]

    # update path keeps them too
    updated = paper.model_copy(update={"reference_count": 11})
    assert await store.update_paper("v2-1", updated)
    got2 = await store.get_paper("v2-1")
    assert got2 is not None
    assert got2.reference_count == 11
    assert got2.arxiv_id == "1706.03762"
    assert got2.fields_of_study == ["cs.LG", "AI"]
    assert got2.references == ["ref-1", "ref-2"]


@pytest.mark.asyncio
async def test_sqlite_roundtrip_preserves_v2_fields(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "v2.db"))
    await store.connect()
    try:
        await _check_v2_fields_roundtrip(store)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_roundtrip_preserves_v2_fields(tmp_path) -> None:
    store = JSONStorage(str(tmp_path / "data"))
    await store.connect()
    try:
        await _check_v2_fields_roundtrip(store)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_legacy_db_migrates_v2_columns(tmp_path) -> None:
    """I3: pre-v2 databases get the new columns via idempotent ALTER TABLE."""
    from sqlalchemy import create_engine

    db = tmp_path / "legacy_v1.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE papers ("
            "id VARCHAR(64) PRIMARY KEY, "
            "title TEXT NOT NULL, "
            "authors JSON, "
            "year INTEGER, "
            "venue TEXT, "
            "abstract TEXT, "
            "doi VARCHAR(255), "
            "url TEXT, "
            "pdf_url TEXT, "
            "citations INTEGER, "
            "keywords JSON, "
            "evidence JSON NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO papers (id, title, evidence) VALUES ('old-1', 'Old', '[]')"
        )
    engine.dispose()

    store = SQLiteStorage(str(db))
    await store.connect()
    try:
        # old row still readable after migration
        old = await store.get_paper("old-1")
        assert old is not None and old.title == "Old"
        # new v2 columns are writeable
        paper = Paper(
            id="v2-new",
            title="New",
            arxiv_id="1706.03762",
            fields_of_study=["cs.LG"],
            references=["x"],
            citations_list=["y"],
            evidence_list=[_ev()],
        )
        await store.save_paper(paper)
        got = await store.get_paper("v2-new")
        assert got is not None
        assert got.arxiv_id == "1706.03762"
        assert got.fields_of_study == ["cs.LG"]
        assert got.references == ["x"]
        assert got.citations_list == ["y"]
        # reconnecting runs the migration again without error (idempotent)
    finally:
        await store.close()
    store2 = SQLiteStorage(str(db))
    await store2.connect()
    try:
        got = await store2.get_paper("v2-new")
        assert got is not None and got.arxiv_id == "1706.03762"
    finally:
        await store2.close()


async def _check_pseudo_id_filtering(store) -> None:
    """M3: unresolved ``~name`` pseudo IDs never leak into graph results."""
    paper = Paper(
        title="T",
        authors=[
            AuthorRef(author_id="a1", name="Alice", position=1),
            AuthorRef(name="Bob", position=2),  # unresolved -> ~Bob
        ],
        year=2021,
        evidence_list=[_ev()],
    )
    pid = await store.save_paper(paper)
    # unresolved authors are still queryable by their pseudo key ...
    assert await store.get_author_papers("~Bob") == [pid]
    # ... but the pseudo ID is filtered out of coauthor results
    assert await store.get_coauthors("a1") == []
    # paper IDs returned by get_author_papers never carry the ~ prefix
    assert all(not x.startswith("~") for x in await store.get_author_papers("a1"))


@pytest.mark.asyncio
async def test_sqlite_coauthors_filter_pseudo_ids(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "m3.db"))
    await store.connect()
    try:
        await _check_pseudo_id_filtering(store)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_coauthors_filter_pseudo_ids(tmp_path) -> None:
    store = JSONStorage(str(tmp_path / "data"))
    await store.connect()
    try:
        await _check_pseudo_id_filtering(store)
    finally:
        await store.close()
