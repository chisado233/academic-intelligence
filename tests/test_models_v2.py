"""Tests for the 3A v2 data model extensions.

Covers the new ``AuthorRef`` model, ``Paper.authors`` typed as
``List[AuthorRef]`` (with string coercion), new ``Author`` identifier /
disambiguation fields, new ``Paper`` graph fields, ``Evidence.source_id``,
and the v2 ``Config`` extension fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    Evidence,
    Paper,
)
from academic_intelligence.core.types import Config, SourceType


def _ev() -> Evidence:
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W1",
        confidence=0.85,
    )


# ---------------------------------------------------------------------------
# AuthorRef
# ---------------------------------------------------------------------------


def test_author_ref_defaults() -> None:
    ref = AuthorRef(name="Ada Lovelace")
    assert ref.author_id is None
    assert ref.name == "Ada Lovelace"
    assert ref.position == 1
    assert ref.is_corresponding is False
    assert ref.affiliation is None


def test_author_ref_full_instantiation() -> None:
    ref = AuthorRef(
        author_id="a-1",
        name="Ada Lovelace",
        position=2,
        is_corresponding=True,
        affiliation="University College London",
    )
    assert ref.author_id == "a-1"
    assert ref.position == 2
    assert ref.is_corresponding is True
    assert ref.affiliation == "University College London"


def test_author_ref_roundtrip() -> None:
    ref = AuthorRef(
        author_id="a-1",
        name="Ada Lovelace",
        position=2,
        is_corresponding=True,
        affiliation="UCL",
    )
    restored = AuthorRef.from_dict(ref.to_dict())
    assert restored == ref
    assert restored.author_id == "a-1"
    assert restored.is_corresponding is True


def test_author_ref_empty_name_rejected() -> None:
    with pytest.raises(ValidationError):
        AuthorRef(name="   ")
    with pytest.raises(ValidationError):
        AuthorRef(name="")


def test_author_ref_bad_position_rejected() -> None:
    with pytest.raises(ValidationError):
        AuthorRef(name="Ada", position=0)


# ---------------------------------------------------------------------------
# Paper.authors: AuthorRef list with string coercion
# ---------------------------------------------------------------------------


def test_paper_authors_from_strings() -> None:
    paper = Paper(title="T", authors=["Ada", "Bob"], evidence=_ev())
    assert len(paper.authors) == 2
    assert all(isinstance(a, AuthorRef) for a in paper.authors)
    assert [a.name for a in paper.authors] == ["Ada", "Bob"]
    assert paper.authors[0].position == 1
    assert paper.authors[1].position == 2


def test_paper_authors_from_author_refs() -> None:
    refs = [
        AuthorRef(name="Ada", position=1),
        AuthorRef(name="Bob", position=2, is_corresponding=True),
    ]
    paper = Paper(title="T", authors=refs, evidence=_ev())
    assert [a.name for a in paper.authors] == ["Ada", "Bob"]
    assert paper.authors[1].is_corresponding is True


def test_paper_authors_mixed_inputs() -> None:
    paper = Paper(
        title="T",
        authors=["Ada", AuthorRef(name="Bob", position=2)],
        evidence=_ev(),
    )
    assert [a.name for a in paper.authors] == ["Ada", "Bob"]
    assert paper.authors[0].position == 1
    assert paper.authors[1].position == 2


def test_paper_authors_from_dicts() -> None:
    paper = Paper(
        title="T",
        authors=[{"name": "Ada", "position": 1}],
        evidence=_ev(),
    )
    assert isinstance(paper.authors[0], AuthorRef)
    assert paper.authors[0].name == "Ada"


def test_paper_authors_default_empty() -> None:
    paper = Paper(title="T", evidence=_ev())
    assert paper.authors == []


def test_paper_authors_name_semantics() -> None:
    """author.name must be usable on every element of paper.authors."""
    paper = Paper(title="T", authors=["Ada Lovelace"], evidence=_ev())
    assert paper.authors[0].name == "Ada Lovelace"


def test_paper_authors_json_roundtrip() -> None:
    paper = Paper(title="T", authors=["Ada", "Bob"], evidence=_ev())
    restored = Paper.from_dict(paper.to_dict())
    assert isinstance(restored.authors[0], AuthorRef)
    assert [a.name for a in restored.authors] == ["Ada", "Bob"]


# ---------------------------------------------------------------------------
# Author v2 fields
# ---------------------------------------------------------------------------


def test_author_new_field_defaults() -> None:
    author = Author(name="Ada Lovelace", evidence=_ev())
    assert author.orcid is None
    assert author.semantic_scholar_id is None
    assert author.openalex_id is None
    assert author.aliases == []
    assert author.disambiguation_status == "auto"


def test_author_new_fields_set() -> None:
    author = Author(
        name="Ada Lovelace",
        orcid="0000-0001-2345-6789",
        semantic_scholar_id="s2-1",
        openalex_id="A1",
        aliases=["A. Lovelace", "Augusta Ada King"],
        disambiguation_status="confirmed",
        evidence=_ev(),
    )
    assert author.orcid == "0000-0001-2345-6789"
    assert author.semantic_scholar_id == "s2-1"
    assert author.openalex_id == "A1"
    assert author.aliases == ["A. Lovelace", "Augusta Ada King"]
    assert author.disambiguation_status == "confirmed"


def test_author_invalid_orcid_rejected() -> None:
    with pytest.raises(ValidationError):
        Author(name="Ada", orcid="not-an-orcid", evidence=_ev())
    with pytest.raises(ValidationError):
        Author(name="Ada", orcid="0000-0001-2345", evidence=_ev())


def test_author_orcid_normalized_from_url() -> None:
    author = Author(
        name="Ada",
        orcid="https://orcid.org/0000-0001-2345-6789",
        evidence=_ev(),
    )
    assert author.orcid == "0000-0001-2345-6789"


def test_author_new_fields_roundtrip() -> None:
    author = Author(
        name="Ada",
        orcid="0000-0001-2345-6789",
        semantic_scholar_id="s2-9",
        aliases=["A. Lovelace"],
        disambiguation_status="ambiguous",
        evidence=_ev(),
    )
    restored = Author.from_dict(author.to_dict())
    assert restored.orcid == "0000-0001-2345-6789"
    assert restored.semantic_scholar_id == "s2-9"
    assert restored.aliases == ["A. Lovelace"]
    assert restored.disambiguation_status == "ambiguous"


# ---------------------------------------------------------------------------
# Paper v2 fields
# ---------------------------------------------------------------------------


def test_paper_new_field_defaults() -> None:
    paper = Paper(title="T", evidence=_ev())
    assert paper.arxiv_id is None
    assert paper.pmid is None
    assert paper.venue_type is None
    assert paper.reference_count is None
    assert paper.fields_of_study == []
    assert paper.references is None
    assert paper.citations_list is None
    assert paper.citations is None  # legacy count field untouched


def test_paper_new_fields_set() -> None:
    paper = Paper(
        title="T",
        arxiv_id="1706.03762",
        pmid="12345678",
        venue_type="journal",
        reference_count=42,
        fields_of_study=["Computer Science", "AI"],
        references=["r1", "r2"],
        citations_list=["c1"],
        citations=99,
        evidence=_ev(),
    )
    assert paper.arxiv_id == "1706.03762"
    assert paper.pmid == "12345678"
    assert paper.venue_type == "journal"
    assert paper.reference_count == 42
    assert paper.fields_of_study == ["Computer Science", "AI"]
    assert paper.references == ["r1", "r2"]
    assert paper.citations_list == ["c1"]
    assert paper.citations == 99


def test_paper_negative_reference_count_rejected() -> None:
    with pytest.raises(ValidationError):
        Paper(title="T", reference_count=-1, evidence=_ev())


def test_paper_new_fields_roundtrip() -> None:
    paper = Paper(
        title="T",
        arxiv_id="1706.03762",
        pmid="12345678",
        fields_of_study=["AI"],
        references=["a", "b"],
        citations_list=["c"],
        evidence=_ev(),
    )
    restored = Paper.from_dict(paper.to_dict())
    assert restored.arxiv_id == "1706.03762"
    assert restored.pmid == "12345678"
    assert restored.fields_of_study == ["AI"]
    assert restored.references == ["a", "b"]
    assert restored.citations_list == ["c"]


# ---------------------------------------------------------------------------
# Evidence.source_id
# ---------------------------------------------------------------------------


def test_evidence_source_id_default() -> None:
    ev = Evidence(source=SourceType.ARXIV, source_url="https://arxiv.org")
    assert ev.source_id is None


def test_evidence_source_id_set_and_roundtrip() -> None:
    ev = Evidence(
        source=SourceType.ARXIV,
        source_url="https://arxiv.org/abs/1706.03762",
        source_id="1706.03762",
    )
    assert ev.source_id == "1706.03762"
    restored = Evidence.from_dict(ev.to_dict())
    assert restored.source_id == "1706.03762"


# ---------------------------------------------------------------------------
# Config v2 fields
# ---------------------------------------------------------------------------


def test_config_v2_defaults() -> None:
    cfg = Config()
    assert cfg.enable_google_scholar is False
    assert cfg.download_delay == 1.0
    assert cfg.max_concurrent_requests == 4
    assert cfg.max_expand_depth == 3
    assert cfg.max_expand_nodes == 50
    assert cfg.graph_cache_size == 5000
    assert cfg.auto_merge_threshold == 0.85
    assert cfg.ambiguous_threshold == 0.60
    assert cfg.paper_refresh_days == 7
    assert cfg.author_refresh_days == 30


def test_config_v2_set_values() -> None:
    cfg = Config(
        enable_google_scholar=True,
        download_delay=2.5,
        max_concurrent_requests=8,
        max_expand_depth=5,
        max_expand_nodes=100,
        graph_cache_size=10000,
        auto_merge_threshold=0.9,
        ambiguous_threshold=0.5,
        paper_refresh_days=14,
        author_refresh_days=60,
    )
    assert cfg.enable_google_scholar is True
    assert cfg.download_delay == 2.5
    assert cfg.max_concurrent_requests == 8
    assert cfg.max_expand_depth == 5
    assert cfg.max_expand_nodes == 100
    assert cfg.graph_cache_size == 10000
    assert cfg.auto_merge_threshold == 0.9
    assert cfg.ambiguous_threshold == 0.5
    assert cfg.paper_refresh_days == 14
    assert cfg.author_refresh_days == 60


def test_config_v2_threshold_validation() -> None:
    with pytest.raises(ValidationError):
        Config(auto_merge_threshold=1.5)
    with pytest.raises(ValidationError):
        Config(ambiguous_threshold=-0.1)


def test_config_v2_int_validation() -> None:
    with pytest.raises(ValidationError):
        Config(max_concurrent_requests=0)
    with pytest.raises(ValidationError):
        Config(max_expand_depth=0)
    with pytest.raises(ValidationError):
        Config(max_expand_nodes=-5)
    with pytest.raises(ValidationError):
        Config(graph_cache_size=0)
    with pytest.raises(ValidationError):
        Config(paper_refresh_days=0)
    with pytest.raises(ValidationError):
        Config(author_refresh_days=-1)


def test_config_v2_roundtrip_and_legacy_methods_preserved() -> None:
    cfg = Config(proxy="http://p:1", auto_merge_threshold=0.9, max_expand_nodes=77)
    restored = Config.from_dict(cfg.to_dict())
    assert restored.auto_merge_threshold == 0.9
    assert restored.max_expand_nodes == 77
    assert restored.enable_google_scholar is False
    assert restored.proxy_list() == ["http://p:1"]


def test_config_secret_fields_not_serialized_in_plaintext() -> None:
    """I-7: API keys never appear in plaintext in to_dict()/str()/repr()."""
    cfg = Config(
        serpapi_key="sk-test",
        semantic_scholar_api_key="s2-secret-key",
        ieee_api_key="ieee-secret-key",
        openalex_email="alice@example.com",
    )
    serialized = str(cfg.to_dict())
    assert "sk-test" not in serialized
    assert "s2-secret-key" not in serialized
    assert "ieee-secret-key" not in serialized
    combined = f"{cfg!r}{cfg}"
    assert "sk-test" not in combined
    assert "s2-secret-key" not in combined
    assert "ieee-secret-key" not in combined
    # secrets remain retrievable for the adapters
    assert cfg.serpapi_key is not None
    assert cfg.serpapi_key.get_secret_value() == "sk-test"
    assert cfg.ieee_api_key is not None
    assert cfg.ieee_api_key.get_secret_value() == "ieee-secret-key"
