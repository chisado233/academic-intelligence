"""FIX-S ticket tests (B7-P37 round 19 standard-compliance gaps).

- S1: ORCID iDs are validated against the ISO/IEC 7064 MOD 11-2 checksum so
  a well-formed-looking id with a wrong check digit (e.g. ``0000-0001-2345-6780``)
  is rejected instead of stored.
- S2: PMIDs follow the NCBI spec (1-8 pure digits); garbage like ``"abc"`` or
  11-digit strings is rejected by the model and softened to ``None`` in the
  source parsers so one bad record never drops the whole parse.
- S3: arXiv ID normalization rejects trailing garbage (``"2301.00001x"``) so
  fake ids never pollute the dedup key space; version suffixes still
  normalize away and old-style ids pass through unchanged.
- S4: ``academic_intelligence.__all__`` covers the public symbols referenced
  by SKILL.md, so ``from academic_intelligence import *`` exposes them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from academic_intelligence.core.models import Author, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.processors.deduplicator import Deduplicator, _normalize_arxiv_id
from academic_intelligence.sources.pubmed import PubMedSource
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev(
    source: SourceType = SourceType.OPENALEX,
    conf: float = 0.8,
) -> Evidence:
    return Evidence(
        source=source,
        source_url=f"https://{source.value}/record",
        confidence=conf,
        collected_at=datetime.now(UTC),
    )


def _pubmed_article(pmid: str, title: str = "Soft PMID paper") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <ArticleTitle>{title}</ArticleTitle>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


# ---------------------------------------------------------------------------
# S1: ORCID checksum (ISO/IEC 7064 MOD 11-2)
# ---------------------------------------------------------------------------


def test_orcid_valid_checksum_accepted() -> None:
    for valid in ("0000-0001-2345-6789", "0000-0002-1825-0097", "0000-0002-1694-233X"):
        author = Author(name="Ada", orcid=valid, evidence=_ev())
        assert author.orcid == valid


def test_orcid_lowercase_x_normalized() -> None:
    author = Author(name="Ada", orcid="0000-0002-1694-233x", evidence=_ev())
    assert author.orcid == "0000-0002-1694-233X"


def test_orcid_wrong_checksum_rejected() -> None:
    # ``0000-0001-2345-6780`` is well-formed but the check digit must be 9.
    with pytest.raises(ValidationError):
        Author(name="Ada", orcid="0000-0001-2345-6780", evidence=_ev())


def test_orcid_wrong_x_checksum_rejected() -> None:
    # Base ``000000021694233`` requires check digit X; a digit 9 must fail.
    with pytest.raises(ValidationError):
        Author(name="Ada", orcid="0000-0002-1694-2339", evidence=_ev())


# ---------------------------------------------------------------------------
# S2: PMID validation (NCBI: 1-8 pure digits)
# ---------------------------------------------------------------------------


def test_paper_valid_pmid_accepted() -> None:
    for valid in ("26017442", "1", "99999999"):
        paper = Paper(title="T", pmid=valid, evidence=_ev())
        assert paper.pmid == valid


def test_paper_invalid_pmid_rejected() -> None:
    for invalid in ("abc", "12a34", "999999999", "12345678901"):
        with pytest.raises(ValidationError):
            Paper(title="T", pmid=invalid, evidence=_ev())


def test_paper_empty_pmid_softened_to_none() -> None:
    assert Paper(title="T", pmid="", evidence=_ev()).pmid is None


def test_semantic_scholar_parse_softens_invalid_pmid() -> None:
    src = SemanticScholarSource(http_client=MagicMock())
    paper = src._parse_paper({"title": "S2 Paper", "externalIds": {"PMID": "bad-pmid-12"}})
    assert paper.pmid is None
    assert paper.title == "S2 Paper"  # the record survives the bad PMID


def test_pubmed_parse_softens_invalid_pmid() -> None:
    src = PubMedSource(http_client=MagicMock())
    papers = src._parse_efetch_xml(_pubmed_article("not-a-pmid"))
    assert len(papers) == 1
    assert papers[0].pmid is None
    assert papers[0].title == "Soft PMID paper"


def test_pubmed_parse_keeps_valid_pmid() -> None:
    src = PubMedSource(http_client=MagicMock())
    papers = src._parse_efetch_xml(_pubmed_article("26017442"))
    assert len(papers) == 1
    assert papers[0].pmid == "26017442"


# ---------------------------------------------------------------------------
# S3: arXiv ID trailing-garbage hardening
# ---------------------------------------------------------------------------


def test_normalize_arxiv_id_rejects_trailing_garbage() -> None:
    assert _normalize_arxiv_id("2301.00001x") is None
    assert _normalize_arxiv_id("2301.00001v3x") is None


def test_normalize_arxiv_id_strips_version() -> None:
    assert _normalize_arxiv_id("2301.00001v3") == "2301.00001"
    assert _normalize_arxiv_id("https://arxiv.org/abs/2301.00001v1") == "2301.00001"


def test_normalize_arxiv_id_keeps_old_style() -> None:
    assert _normalize_arxiv_id("hep-th/9901001") == "hep-th/9901001"
    assert _normalize_arxiv_id("cs/0501001v2") == "cs/0501001"


def test_dedup_garbage_arxiv_id_not_in_key_space() -> None:
    d = Deduplicator()
    a = Paper(title="Real arXiv paper A", arxiv_id="2301.00001", evidence=_ev())
    b = Paper(title="Completely Different Paper", arxiv_id="2301.00001x", evidence=_ev())
    merged = d.deduplicate_papers([a, b])
    assert len(merged) == 2  # the garbage id must never join the key space


# ---------------------------------------------------------------------------
# S4: __all__ covers SKILL.md-referenced public symbols
# ---------------------------------------------------------------------------

PUBLIC_SYMBOLS = [
    "Deduplicator",
    "KnowledgeGraph",
    "IncrementalProcessor",
    "GoogleScholarSource",
    "SemanticScholarSource",
    "OpenAlexSource",
    "ArxivSource",
    "PubMedSource",
    "IEEESource",
    "BaseSource",
    "BaseStorage",
    "HTTPClient",
    "JSONStorage",
    "SQLiteStorage",
    "Cache",
    "expand_from_graph",
    "author_entity_key",
]


def test_star_import_exposes_public_symbols() -> None:
    ns: dict[str, object] = {}
    exec("from academic_intelligence import *", ns)  # noqa: S102
    for name in PUBLIC_SYMBOLS:
        assert name in ns, f"{name!r} missing from academic_intelligence.__all__"
