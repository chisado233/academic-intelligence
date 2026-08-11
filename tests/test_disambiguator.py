"""Unit tests for the author disambiguation processor (3A v2 design §6.2).

Covers both error directions the advisor cares about:
- false merge: two distinct people sharing a name must NOT be merged;
- missed merge: one person across sources (ID-linked or same profile) MUST be merged.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from academic_intelligence.core.models import Author, Evidence
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.deduplicator import Deduplicator
from academic_intelligence.processors.disambiguator import (
    AuthorDisambiguator,
    DisambiguationConfig,
    DisambiguationScore,
    affiliation_overlap,
    coauthor_overlap,
    name_similarity,
    topic_similarity,
    venue_overlap,
    year_range_overlap,
)
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage

ORCID_A = "0000-0001-2345-6789"
ORCID_B = "0000-0002-1825-0097"


def _ev(source: SourceType = SourceType.OPENALEX, conf: float = 0.8) -> Evidence:
    return Evidence(source=source, source_url="https://example.com", confidence=conf)


def _author(**kwargs: object) -> Author:
    defaults: dict[str, object] = {
        "name": "Wei Zhang",
        "evidence": _ev(),
    }
    defaults.update(kwargs)
    return Author(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_disambiguation_config_defaults() -> None:
    config = DisambiguationConfig()
    assert config.auto_merge_threshold == pytest.approx(0.85)
    assert config.ambiguous_threshold == pytest.approx(0.60)
    assert config.auto_merge_threshold > config.ambiguous_threshold


def test_disambiguation_config_validation() -> None:
    with pytest.raises(ValueError):
        DisambiguationConfig(ambiguous_threshold=0.9, auto_merge_threshold=0.85)
    with pytest.raises(ValueError):
        DisambiguationConfig(auto_merge_threshold=1.5)


def test_custom_weights_change_score() -> None:
    a = _author(affiliation="Tsinghua University", interests=["computer vision"])
    b = _author(affiliation="Tsinghua University", interests=["computer vision"])
    default = AuthorDisambiguator().score_pair(a, b)
    heavy_name = AuthorDisambiguator(
        DisambiguationConfig(
            name_weight=0.9,
            affiliation_weight=0.025,
            topic_weight=0.025,
            coauthor_weight=0.025,
            year_weight=0.025,
            venue_weight=0.0,
        )
    ).score_pair(a, b)
    assert default.total != heavy_name.total


# ---------------------------------------------------------------------------
# Feature functions
# ---------------------------------------------------------------------------


def test_name_similarity_variants() -> None:
    # Same name spelled with initials ("Wei Zhang" / "W. Zhang")
    assert name_similarity("Wei Zhang", "W. Zhang") >= 0.85
    # Same tokens, different order ("Wei Zhang" / "Zhang Wei")
    assert name_similarity("Wei Zhang", "Zhang Wei") == pytest.approx(1.0)
    # Identical names
    assert name_similarity("Wei Zhang", "Wei Zhang") == pytest.approx(1.0)
    # Unrelated names stay low
    assert name_similarity("Wei Zhang", "John Smith") < 0.5


def test_affiliation_overlap() -> None:
    # Same institution with a department suffix
    assert affiliation_overlap("Stanford University", "Stanford University, Dept of CS") >= 0.85
    # Different institutions stay low
    assert affiliation_overlap("Tsinghua University", "Stanford University") < 0.5
    # Identical
    assert affiliation_overlap("Stanford University", "Stanford University") == pytest.approx(1.0)
    # Missing data is neutral
    assert affiliation_overlap(None, None) == pytest.approx(0.5)
    assert affiliation_overlap("Stanford University", None) == pytest.approx(0.0)


def test_topic_similarity() -> None:
    assert topic_similarity([], []) == pytest.approx(0.5)
    assert topic_similarity(["NLP"], []) == pytest.approx(0.0)
    assert topic_similarity(["NLP", "deep learning"], ["NLP", "deep learning"]) == pytest.approx(1.0)
    overlap = topic_similarity(["NLP", "deep learning"], ["NLP", "medical imaging"])
    assert 0.0 < overlap < 1.0


def test_coauthor_overlap() -> None:
    assert coauthor_overlap(["Alice", "Bob"], ["Alice", "Carol"]) == pytest.approx(1 / 3)
    assert coauthor_overlap(["Alice"], ["Bob"]) == pytest.approx(0.0)
    assert coauthor_overlap([], []) == pytest.approx(0.5)


def test_year_range_overlap() -> None:
    assert year_range_overlap([2015, 2018, 2020], [2018, 2021]) == pytest.approx(3 / 7)
    assert year_range_overlap([2015], [2020]) == pytest.approx(0.0)
    assert year_range_overlap([2020], [2020]) == pytest.approx(1.0)
    assert year_range_overlap(None, None) == pytest.approx(0.5)


def test_venue_overlap() -> None:
    assert venue_overlap(["NeurIPS"], ["NeurIPS"]) == pytest.approx(1.0)
    assert venue_overlap(["NeurIPS"], ["ICML"]) == pytest.approx(0.0)
    assert venue_overlap([], []) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# score_pair
# ---------------------------------------------------------------------------


def test_score_pair_id_direct_link_orcid() -> None:
    a = _author(name="Wei Zhang", orcid=ORCID_A, affiliation="Tsinghua University")
    b = _author(name="W. Zhang", orcid=ORCID_A, affiliation="Stanford University")
    score = AuthorDisambiguator().score_pair(a, b)
    assert score.id_linked is True
    assert score.total == pytest.approx(1.0)


def test_score_pair_id_direct_link_any_authority_id() -> None:
    dis = AuthorDisambiguator()
    # Semantic Scholar id
    assert dis.score_pair(
        _author(semantic_scholar_id="S2-1"),
        _author(semantic_scholar_id="S2-1"),
    ).id_linked is True
    # OpenAlex id
    assert dis.score_pair(
        _author(openalex_id="A123"),
        _author(openalex_id="A123"),
    ).id_linked is True


def test_score_pair_no_shared_id_not_linked() -> None:
    a = _author(orcid=ORCID_A, openalex_id="A1", semantic_scholar_id="S2-1")
    b = _author(orcid=ORCID_B, openalex_id="A2", semantic_scholar_id="S2-2")
    assert AuthorDisambiguator().score_pair(a, b).id_linked is False


def test_score_pair_different_people_below_threshold() -> None:
    """Two distinct 'Wei Zhang' (different institution, different topics)."""
    a = _author(
        name="Wei Zhang",
        affiliation="Tsinghua University",
        interests=["computer vision"],
    )
    b = _author(
        name="Wei Zhang",
        affiliation="Stanford University",
        interests=["biomedical imaging"],
    )
    score = AuthorDisambiguator().score_pair(a, b)
    assert score.total < 0.60
    assert score.name_similarity == pytest.approx(1.0)
    assert score.affiliation_overlap == pytest.approx(0.0)
    assert score.topic_similarity == pytest.approx(0.0)


def test_score_pair_same_person_high() -> None:
    """Same name + same affiliation + same topics merge (>= 0.85)."""
    a = _author(
        name="Wei Zhang",
        affiliation="Tsinghua University",
        interests=["computer vision"],
    )
    b = _author(
        name="Wei Zhang",
        affiliation="Tsinghua University",
        interests=["computer vision"],
    )
    score = AuthorDisambiguator().score_pair(a, b)
    assert score.total >= 0.85


def test_score_pair_ambiguous_range() -> None:
    """Same name + same affiliation but different topics -> ambiguous band."""
    a = _author(name="Wei Zhang", affiliation="Tsinghua University", interests=["computer vision"])
    b = _author(name="Wei Zhang", affiliation="Tsinghua University", interests=["medicine"])
    score = AuthorDisambiguator().score_pair(a, b)
    assert 0.60 <= score.total < 0.85


# ---------------------------------------------------------------------------
# cluster
# ---------------------------------------------------------------------------


def test_cluster_groups_id_linked_and_high_similarity() -> None:
    a = _author(name="Wei Zhang", orcid=ORCID_A)
    b = _author(name="W. Zhang", orcid=ORCID_A)  # ID-linked to a
    c = _author(name="John Smith", affiliation="MIT", interests=["physics"])
    clusters = AuthorDisambiguator().cluster([a, b, c])
    assert len(clusters) == 2
    grouped = {id(member) for cluster in clusters for member in cluster}
    assert len(grouped) == 3
    # a and b must land in the same cluster
    for cluster in clusters:
        members = {id(m) for m in cluster}
        if id(a) in members:
            assert id(b) in members
            assert id(c) not in members


def test_cluster_empty_and_single() -> None:
    dis = AuthorDisambiguator()
    assert dis.cluster([]) == []
    single = [_author()]
    clusters = dis.cluster(single)
    assert len(clusters) == 1
    assert len(clusters[0]) == 1


# ---------------------------------------------------------------------------
# disambiguate
# ---------------------------------------------------------------------------


def test_disambiguate_merges_id_linked_records() -> None:
    a = _author(
        name="Wei Zhang",
        orcid=ORCID_A,
        evidence=_ev(SourceType.OPENALEX, 0.9),
    )
    b = _author(
        name="W. Zhang",
        orcid=ORCID_A,
        evidence=_ev(SourceType.SEMANTIC_SCHOLAR, 0.88),
    )
    result = AuthorDisambiguator().disambiguate([a, b])
    assert len(result) == 1
    merged = result[0]
    assert merged.orcid == ORCID_A
    assert merged.disambiguation_status == "auto"
    # Both sources' evidence are preserved
    sources = {ev.source for ev in merged.evidence_list}
    assert SourceType.OPENALEX in sources
    assert SourceType.SEMANTIC_SCHOLAR in sources


def test_disambiguate_preserves_confirmed_status() -> None:
    a = _author(name="Wei Zhang", orcid=ORCID_A, disambiguation_status="confirmed")
    b = _author(name="W. Zhang", orcid=ORCID_A)
    result = AuthorDisambiguator().disambiguate([a, b])
    assert len(result) == 1
    assert result[0].disambiguation_status == "confirmed"


def test_disambiguate_merges_aliases() -> None:
    a = _author(name="Wei Zhang", orcid=ORCID_A, evidence=_ev(conf=0.95))
    b = _author(name="W. Zhang", orcid=ORCID_A, evidence=_ev(conf=0.7))
    result = AuthorDisambiguator().disambiguate([a, b])
    assert len(result) == 1
    assert result[0].name == "Wei Zhang"
    assert "W. Zhang" in result[0].aliases


def test_disambiguate_keeps_different_people() -> None:
    """False-merge guard: two 'Wei Zhang' with different profiles stay apart."""
    a = _author(name="Wei Zhang", affiliation="Tsinghua University", interests=["computer vision"])
    b = _author(name="Wei Zhang", affiliation="Stanford University", interests=["biomedical imaging"])
    result = AuthorDisambiguator().disambiguate([a, b])
    assert len(result) == 2
    assert all(author.disambiguation_status == "auto" for author in result)


def test_disambiguate_marks_ambiguous_pairs() -> None:
    a = _author(name="Wei Zhang", affiliation="Tsinghua University", interests=["computer vision"])
    b = _author(name="Wei Zhang", affiliation="Tsinghua University", interests=["medicine"])
    result = AuthorDisambiguator().disambiguate([a, b])
    assert len(result) == 2  # not merged
    assert all(author.disambiguation_status == "ambiguous" for author in result)


def test_disambiguate_empty_and_single() -> None:
    dis = AuthorDisambiguator()
    assert dis.disambiguate([]) == []
    single = [_author(name="Wei Zhang")]
    assert len(dis.disambiguate(single)) == 1


def test_disambiguate_catches_what_dedup_misses() -> None:
    """方案1 relationship: disambiguator runs after name-based dedup.

    Name-only deduplication cannot merge "Wei Zhang" with "W. Zhang"
    (token Jaccard < 0.8), but the ID direct link layer merges them.
    """
    a = _author(name="Wei Zhang", orcid=ORCID_A, evidence=_ev(SourceType.OPENALEX, 0.9))
    b = _author(name="W. Zhang", orcid=ORCID_A, evidence=_ev(SourceType.OPENALEX, 0.8))
    deduped = Deduplicator().deduplicate_authors([a, b])
    assert len(deduped) == 2
    result = AuthorDisambiguator().disambiguate(deduped)
    assert len(result) == 1
    assert result[0].orcid == ORCID_A


def test_disambiguate_unions_context_features() -> None:
    a = _author(
        name="Wei Zhang",
        orcid=ORCID_A,
        coauthors=["Alice", "Bob"],
        venues=["NeurIPS"],
        active_years=[2018, 2019],
    )
    b = _author(
        name="W. Zhang",
        orcid=ORCID_A,
        coauthors=["Carol"],
        venues=["ICML"],
        active_years=[2020],
    )
    result = AuthorDisambiguator().disambiguate([a, b])
    assert len(result) == 1
    merged = result[0]
    assert set(merged.coauthors) == {"Alice", "Bob", "Carol"}
    assert set(merged.venues) == {"NeurIPS", "ICML"}
    assert merged.active_years == [2018, 2019, 2020]


# ---------------------------------------------------------------------------
# Persistence of disambiguation state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disambiguation_status_persisted_json(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONStorage(tmp)
        await store.connect()
        try:
            merged = AuthorDisambiguator().disambiguate(
                [
                    _author(name="Wei Zhang", orcid=ORCID_A, evidence=_ev(conf=0.9)),
                    _author(name="W. Zhang", orcid=ORCID_A, evidence=_ev(conf=0.8)),
                ]
            )[0]
            aid = await store.save_author(merged)
            loaded = await store.get_author(aid)
            assert loaded is not None
            assert loaded.disambiguation_status == "auto"
            assert loaded.orcid == ORCID_A
            assert "W. Zhang" in loaded.aliases
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_disambiguation_status_persisted_sqlite(tmp_path: Path) -> None:
    db = str(Path(tmp_path) / "disambig.db")
    store = SQLiteStorage(db)
    await store.connect()
    try:
        ambiguous = AuthorDisambiguator().disambiguate(
            [
                _author(
                    name="Wei Zhang",
                    affiliation="Tsinghua University",
                    interests=["computer vision"],
                ),
                _author(
                    name="Wei Zhang",
                    affiliation="Tsinghua University",
                    interests=["medicine"],
                ),
            ]
        )
        assert [a.disambiguation_status for a in ambiguous] == ["ambiguous", "ambiguous"]
        for a in ambiguous:
            aid = await store.save_author(a)
            loaded = await store.get_author(aid)
            assert loaded is not None
            assert loaded.disambiguation_status == "ambiguous"
    finally:
        await store.close()


def test_score_type_shape() -> None:
    score = AuthorDisambiguator().score_pair(
        _author(name="Wei Zhang", affiliation="MIT"),
        _author(name="Wei Zhang", affiliation="MIT"),
    )
    assert isinstance(score, DisambiguationScore)
    assert 0.0 <= score.total <= 1.0
    fields = [
        score.name_similarity,
        score.affiliation_overlap,
        score.topic_similarity,
        score.coauthor_overlap,
        score.year_range_overlap,
        score.venue_overlap,
    ]
    assert all(0.0 <= f <= 1.0 for f in fields)
