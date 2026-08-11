"""Deduplication and fusion processor for academic data.

Merges duplicate records across multiple sources using ID-based exact
matching, cross-ID mapping (arXiv ↔ DOI), SequenceMatcher title similarity,
and confidence-weighted field resolution (3A v2 design §6.1).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import NamedTuple

from academic_intelligence.core.models import Author, AuthorRef, Evidence, Paper
from academic_intelligence.processors.scorer import ConfidenceScorer
from academic_intelligence.sources.arxiv import _ARXIV_ID_RE

_ARXIV_ID_PREFIXES = ("arxiv:", "https://arxiv.org/abs/", "http://arxiv.org/abs/")

# ``Paper.evidence_list`` / ``Author.evidence_list`` Pydantic max_length; the
# merged evidence list is capped here so a pathological mega-cluster never
# trips a bare ValidationError and takes the collect pipeline down (FIX-L F2).
EVIDENCE_LIST_MAX = 500

# Venue types that represent book-like publications (I-9). A book and a
# periodical article that merely share a title are distinct works and must
# not be fused by the title-similarity rule.
_BOOK_VENUE_TYPES = frozenset(
    {
        "book",
        "chapter",
        "monograph",
        "reference_book",
        "reference-book",
        "reference_entry",
        "reference-entry",
        "edited_book",
        "edited-book",
        "book_chapter",
        "book-chapter",
        "book_set",
        "book-set",
    }
)

# ``Deduplicator.deduplicate_papers`` dispatches to the bucketed
# implementation (:meth:`Deduplicator._deduplicate_papers_bucketed`) once the
# input exceeds this size.  For smaller inputs the original O(n²) loop runs so
# the exact ``compared`` counts small tests assert (FIX-Q Q5) stay unchanged;
# the partition produced by both paths is identical (see the bucketed
# docstring for the exactness argument).
BUCKET_DEDUP_THRESHOLD = 1024

# Conflict-type bits used to classify a paper's ``_id_conflict`` guard set
# (doi / arxiv / pmid).  Only these three types participate in the guard; url
# and the internal id are ID-merge keys but never conflict types.
_DOI_BIT = 1
_ARXIV_BIT = 2
_PMID_BIT = 4

# Venue types that represent periodical / conference publications.
_PERIODICAL_VENUE_TYPES = frozenset(
    {
        "journal",
        "journal_article",
        "journal-article",
        "article",
        "conference",
        "conference_paper",
        "conference-paper",
        "proceedings",
        "proceedings_article",
        "proceedings-article",
        "conference_proceedings",
        "conference-proceedings",
        "review",
    }
)


def _normalize_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# Stopwords dropped when comparing venue names so "Journal of X" and
# "J. X" share a comparable token form (FIX-N F2 / N2).
_VENUE_STOPWORDS = frozenset({"the", "of", "and", "&"})


def _normalize_venue_name(venue: str) -> str:
    """Token-normalize a venue name for conflict comparison (N2).

    Lowercases, strips punctuation (abbreviation dots, dashes, …), drops
    articles/conjunctions, and collapses whitespace so "Med Image Anal."
    and "Medical image analysis" produce comparable token sequences.
    """
    v = venue.strip().lower().replace("&", " and ")
    v = re.sub(r"[^\w\s]", " ", v)
    return " ".join(t for t in v.split() if t and t not in _VENUE_STOPWORDS)


def _venue_names_equivalent(venue_a: str, venue_b: str) -> bool:
    """Return True when two venue strings name the same journal (N2).

    Handles abbreviation vs full-name variants ("Med Image Anal." vs
    "Medical image analysis", "J. Neurosci." vs "Journal of
    Neuroscience"): after token normalization both forms must have the
    same token count and every token of one form must be a prefix of the
    corresponding token of the other.  Genuinely different venues
    ("Nature" vs "Science") never satisfy this, so real conflicts are
    still reported.
    """
    tokens_a = _normalize_venue_name(venue_a).split()
    tokens_b = _normalize_venue_name(venue_b).split()
    if tokens_a == tokens_b:
        return True
    if not tokens_a or not tokens_b or len(tokens_a) != len(tokens_b):
        return False
    return all(
        a.startswith(b) or b.startswith(a)
        for a, b in zip(tokens_a, tokens_b, strict=True)
    )


def _token_set(text: str) -> set[str]:
    return {t for t in _normalize_title(text).split() if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _sequence_ratio(a: str, b: str) -> float:
    """SequenceMatcher ratio on already-normalized strings."""
    return SequenceMatcher(None, a, b).ratio()


def _normalize_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    for prefix in _ARXIV_ID_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    # (FIX-S S3) Strict arXiv ID validation: only the two canonical forms
    # (``YYMM.NNNNN`` or ``archive/YYMMNNN``, optionally versioned) are
    # accepted.  Trailing garbage such as ``"2301.00001x"`` fails the full
    # match and normalizes to ``None`` so fake ids never pollute the dedup
    # key space.
    if _ARXIV_ID_RE.fullmatch(cleaned) is None:
        return None
    # Strip the version suffix (v1/v2/...): records of the same arXiv paper
    # that carry different versions are the same work, so versioned and
    # unversioned forms must compare equal (FIX-L F1 arXiv ID guard).
    cleaned = re.sub(r"v\d+$", "", cleaned)
    return cleaned or None


def _record_source(paper: Paper) -> str:
    """Best-effort source label for a paper record (its primary evidence)."""
    evidence = paper.primary_evidence
    return evidence.source.value if evidence is not None else "unknown"


def _share_exact_identity(a: Paper, b: Paper) -> bool:
    """Return True when the two records carry the same exact ID.

    (FIX-P F5 / P5) Title conflicts are only reported for merges forced by a
    shared exact ID (DOI / arXiv / PMID / URL / internal id): title-similarity
    merges gate on title similarity themselves, so a title difference there is
    the merge rule, not a conflict.
    """
    if a.doi and b.doi and a.doi.lower() == b.doi.lower():
        return True
    a_arxiv = _normalize_arxiv_id(a.arxiv_id)
    b_arxiv = _normalize_arxiv_id(b.arxiv_id)
    if a_arxiv and b_arxiv and a_arxiv == b_arxiv:
        return True
    if a.pmid and b.pmid and a.pmid.strip().lower() == b.pmid.strip().lower():
        return True
    if a.url and b.url and a.url.rstrip("/").lower() == b.url.rstrip("/").lower():
        return True
    return bool(a.id and b.id and a.id == b.id)


def _id_key_values(features: _PaperMatchData) -> list[str]:
    """Exact-ID key values of a paper's precomputed match data.

    Used by the bucketed dedup's global union: two papers sharing any key
    value are merged exactly like :meth:`Deduplicator._exact_match` would.
    The keys are built from the *normalized* feature fields (DOI lowercased,
    arXiv ID normalized, PMID stripped, URL normalized, internal id raw) so
    the union behaves identically to ``_exact_match`` — including the arXiv
    normalization that the P33 prototype omitted (a raw-``lower()`` key would
    have missed ``"arxiv:2301.00001"`` vs ``"2301.00001"`` exact matches).
    """
    keys: list[str] = []
    if features.doi is not None:
        keys.append("doi:" + features.doi)
    if features.arxiv_id is not None:
        keys.append("arxiv:" + features.arxiv_id)
    if features.pmid is not None:
        keys.append("pmid:" + features.pmid)
    if features.url is not None:
        keys.append("url:" + features.url)
    if features.paper_id is not None:
        keys.append("id:" + str(features.paper_id))
    return keys


def _conflict_mask(features: _PaperMatchData) -> int:
    """Bitmask of the conflict types a paper carries (doi / arxiv / pmid).

    Membership uses the normalized feature fields (``is not None``) so a raw
    ``arxiv_id`` that fails normalization (e.g. ``"2301.00001x"``) lands in
    the no-conflict class instead of a fake arXiv class — the same semantics
    ``_id_conflict`` / ``_cross_id_match`` apply to the features.
    """
    mask = 0
    if features.doi is not None:
        mask |= _DOI_BIT
    if features.arxiv_id is not None:
        mask |= _ARXIV_BIT
    if features.pmid is not None:
        mask |= _PMID_BIT
    return mask


def _cross_id_potential(mask_a: int, mask_b: int) -> bool:
    """Whether every pair across classes *mask_a* / *mask_b* may hit the
    arXiv ↔ DOI cross-ID rule.

    ``_cross_id_match`` fires before ``_id_conflict`` in :meth:`_matches`, so
    a pair where one side carries an arXiv ID and the other a DOI can merge on
    title similarity even when the two records otherwise conflict (e.g. an
    arXiv-only record vs a record carrying both a different arXiv ID and a
    DOI).  Class membership is uniform (a class is the set of conflict types
    every member carries), so the predicate depends only on the two masks.
    """
    return bool(
        (mask_a & _ARXIV_BIT and mask_b & _DOI_BIT)
        or (mask_a & _DOI_BIT and mask_b & _ARXIV_BIT)
    )


def _titles_conflict(a: Paper, b: Paper) -> bool:
    """Return True when the two normalized titles differ (FIX-P F5 / P5)."""
    return _normalize_title(a.title) != _normalize_title(b.title)


def detect_field_conflicts(ordered: Sequence[Paper]) -> list[str]:
    """Detect same-field multi-source conflicts among duplicate records.

    *ordered* must be sorted by confidence descending so ``ordered[0]`` is
    the record whose values the merge actually kept.  Each conflict is
    reported as a human-readable warning, e.g. ``"year conflict:
    openalex=2025 vs arxiv=2017"`` (I-10: conflicts need an exposure
    channel instead of being silently swallowed by the merge).  Title
    conflicts (FIX-P F5 / P5) are reported when a merge forced by a shared
    exact ID pairs normalized-different titles.
    """
    if len(ordered) < 2:
        return []
    base = ordered[0]
    base_source = _record_source(base)
    warnings: list[str] = []
    for other in ordered[1:]:
        other_source = _record_source(other)
        # Year conflict: both present, different, and the gap exceeds the
        # ±1 tolerance used by the matching rules.
        if (
            base.year is not None
            and other.year is not None
            and base.year != other.year
            and abs(base.year - other.year) > 1
        ):
            warnings.append(
                f"year conflict: {base_source}={base.year} vs "
                f"{other_source}={other.year}"
            )
        if (
            base.venue
            and other.venue
            and not _venue_names_equivalent(base.venue, other.venue)
        ):
            warnings.append(
                f"venue conflict: {base_source}={base.venue!r} vs "
                f"{other_source}={other.venue!r}"
            )
        # Title conflict (P5): only for ID-forced merges, where a normalized
        # title difference is a genuine data-quality signal.
        if _share_exact_identity(base, other) and _titles_conflict(base, other):
            warnings.append(
                f"title conflict: {base_source}={base.title} vs {other_source}={other.title}"
            )
    return warnings


class _PaperMatchData(NamedTuple):
    """Precomputed, normalized fields used for paper similarity matching.

    Building these once per paper (instead of inside every pairwise
    comparison of the O(n²) deduplication loop) removes the repeated
    Pydantic attribute access and normalization from the hot path.
    """

    doi: str | None
    url: str | None
    paper_id: str | None
    arxiv_id: str | None
    pmid: str | None
    title_norm: str
    title_tokens: set[str]
    author_names: set[str]
    year: int | None
    venue_type: str | None


class SimilarityConfig:
    """Configuration for similarity thresholds and weights."""

    def __init__(
        self,
        title_threshold: float = 0.85,
        author_threshold: float = 0.80,
        year_tolerance: int = 1,
        venue_threshold: float = 0.75,
        seq_title_threshold: float = 0.92,
        cross_id_title_threshold: float = 0.92,
        seq_jaccard_aux_threshold: float = 0.75,
    ) -> None:
        self.title_threshold = title_threshold
        self.author_threshold = author_threshold
        self.year_tolerance = year_tolerance
        self.venue_threshold = venue_threshold
        # SequenceMatcher-based title similarity (3A v2 §6.1 rule 3).
        self.seq_title_threshold = seq_title_threshold
        # Title similarity required for the arXiv ↔ DOI cross-ID merge.
        self.cross_id_title_threshold = cross_id_title_threshold
        # Jaccard token overlap retained as an auxiliary guard for the
        # SequenceMatcher rule (titles differing only by a numeric suffix
        # such as "Paper 0" vs "Paper 20" must not be over-merged).
        self.seq_jaccard_aux_threshold = seq_jaccard_aux_threshold


class Deduplicator:
    """Merges duplicate academic records across multiple sources."""

    def __init__(self, config: SimilarityConfig | None = None) -> None:
        self.config = config or SimilarityConfig()
        self._scorer = ConfidenceScorer()
        self._stats: dict[str, int] = {
            "compared": 0,
            "merged": 0,
            "clusters": 0,
            "evidence_truncated": 0,
        }
        self._warnings: list[str] = []

    def deduplicate_papers(self, papers: Sequence[Paper]) -> list[Paper]:
        """Remove duplicate papers and return merged unique records.

        Clustering uses a transitive closure (union-find): pairs of records
        are tested with the match rules and all mutually reachable records
        are merged into one cluster. Unlike a seed-star walk this is
        input-order independent — a bridge record that matches two otherwise
        unrelated records pulls them into the same cluster instead of being
        absorbed by the first seed (I6).

        (B7-P43) For inputs below :data:`BUCKET_DEDUP_THRESHOLD` every pair is
        tested (the O(n²) loop); larger inputs dispatch to the bucketed
        implementation (:meth:`_deduplicate_papers_bucketed`), which skips
        only pairs that are provably unmergeable (exact-ID union, conflict-
        class skip, title-token blocking) and produces the identical cluster
        partition — 10k same-DOI records drop from 71-93s to well under a
        second.

        (Q5) The stats are reset at the start of every call so
        ``compared``/``merged``/``clusters``/``evidence_truncated`` all
        describe this call only — previously ``compared``/``merged``
        accumulated across calls while ``clusters`` was overwritten each
        call, i.e. the same field carried two different semantics.
        """
        self._warnings = []
        self.reset_stats()
        if not papers:
            return []

        items = list(papers)
        n = len(items)
        if n <= 1:
            return items

        # (B7-P43) Scaling path: once the input is large enough that the
        # O(n²) pairwise loop is the bottleneck (10k same-DOI records took
        # 71-93s), dispatch to the bucketed implementation.  Both paths
        # produce the identical cluster partition (see
        # :meth:`_deduplicate_papers_bucketed`); small inputs keep the
        # original loop so the exact ``compared`` counts the Q5 stats tests
        # assert are unchanged.
        if n >= BUCKET_DEDUP_THRESHOLD:
            return self._deduplicate_papers_bucketed(items)

        # Precompute normalized match fields once per paper so the O(n²)
        # comparison loop never re-accesses Pydantic models or rebuilds
        # normalization artifacts (keyed by id() since Paper.id may be None).
        features = {id(p): self._precompute_paper(p) for p in items}

        # Union-Find over the input indices with path halving; the resulting
        # connected components are the dedup clusters.
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        for i in range(n):
            data_i = features[id(items[i])]
            for j in range(i + 1, n):
                self._stats["compared"] += 1
                if self._matches(data_i, features[id(items[j])]):
                    union(i, j)

        # Group by root in first-appearance order. The cluster partition is
        # input-order independent; element order within a cluster follows the
        # input order.
        clusters: list[list[Paper]] = []
        root_index: dict[int, int] = {}
        for i, paper in enumerate(items):
            root = find(i)
            if root not in root_index:
                root_index[root] = len(clusters)
                clusters.append([])
            clusters[root_index[root]].append(paper)

        self._stats["clusters"] = len(clusters)
        result: list[Paper] = []
        for cluster in clusters:
            if len(cluster) == 1:
                result.append(cluster[0])
            else:
                self._stats["merged"] += len(cluster) - 1
                result.append(self._merge_papers(cluster))
        return result

    def deduplicate_papers_bucketed(
        self,
        papers: Sequence[Paper],
        *,
        title_token_block: bool = True,
    ) -> list[Paper]:
        """Bucketed dedup entry point (B7-P43 V1).

        Public API identical to :meth:`deduplicate_papers` (same stats /
        warnings semantics); ``title_token_block`` toggles the title-token
        blocking refinement, which is exact (never changes the partition).
        """
        self._warnings = []
        self.reset_stats()
        if not papers:
            return []
        items = list(papers)
        if len(items) <= 1:
            return items
        return self._deduplicate_papers_bucketed(
            items, title_token_block=title_token_block
        )

    def _deduplicate_papers_bucketed(
        self,
        items: list[Paper],
        *,
        title_token_block: bool = True,
    ) -> list[Paper]:
        """Bucketed deduplication (exact-consistency, B7-P43 V1).

        Inputs ≥ :data:`BUCKET_DEDUP_THRESHOLD` take this path; the produced
        cluster partition is provably identical to the original O(n²)
        union-find loop.  Design (validated in P33, with the cross-ID fix
        below):

        - **Global ID union**: papers sharing any exact-ID key value (DOI /
          arXiv / PMID / URL / internal id) are unioned directly — this
          replicates ``_exact_match`` plus its transitive closure.
        - **Conflict classes**: each paper is classified by the subset of
          conflict types (doi / arxiv / pmid) it carries (from the normalized
          features).  Within a non-empty class, two non-union papers carry
          the same conflict type with different values, so ``_id_conflict``
          blocks every title rule — except when the class carries BOTH arXiv
          and DOI, in which case every pair is arXiv↔DOI cross-ID potential
          and must be compared (``_cross_id_match`` fires before
          ``_id_conflict``).
        - **Between classes**: overlapping classes block every pair (shared
          conflict type, different values) unless the masks are cross-ID
          potential; disjoint classes have no ``_id_conflict`` and must be
          compared.
        - **Empty class** (no conflict types): pairs have no ``_id_conflict``
          guard, so title-based merges are possible and pairs must be
          compared.
        - **Title-token blocking** (refinement, still exact): every
          title-based merge rule requires a non-empty normalized-token
          intersection (seq-title needs Jaccard ≥ 0.75; the weighted score
          caps at 0.40 with Jaccard 0), and the cross-ID rule needs a ≥ 0.92
          SequenceMatcher ratio which disjoint single-token titles can reach
          — so pairs are only skipped when their token sets are disjoint AND
          they are not cross-ID potential.  Empty-title papers can only merge
          with other empty-title papers (Jaccard(∅,∅)=1.0), so they are
          compared among themselves.
        """
        n = len(items)
        features = {id(p): self._precompute_paper(p) for p in items}

        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        # 1. Global union over exact-ID key values.
        key_members: dict[str, list[int]] = {}
        for i, paper in enumerate(items):
            for key in _id_key_values(features[id(paper)]):
                key_members.setdefault(key, []).append(i)
        for members in key_members.values():
            if len(members) >= 2:
                first = members[0]
                for member in members[1:]:
                    union(first, member)

        # 2. Conflict classes + per-class title-token index (built once so the
        # token-blocked comparisons never re-normalize titles).
        class_members: dict[int, list[int]] = {}
        tok_index: dict[int, dict[str, set[int]]] = {}
        empty_title: dict[int, list[int]] = {}
        for i, paper in enumerate(items):
            mask = _conflict_mask(features[id(paper)])
            class_members.setdefault(mask, []).append(i)
        for mask, members in class_members.items():
            tokens: dict[str, set[int]] = {}
            empties: list[int] = []
            for i in members:
                title_tokens = features[id(items[i])].title_tokens
                if not title_tokens:
                    empties.append(i)
                    continue
                for token in title_tokens:
                    tokens.setdefault(token, set()).add(i)
            tok_index[mask] = tokens
            empty_title[mask] = empties

        compared = 0

        def compare(i: int, j: int) -> None:
            nonlocal compared
            compared += 1
            if self._matches(features[id(items[i])], features[id(items[j])]):
                union(i, j)

        def compare_all(left: list[int], right: list[int]) -> None:
            """Compare every (i, j) pair — used when every pair is cross-ID
            potential, where token blocking would be unsound."""
            nonlocal compared
            for i in left:
                data_i = features[id(items[i])]
                for j in right:
                    compared += 1
                    if self._matches(data_i, features[id(items[j])]):
                        union(i, j)

        def compare_token_filtered(
            left: list[int],
            right: list[int],
            right_tokens: dict[str, set[int]],
            left_empties: list[int],
            right_empties: list[int],
        ) -> None:
            """Compare pairs sharing a title token (or both empty-title).

            *left* / *right* belong to disjoint classes, so every unordered
            pair is visited exactly once; empty-title members of *left* can
            only merge with empty-title members of *right*.
            """
            for i in left:
                title_tokens = features[id(items[i])].title_tokens
                if not title_tokens:
                    continue
                candidates: set[int] = set()
                for token in title_tokens:
                    candidates.update(right_tokens.get(token, ()))
                for j in candidates:
                    compare(i, j)
            for i in left_empties:
                for j in right_empties:
                    compare(i, j)

        def compare_within(mask: int) -> None:
            members = class_members[mask]
            if mask == 0:
                # Empty class: no _id_conflict guard -> token-intersecting and
                # both-empty-title pairs must be compared (each pair once via
                # j > i).
                own_tokens = tok_index[mask]
                for i in members:
                    title_tokens = features[id(items[i])].title_tokens
                    if not title_tokens:
                        continue
                    candidates: set[int] = set()
                    for token in title_tokens:
                        candidates.update(own_tokens.get(token, ()))
                    for j in candidates:
                        if j > i:
                            compare(i, j)
                empties = empty_title[mask]
                for k in range(len(empties)):
                    for other in empties[k + 1 :]:
                        compare(empties[k], other)
            elif (mask & _DOI_BIT) and (mask & _ARXIV_BIT):
                # Class carries both arXiv and DOI -> every pair is cross-ID
                # potential; compare all.
                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        compare(members[i], members[j])
            # else: skip — non-union pairs carry the shared conflict type with
            # different values and are not cross-ID potential, so every merge
            # rule is blocked.

        # 3. Class-pair sweep.  masks are sorted for deterministic iteration.
        masks = sorted(class_members)
        for a in range(len(masks)):
            for b in range(a, len(masks)):
                mask_a, mask_b = masks[a], masks[b]
                if mask_a == mask_b:
                    compare_within(mask_a)
                elif mask_a & mask_b:
                    # Overlapping classes: every pair carries a shared conflict
                    # type with different values -> blocked, unless the pair is
                    # arXiv↔DOI cross-ID potential.
                    if _cross_id_potential(mask_a, mask_b):
                        compare_all(class_members[mask_a], class_members[mask_b])
                elif title_token_block:
                    # Disjoint classes: no _id_conflict -> compare token-
                    # intersecting pairs (or all pairs when cross-ID potential,
                    # where token blocking would be unsound).
                    if _cross_id_potential(mask_a, mask_b):
                        compare_all(class_members[mask_a], class_members[mask_b])
                    else:
                        members_a = class_members[mask_a]
                        members_b = class_members[mask_b]
                        if len(members_a) <= len(members_b):
                            left = members_a
                            right = members_b
                            left_empties = empty_title[mask_a]
                            right_empties = empty_title[mask_b]
                            right_tokens = tok_index[mask_b]
                        else:
                            left = members_b
                            right = members_a
                            left_empties = empty_title[mask_b]
                            right_empties = empty_title[mask_a]
                            right_tokens = tok_index[mask_a]
                        compare_token_filtered(
                            left, right, right_tokens, left_empties, right_empties
                        )
                else:
                    compare_all(class_members[mask_a], class_members[mask_b])

        self._stats["compared"] = compared

        # 4. Cluster partition + merge — identical to the original loop.
        clusters: list[list[Paper]] = []
        root_index: dict[int, int] = {}
        for i, paper in enumerate(items):
            root = find(i)
            if root not in root_index:
                root_index[root] = len(clusters)
                clusters.append([])
            clusters[root_index[root]].append(paper)

        self._stats["clusters"] = len(clusters)
        result: list[Paper] = []
        for cluster in clusters:
            if len(cluster) == 1:
                result.append(cluster[0])
            else:
                self._stats["merged"] += len(cluster) - 1
                result.append(self._merge_papers(cluster))
        return result

    def deduplicate_authors(self, authors: Sequence[Author]) -> list[Author]:
        """Remove duplicate authors and return merged unique records."""
        self.reset_stats()
        if not authors:
            return []

        remaining = list(authors)
        clusters: list[list[Author]] = []

        while remaining:
            seed = remaining.pop(0)
            cluster = [seed]
            still: list[Author] = []
            seed_name = _normalize_title(seed.name)
            for other in remaining:
                self._stats["compared"] += 1
                other_name = _normalize_title(other.name)
                sim = _jaccard(_token_set(seed.name), _token_set(other.name))
                if seed_name == other_name or sim >= self.config.author_threshold:
                    cluster.append(other)
                else:
                    still.append(other)
            remaining = still
            clusters.append(cluster)

        result: list[Author] = []
        for cluster in clusters:
            if len(cluster) == 1:
                result.append(cluster[0])
            else:
                self._stats["merged"] += len(cluster) - 1
                result.append(self._merge_authors(cluster))
        return result

    def _precompute_paper(self, paper: Paper) -> _PaperMatchData:
        """Extract normalized matching fields from a paper once."""
        return _PaperMatchData(
            doi=paper.doi.lower() if paper.doi else None,
            url=paper.url.rstrip("/").lower() if paper.url else None,
            paper_id=paper.id,
            arxiv_id=_normalize_arxiv_id(paper.arxiv_id),
            pmid=paper.pmid.strip() if paper.pmid else None,
            title_norm=_normalize_title(paper.title),
            title_tokens=_token_set(paper.title),
            author_names={_normalize_title(a.name) for a in paper.authors},
            year=paper.year,
            venue_type=paper.venue_type,
        )

    # ------------------------------------------------------------------
    # Matching rules (3A v2 §6.1)
    # ------------------------------------------------------------------

    def _matches(self, data_a: _PaperMatchData, data_b: _PaperMatchData) -> bool:
        """Any single rule firing merges the two records."""
        if self._exact_match(data_a, data_b):
            return True
        if self._cross_id_match(data_a, data_b):
            return True
        if self._id_conflict(data_a, data_b):
            return False
        if self._seq_title_match(data_a, data_b):
            return True
        return self._fuzzy_match(data_a, data_b) >= self.config.title_threshold

    def _exact_match(self, data_a: _PaperMatchData, data_b: _PaperMatchData) -> bool:
        """Exact match by DOI / arXiv ID / PMID / URL / internal id."""
        if data_a.doi is not None and data_b.doi is not None and data_a.doi == data_b.doi:
            return True
        if data_a.arxiv_id is not None and data_b.arxiv_id is not None and data_a.arxiv_id == data_b.arxiv_id:
            return True
        if data_a.pmid is not None and data_b.pmid is not None and data_a.pmid == data_b.pmid:
            return True
        if data_a.url is not None and data_b.url is not None and data_a.url == data_b.url:
            return True
        return bool(data_a.paper_id and data_b.paper_id and data_a.paper_id == data_b.paper_id)

    def _cross_id_match(self, data_a: _PaperMatchData, data_b: _PaperMatchData) -> bool:
        """arXiv ↔ DOI cross mapping (3A v2 §6.1 rule 2).

        When one record only carries an arXiv ID and the other only carries
        a DOI — the arXiv preprint and the journal version of the same work —
        they are considered the same paper if the normalized titles are
        highly similar (SequenceMatcher >= ``cross_id_title_threshold``).
        """
        a_arxiv = data_a.arxiv_id is not None
        a_doi = data_a.doi is not None
        b_arxiv = data_b.arxiv_id is not None
        b_doi = data_b.doi is not None
        if not ((a_arxiv and b_doi) or (a_doi and b_arxiv)):
            return False
        title_sim = _sequence_ratio(data_a.title_norm, data_b.title_norm)
        return title_sim >= self.config.cross_id_title_threshold

    @staticmethod
    def _id_conflict(data_a: _PaperMatchData, data_b: _PaperMatchData) -> bool:
        """Return True when both records carry the same ID type with
        different values (FIX-L F1).

        ID evidence outranks title similarity: two records that both carry a
        DOI / PMID / arXiv ID and disagree on it are distinct works and must
        not be fused by the title rules. One-sided IDs (only one record has a
        DOI, or one has an arXiv ID and the other a DOI) are not a conflict —
        those are the cross-ID and title-merge scenarios that legitimately
        fuse. Exact ID matches never reach this guard: they merge in
        :meth:`_exact_match` first.
        """
        doi_conflict = (
            data_a.doi is not None
            and data_b.doi is not None
            and data_a.doi != data_b.doi
        )
        pmid_conflict = (
            data_a.pmid is not None
            and data_b.pmid is not None
            and data_a.pmid != data_b.pmid
        )
        arxiv_conflict = (
            data_a.arxiv_id is not None
            and data_b.arxiv_id is not None
            and data_a.arxiv_id != data_b.arxiv_id
        )
        return doi_conflict or pmid_conflict or arxiv_conflict

    def _seq_title_match(self, data_a: _PaperMatchData, data_b: _PaperMatchData) -> bool:
        """SequenceMatcher-based title similarity (3A v2 §6.1 rule 3).

        Title similarity is upgraded from Jaccard to ``difflib.SequenceMatcher``;
        the Jaccard token overlap is retained as an auxiliary guard so that
        near-identical titles that only differ in a numeric suffix (e.g.
        "Unique Paper 0" vs "Unique Paper 20") are not over-merged. The cheap
        Jaccard check runs first so SequenceMatcher (O(n·m)) is only invoked
        for genuinely similar titles.

        I-9 guards: title similarity alone must not fuse distinct works that
        merely share a title. Records with conflicting publication evidence
        (a >1-year gap, or a book-like venue vs a periodical venue) are
        distinct publications and are not merged here — records that really
        are the same work carry a shared DOI / arXiv ID / PMID and merge via
        the ID rules instead.
        """
        jac = _jaccard(data_a.title_tokens, data_b.title_tokens)
        if jac < self.config.seq_jaccard_aux_threshold:
            return False
        seq = _sequence_ratio(data_a.title_norm, data_b.title_norm)
        if seq < self.config.seq_title_threshold:
            return False
        if (
            data_a.year is not None
            and data_b.year is not None
            and abs(data_a.year - data_b.year) > 1
        ):
            return False
        return not self._venue_type_conflict(data_a.venue_type, data_b.venue_type)

    @staticmethod
    def _venue_type_conflict(venue_type_a: str | None, venue_type_b: str | None) -> bool:
        """Return True when the two venue types are fundamentally incompatible.

        A book-like publication and a periodical (journal / conference)
        publication with the same title are different works (I-9); every other
        combination (both periodicals, preprint vs journal, unknown) remains
        eligible for a title-similarity merge.
        """
        va = (venue_type_a or "").strip().lower()
        vb = (venue_type_b or "").strip().lower()
        if not va or not vb:
            return False
        a_book = va in _BOOK_VENUE_TYPES
        b_book = vb in _BOOK_VENUE_TYPES
        a_period = va in _PERIODICAL_VENUE_TYPES
        b_period = vb in _PERIODICAL_VENUE_TYPES
        return (a_book and b_period) or (a_period and b_book)

    def _fuzzy_match(self, data_a: _PaperMatchData, data_b: _PaperMatchData) -> float:
        """Calculate weighted fuzzy similarity score between two papers.

        I-9 guards mirror the SequenceMatcher rule: records whose publication
        evidence conflicts — a >1-year gap or a book-like venue vs a periodical
        venue — are distinct works and never merge through the weighted score.
        The guards return 0 so the score cannot reach the merge threshold even
        with identical titles and authors; records that really are the same
        work merge via the ID rules instead.
        """
        if (
            data_a.year is not None
            and data_b.year is not None
            and abs(data_a.year - data_b.year) > 1
        ):
            return 0.0
        if self._venue_type_conflict(data_a.venue_type, data_b.venue_type):
            return 0.0
        title_sim = _jaccard(data_a.title_tokens, data_b.title_tokens)

        # Author overlap
        author_sim = (
            _jaccard(data_a.author_names, data_b.author_names)
            if data_a.author_names or data_b.author_names
            else 0.5
        )

        # Year closeness
        year_sim = 1.0
        if data_a.year is not None and data_b.year is not None:
            diff = abs(data_a.year - data_b.year)
            if diff > self.config.year_tolerance:
                year_sim = 0.0
            elif diff == 1:
                year_sim = 0.8

        # Weighted combination
        score = 0.6 * title_sim + 0.25 * author_sim + 0.15 * year_sim
        return score

    # ------------------------------------------------------------------
    # Fusion
    # ------------------------------------------------------------------

    @staticmethod
    def _primary_confidence(record: Paper | Author) -> float:
        evidence = record.primary_evidence
        return evidence.confidence if evidence is not None else 0.0

    def _merge_papers(self, cluster: Sequence[Paper]) -> Paper:
        """Merge a cluster of duplicate papers into a single record.

        Field-level conflicts are resolved by picking the value from the
        highest-confidence source; the merged ``evidence_list`` keeps every
        source's evidence and the composite confidence is recomputed with
        the :class:`ConfidenceScorer` (baseline + multi-source bonus).
        """
        # Sort by confidence descending
        ordered = sorted(cluster, key=self._primary_confidence, reverse=True)
        base = ordered[0]
        # I-10: surface same-field multi-source conflicts (never blocks the
        # merge; the highest-confidence value still wins).
        self._warnings.extend(detect_field_conflicts(ordered))

        def pick_str(attr: str) -> str | None:
            for p in ordered:
                val = getattr(p, attr)
                if val:
                    return val  # type: ignore[no-any-return]
            return None

        def pick_int(attr: str) -> int | None:
            best: int | None = None
            best_conf = -1.0
            for p in ordered:
                val = getattr(p, attr)
                if val is not None and self._primary_confidence(p) > best_conf:
                    best = val
                    best_conf = self._primary_confidence(p)
            # For citations prefer max
            if attr == "citations":
                vals = [p.citations for p in ordered if p.citations is not None]
                return max(vals) if vals else None
            return best

        def merge_id_list(attr: str) -> list[str] | None:
            merged: list[str] = []
            for p in ordered:
                for val in getattr(p, attr) or []:
                    if val not in merged:
                        merged.append(val)
            return merged or None

        authors: list[AuthorRef] = []
        seen_authors: set[str] = set()
        for p in ordered:
            for ref in p.authors:
                key = _normalize_title(ref.name)
                if key not in seen_authors:
                    seen_authors.add(key)
                    authors.append(
                        ref.model_copy(update={"position": len(authors) + 1})
                    )

        keywords: list[str] = []
        seen_kw: set[str] = set()
        for p in ordered:
            for k in p.keywords:
                key = k.lower()
                if key not in seen_kw:
                    seen_kw.add(key)
                    keywords.append(k)

        fields_of_study: list[str] = []
        seen_fos: set[str] = set()
        for p in ordered:
            for f in p.fields_of_study:
                key = f.lower()
                if key not in seen_fos:
                    seen_fos.add(key)
                    fields_of_study.append(f)

        evidence_list = self._merge_evidence_lists(
            [p.evidence_list for p in ordered]
        )
        evidence_list = self._cap_evidence_list(evidence_list)
        paper_id = next((p.id for p in ordered if p.id), None)

        merged = Paper(
            id=paper_id,
            title=base.title,
            authors=authors,
            year=pick_int("year"),
            venue=pick_str("venue"),
            venue_type=pick_str("venue_type"),
            abstract=pick_str("abstract"),
            doi=pick_str("doi"),
            arxiv_id=pick_str("arxiv_id"),
            pmid=pick_str("pmid"),
            url=pick_str("url"),
            pdf_url=pick_str("pdf_url"),
            citations=pick_int("citations"),
            reference_count=pick_int("reference_count"),
            keywords=keywords,
            fields_of_study=fields_of_study,
            references=merge_id_list("references"),
            citations_list=merge_id_list("citations_list"),
            evidence_list=evidence_list,
        )
        # Recompute the composite confidence through the same path as the
        # incremental merger: :meth:`ConfidenceScorer.score_paper` applies the
        # multi-source bonus AND the field-level adjustments (DOI +0.05, PDF
        # +0.03, staleness -0.10) so fused records score identically no matter
        # which merge path produced them (I2).
        return self._scorer.score_paper(merged)

    def _merge_authors(self, cluster: Sequence[Author]) -> Author:
        ordered = sorted(cluster, key=self._primary_confidence, reverse=True)
        base = ordered[0]

        def pick_str(attr: str) -> str | None:
            for a in ordered:
                val = getattr(a, attr)
                if val:
                    return val  # type: ignore[no-any-return]
            return None

        def pick_int(attr: str) -> int | None:
            vals = [getattr(a, attr) for a in ordered if getattr(a, attr) is not None]
            if not vals:
                return None
            if attr in ("citations", "h_index"):
                return max(vals)  # type: ignore[no-any-return]
            return vals[0]  # type: ignore[no-any-return]

        interests: list[str] = []
        seen: set[str] = set()
        for a in ordered:
            for i in a.interests:
                key = i.lower()
                if key not in seen:
                    seen.add(key)
                    interests.append(i)

        # Identity fields (3A v2 §6.2): authority IDs keep their non-empty
        # value (highest-confidence record wins, same as pick_str); aliases
        # are the union of every record's aliases plus its non-canonical name
        # variant; disambiguation_status is "confirmed" when any record was
        # confirmed, otherwise "auto" (I4).
        aliases: list[str] = []
        seen_alias: set[str] = set()
        for a in ordered:
            candidates = list(a.aliases)
            if a.name != base.name:
                candidates.append(a.name)
            for alias in candidates:
                key = alias.lower()
                if key not in seen_alias:
                    seen_alias.add(key)
                    aliases.append(alias)

        disambiguation_status = (
            "confirmed"
            if any(a.disambiguation_status == "confirmed" for a in ordered)
            else "auto"
        )

        evidence_list = self._merge_evidence_lists(
            [a.evidence_list for a in ordered]
        )
        evidence_list = self._cap_evidence_list(evidence_list)
        merged = Author(
            id=next((a.id for a in ordered if a.id), None),
            name=base.name,
            orcid=pick_str("orcid"),
            semantic_scholar_id=pick_str("semantic_scholar_id"),
            openalex_id=pick_str("openalex_id"),
            aliases=aliases,
            disambiguation_status=disambiguation_status,
            affiliation=pick_str("affiliation"),
            email=pick_str("email"),
            homepage=pick_str("homepage"),
            h_index=pick_int("h_index"),
            citations=pick_int("citations"),
            interests=interests,
            profile_url=pick_str("profile_url"),
            evidence_list=evidence_list,
        )
        # Same scoring path as the incremental merger (I2): multi-source
        # baseline plus staleness adjustment for authors.
        return self._scorer.score_author(merged)

    @staticmethod
    def _merge_evidence_lists(
        evidence_lists: Sequence[Sequence[Evidence]],
    ) -> list[Evidence]:
        """Union evidences across records, de-duplicating by (source, source_id).

        Every source's evidence is preserved so the merged ``evidence_list``
        contains one entry per confirming source.
        """
        seen: set[tuple[str, str | None]] = set()
        result: list[Evidence] = []
        for evidences in evidence_lists:
            for ev in evidences:
                key = (ev.source.value, ev.source_id)
                if key not in seen:
                    seen.add(key)
                    result.append(ev)
        return result

    def _cap_evidence_list(self, evidence_list: list[Evidence]) -> list[Evidence]:
        """Cap a merged evidence list at the model's ``max_length`` (500).

        A pathological mega-cluster (e.g. hundreds of records fused by a
        shared DOI) produces more evidence entries than ``Paper.evidence_list``
        / ``Author.evidence_list`` accept; without this cap the merge would
        raise a bare Pydantic ValidationError and take the whole collect
        pipeline down (FIX-L F2). Keep the 500 highest-confidence entries
        (stable sort breaks ties by original order) and record the truncation
        in stats/warnings so it stays observable.
        """
        if len(evidence_list) <= EVIDENCE_LIST_MAX:
            return evidence_list
        capped = sorted(
            evidence_list, key=lambda e: e.confidence, reverse=True
        )[:EVIDENCE_LIST_MAX]
        self._stats["evidence_truncated"] += 1
        self._warnings.append(
            f"evidence truncated: {len(evidence_list)} -> {EVIDENCE_LIST_MAX} "
            "(mega-cluster exceeds evidence_list cap; kept highest confidence)"
        )
        return capped

    def get_stats(self) -> dict[str, int]:
        """Return deduplication statistics."""
        return self._stats.copy()

    def reset_stats(self) -> None:
        """Reset deduplication statistics."""
        self._stats = {
            "compared": 0,
            "merged": 0,
            "clusters": 0,
            "evidence_truncated": 0,
        }

    def get_warnings(self) -> list[str]:
        """Return the field-conflict warnings from the last paper merge."""
        return list(self._warnings)

    def pop_warnings(self) -> list[str]:
        """Return and clear the accumulated field-conflict warnings."""
        warnings = self._warnings
        self._warnings = []
        return warnings
