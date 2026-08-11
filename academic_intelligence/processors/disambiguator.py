"""Author disambiguation processor (3A v2 design §6.2).

Separates distinct people who share a name ("同名不同人") using a two-layer
strategy:

1. **ID direct link** — any two records sharing an authority ID (ORCID /
   Semantic Scholar author ID / OpenAlex author ID) are the same person.
2. **Heuristic clustering** — without authority IDs, a weighted feature
   vector decides the outcome:

   - ``>= auto_merge_threshold`` (0.85) → auto-merge,
     ``disambiguation_status = "auto"``;
   - ``ambiguous_threshold`` (0.60) .. auto → marked ``"ambiguous"``, kept
     separate, waiting for the third-layer confirmation interface (Phase 1
     placeholders only);
   - ``< ambiguous_threshold`` → distinct people.

Relationship to :class:`~academic_intelligence.processors.deduplicator.Deduplicator`
(方案 1): disambiguation is an independent, more-strict stage that runs after
name-based deduplication. Name dedup merges identical spellings; the
disambiguator additionally merges ID-linked records the name matcher cannot
see and marks genuinely uncertain pairs as ``ambiguous`` without merging them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import NamedTuple

from academic_intelligence.core.models import Author
from academic_intelligence.processors.deduplicator import Deduplicator

# Institution words that carry little identity information (affiliation
# overlap is computed on the remaining distinguishing tokens).
_GENERIC_AFFILIATION_WORDS = {
    "university",
    "universities",
    "institute",
    "institutes",
    "college",
    "school",
    "department",
    "dept",
    "faculty",
    "laboratory",
    "lab",
    "center",
    "centre",
    "of",
    "the",
    "and",
    "at",
    "research",
}


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation; keep CJK characters intact."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff ]", " ", text.lower()).strip()


def _token_set(text: str) -> set[str]:
    return {t for t in _normalize(text).split() if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _surname(name: str) -> str | None:
    parts = _normalize(name).split()
    return parts[-1] if parts else None


# ---------------------------------------------------------------------------
# Feature functions (3A v2 §6.2 DisambiguationFeatures)
# ---------------------------------------------------------------------------


def name_similarity(a: str, b: str) -> float:
    """Name variant similarity.

    Equal normalized names and same-token reorderings ("Wei Zhang" /
    "Zhang Wei") score 1.0; SequenceMatcher similarity is boosted when the
    surnames (last tokens) match, which covers "Wei Zhang" / "W. Zhang".
    """
    na, nb = _normalize(a), _normalize(b)
    if not na and not nb:
        return 0.5
    if na == nb:
        return 1.0
    ta, tb = _token_set(a), _token_set(b)
    if ta == tb and ta:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    sa, sb = _surname(a), _surname(b)
    if sa and sb and sa == sb:
        seq = min(1.0, seq + 0.15)
    return seq


def affiliation_overlap(a: str | None, b: str | None) -> float:
    """Affiliation overlap via substring containment and token Jaccard.

    Missing data on both sides is neutral (0.5); one-sided data scores 0.0.
    """
    if not a and not b:
        return 0.5
    if not a or not b:
        return 0.0
    na, nb = _normalize(a), _normalize(b)
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    ga = _token_set(a) - _GENERIC_AFFILIATION_WORDS
    gb = _token_set(b) - _GENERIC_AFFILIATION_WORDS
    return _jaccard(ga, gb)


def topic_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """Research topic similarity (Jaccard over interest token sets)."""
    ta = _token_set(" ".join(a)) if a else set()
    tb = _token_set(" ".join(b)) if b else set()
    if not ta and not tb:
        return 0.5
    if not ta or not tb:
        return 0.0
    return _jaccard(ta, tb)


def coauthor_overlap(a: Sequence[str], b: Sequence[str]) -> float:
    """Co-author network overlap (Jaccard over normalized name sets)."""
    ta = {_normalize(n) for n in a}
    tb = {_normalize(n) for n in b}
    if not ta and not tb:
        return 0.5
    if not ta or not tb:
        return 0.0
    return _jaccard(ta, tb)


def year_range_overlap(a: Sequence[int] | None, b: Sequence[int] | None) -> float:
    """Active-year range overlap (intersection / union of the year spans)."""
    ya = [int(y) for y in (a or [])]
    yb = [int(y) for y in (b or [])]
    if not ya and not yb:
        return 0.5
    if not ya or not yb:
        return 0.0
    lo_a, hi_a = min(ya), max(ya)
    lo_b, hi_b = min(yb), max(yb)
    inter = max(0, min(hi_a, hi_b) - max(lo_a, lo_b) + 1)
    union = max(hi_a, hi_b) - min(lo_a, lo_b) + 1
    return inter / union if union else 0.0


def venue_overlap(a: Sequence[str], b: Sequence[str]) -> float:
    """Publication venue overlap (Jaccard over normalized venue names)."""
    ta = {_normalize(v) for v in a}
    tb = {_normalize(v) for v in b}
    if not ta and not tb:
        return 0.5
    if not ta or not tb:
        return 0.0
    return _jaccard(ta, tb)


# ---------------------------------------------------------------------------
# Configuration & score types
# ---------------------------------------------------------------------------


class DisambiguationConfig:
    """Weights and thresholds for author disambiguation.

    Default weights sum to 1.0 (name .35 / affiliation .25 / topic .15 /
    coauthor .10 / year .075 / venue .075); features with no data on either
    side contribute a neutral 0.5 so missing context never penalizes a pair.
    """

    def __init__(
        self,
        auto_merge_threshold: float = 0.85,
        ambiguous_threshold: float = 0.60,
        name_weight: float = 0.35,
        affiliation_weight: float = 0.25,
        topic_weight: float = 0.15,
        coauthor_weight: float = 0.10,
        year_weight: float = 0.075,
        venue_weight: float = 0.075,
    ) -> None:
        if not 0.0 <= ambiguous_threshold <= auto_merge_threshold <= 1.0:
            raise ValueError(
                "need 0 <= ambiguous_threshold <= auto_merge_threshold <= 1, "
                f"got ambiguous={ambiguous_threshold}, auto={auto_merge_threshold}"
            )
        for name, weight in (
            ("name_weight", name_weight),
            ("affiliation_weight", affiliation_weight),
            ("topic_weight", topic_weight),
            ("coauthor_weight", coauthor_weight),
            ("year_weight", year_weight),
            ("venue_weight", venue_weight),
        ):
            if not 0.0 <= weight <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {weight}")
        self.auto_merge_threshold = auto_merge_threshold
        self.ambiguous_threshold = ambiguous_threshold
        self.name_weight = name_weight
        self.affiliation_weight = affiliation_weight
        self.topic_weight = topic_weight
        self.coauthor_weight = coauthor_weight
        self.year_weight = year_weight
        self.venue_weight = venue_weight


class DisambiguationScore(NamedTuple):
    """Per-pair disambiguation score with feature breakdown."""

    total: float
    name_similarity: float
    affiliation_overlap: float
    topic_similarity: float
    coauthor_overlap: float
    year_range_overlap: float
    venue_overlap: float
    id_linked: bool = False


# ---------------------------------------------------------------------------
# Disambiguator
# ---------------------------------------------------------------------------


class AuthorDisambiguator:
    """Two-layer author identity disambiguation (3A v2 §6.2)."""

    def __init__(self, config: DisambiguationConfig | None = None) -> None:
        self.config = config or DisambiguationConfig()
        self._dedup = Deduplicator()

    # -- Layer 1: ID direct link ------------------------------------------

    @staticmethod
    def id_linked(a: Author, b: Author) -> bool:
        """True when two records share any authority ID (ORCID / S2 / OpenAlex)."""
        return bool(
            (a.orcid and b.orcid and a.orcid == b.orcid)
            or (
                a.semantic_scholar_id
                and b.semantic_scholar_id
                and a.semantic_scholar_id == b.semantic_scholar_id
            )
            or (a.openalex_id and b.openalex_id and a.openalex_id == b.openalex_id)
        )

    @staticmethod
    def authority_ids_conflict(a: Author, b: Author) -> bool:
        """True when the same authority system identifies different people."""
        return bool(
            (a.orcid and b.orcid and a.orcid != b.orcid)
            or (
                a.semantic_scholar_id
                and b.semantic_scholar_id
                and a.semantic_scholar_id != b.semantic_scholar_id
            )
            or (
                a.openalex_id
                and b.openalex_id
                and a.openalex_id != b.openalex_id
            )
        )

    # -- Layer 2: heuristic scoring ----------------------------------------

    def score_pair(self, a: Author, b: Author) -> DisambiguationScore:
        """Return the weighted feature score for one author pair.

        ID-linked pairs score a perfect 1.0 (authoritative identity wins over
        any heuristic disagreement).
        """
        name = name_similarity(a.name, b.name)
        affiliation = affiliation_overlap(a.affiliation, b.affiliation)
        topic = topic_similarity(a.interests, b.interests)
        coauthor = coauthor_overlap(a.coauthors, b.coauthors)
        year = year_range_overlap(a.active_years, b.active_years)
        venue = venue_overlap(a.venues, b.venues)

        if self.authority_ids_conflict(a, b):
            return DisambiguationScore(
                total=0.0,
                name_similarity=name,
                affiliation_overlap=affiliation,
                topic_similarity=topic,
                coauthor_overlap=coauthor,
                year_range_overlap=year,
                venue_overlap=venue,
            )

        if self.id_linked(a, b):
            return DisambiguationScore(
                total=1.0,
                name_similarity=name,
                affiliation_overlap=affiliation,
                topic_similarity=topic,
                coauthor_overlap=coauthor,
                year_range_overlap=year,
                venue_overlap=venue,
                id_linked=True,
            )

        total = (
            self.config.name_weight * name
            + self.config.affiliation_weight * affiliation
            + self.config.topic_weight * topic
            + self.config.coauthor_weight * coauthor
            + self.config.year_weight * year
            + self.config.venue_weight * venue
        )
        # Exact normalized name + institution is the active design's strong
        # cross-source identity signal.  A small corroboration bonus lets two
        # otherwise sparse profiles reach the default 0.85 merge threshold,
        # while an explicit topic disagreement receives no bonus and remains
        # ambiguous.  Same-system authority-ID conflicts were vetoed above.
        if name == 1.0 and affiliation >= 0.9 and topic >= 0.5:
            total = min(1.0, total + 0.05)
        return DisambiguationScore(
            total=total,
            name_similarity=name,
            affiliation_overlap=affiliation,
            topic_similarity=topic,
            coauthor_overlap=coauthor,
            year_range_overlap=year,
            venue_overlap=venue,
        )

    # -- Clustering --------------------------------------------------------

    def cluster(self, authors: Sequence[Author]) -> list[list[Author]]:
        """Group authors into identity clusters without merging.

        Union-find over strong pairs only (ID-linked or score >= auto-merge
        threshold); ambiguous pairs are deliberately NOT unioned.
        """
        items = list(authors)
        n = len(items)
        if not items:
            return []
        if n == 1:
            return [items]

        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a_idx: int, b_idx: int) -> None:
            root_a, root_b = find(a_idx), find(b_idx)
            if root_a != root_b:
                parent[root_b] = root_a

        for i in range(n):
            for j in range(i + 1, n):
                score = self.score_pair(items[i], items[j])
                if score.id_linked or score.total >= self.config.auto_merge_threshold:
                    union(i, j)

        clusters: list[list[Author]] = []
        root_index: dict[int, int] = {}
        for i, author in enumerate(items):
            root = find(i)
            if root not in root_index:
                root_index[root] = len(clusters)
                clusters.append([])
            clusters[root_index[root]].append(author)
        return clusters

    # -- Merging & ambiguous marking ----------------------------------------

    def disambiguate(self, authors: Sequence[Author]) -> list[Author]:
        """Merge ID-linked / high-similarity records and mark ambiguous pairs.

        Returns the disambiguated author list:

        - clusters of >= 2 records are fused into one record (identity fields
          unioned, evidence merged) with ``disambiguation_status`` preserved
          from :meth:`Deduplicator._merge_authors` ("confirmed" if any member
          was confirmed, else "auto");
        - records that share an ambiguous-range pair (0.60 .. 0.85) with a
          record in a different cluster are flagged ``"ambiguous"`` and left
          unmerged, waiting for the user-confirmation layer.
        """
        items = list(authors)
        if not items:
            return []

        clusters = self.cluster(items)

        merged: list[Author] = []
        output_of: dict[int, int] = {}  # id(original) -> output index
        for cluster in clusters:
            if len(cluster) == 1:
                merged.append(cluster[0])
            else:
                merged.append(self._merge_cluster(cluster))
            for original in cluster:
                output_of[id(original)] = len(merged) - 1

        ambiguous_outputs: set[int] = set()
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if output_of[id(items[i])] == output_of[id(items[j])]:
                    continue
                score = self.score_pair(items[i], items[j])
                if (
                    self.config.ambiguous_threshold
                    <= score.total
                    < self.config.auto_merge_threshold
                ):
                    ambiguous_outputs.add(output_of[id(items[i])])
                    ambiguous_outputs.add(output_of[id(items[j])])

        for idx in ambiguous_outputs:
            record = merged[idx]
            if record.disambiguation_status == "auto":
                merged[idx] = record.model_copy(
                    update={"disambiguation_status": "ambiguous"}
                )
        return merged

    def _merge_cluster(self, cluster: Sequence[Author]) -> Author:
        """Fuse one identity cluster, unioning disambiguation context.

        Field-level fusion reuses :meth:`Deduplicator._merge_authors` so the
        fused record follows the exact same confidence-weighted merge and
        ``disambiguation_status`` rules as the dedup path; coauthor / venue /
        active-year context is unioned on top (those fields do not exist on
        the dedup path).
        """
        merged = self._dedup._merge_authors(cluster)  # noqa: SLF001 (same package)
        coauthors = list(dict.fromkeys(n for a in cluster for n in a.coauthors))
        venues = list(dict.fromkeys(v for a in cluster for v in a.venues))
        years = [y for a in cluster for y in (a.active_years or [])]
        active_years: list[int] | None = sorted(set(years)) if years else None
        if coauthors or venues or active_years:
            merged = merged.model_copy(
                update={
                    "coauthors": coauthors,
                    "venues": venues,
                    "active_years": active_years,
                }
            )
        return merged
