"""Deduplication and fusion processor for academic data.

Merges duplicate records across multiple sources using similarity matching
and confidence-weighted field resolution.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from academic_intelligence.core.models import Author, Evidence, Paper


def _normalize_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


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


class SimilarityConfig:
    """Configuration for similarity thresholds and weights."""

    def __init__(
        self,
        title_threshold: float = 0.85,
        author_threshold: float = 0.80,
        year_tolerance: int = 1,
        venue_threshold: float = 0.75,
    ) -> None:
        self.title_threshold = title_threshold
        self.author_threshold = author_threshold
        self.year_tolerance = year_tolerance
        self.venue_threshold = venue_threshold


class Deduplicator:
    """Merges duplicate academic records across multiple sources."""

    def __init__(self, config: SimilarityConfig | None = None) -> None:
        self.config = config or SimilarityConfig()
        self._stats: Dict[str, int] = {"compared": 0, "merged": 0, "clusters": 0}

    def deduplicate_papers(self, papers: Sequence[Paper]) -> List[Paper]:
        """Remove duplicate papers and return merged unique records."""
        if not papers:
            return []

        remaining = list(papers)
        clusters: List[List[Paper]] = []

        while remaining:
            seed = remaining.pop(0)
            cluster = [seed]
            still: List[Paper] = []
            for other in remaining:
                self._stats["compared"] += 1
                if self._exact_match(seed, other) or self._fuzzy_match(seed, other) >= self.config.title_threshold:
                    cluster.append(other)
                else:
                    still.append(other)
            remaining = still
            clusters.append(cluster)

        self._stats["clusters"] = len(clusters)
        result: List[Paper] = []
        for cluster in clusters:
            if len(cluster) == 1:
                result.append(cluster[0])
            else:
                self._stats["merged"] += len(cluster) - 1
                result.append(self._merge_papers(cluster))
        return result

    def deduplicate_authors(self, authors: Sequence[Author]) -> List[Author]:
        """Remove duplicate authors and return merged unique records."""
        if not authors:
            return []

        remaining = list(authors)
        clusters: List[List[Author]] = []

        while remaining:
            seed = remaining.pop(0)
            cluster = [seed]
            still: List[Author] = []
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

        result: List[Author] = []
        for cluster in clusters:
            if len(cluster) == 1:
                result.append(cluster[0])
            else:
                self._stats["merged"] += len(cluster) - 1
                result.append(self._merge_authors(cluster))
        return result

    def _exact_match(self, paper_a: Paper, paper_b: Paper) -> bool:
        """Check if two papers match exactly by DOI or URL."""
        if paper_a.doi and paper_b.doi:
            if paper_a.doi.lower() == paper_b.doi.lower():
                return True
        if paper_a.url and paper_b.url:
            if paper_a.url.rstrip("/").lower() == paper_b.url.rstrip("/").lower():
                return True
        if paper_a.id and paper_b.id and paper_a.id == paper_b.id:
            return True
        return False

    def _fuzzy_match(self, paper_a: Paper, paper_b: Paper) -> float:
        """Calculate fuzzy similarity score between two papers."""
        title_sim = _jaccard(_token_set(paper_a.title), _token_set(paper_b.title))

        # Author overlap
        authors_a = {_normalize_title(a) for a in paper_a.authors}
        authors_b = {_normalize_title(a) for a in paper_b.authors}
        author_sim = _jaccard(authors_a, authors_b) if authors_a or authors_b else 0.5

        # Year closeness
        year_sim = 1.0
        if paper_a.year is not None and paper_b.year is not None:
            diff = abs(paper_a.year - paper_b.year)
            if diff > self.config.year_tolerance:
                year_sim = 0.0
            elif diff == 1:
                year_sim = 0.8

        # Weighted combination
        score = 0.6 * title_sim + 0.25 * author_sim + 0.15 * year_sim
        return score

    def _merge_papers(self, cluster: Sequence[Paper]) -> Paper:
        """Merge a cluster of duplicate papers into a single record."""
        # Sort by confidence descending
        ordered = sorted(cluster, key=lambda p: p.evidence.confidence, reverse=True)
        base = ordered[0]

        def pick_str(attr: str) -> Optional[str]:
            for p in ordered:
                val = getattr(p, attr)
                if val:
                    return val  # type: ignore[no-any-return]
            return None

        def pick_int(attr: str) -> Optional[int]:
            best: Optional[int] = None
            best_conf = -1.0
            for p in ordered:
                val = getattr(p, attr)
                if val is not None and p.evidence.confidence > best_conf:
                    best = val
                    best_conf = p.evidence.confidence
            # For citations prefer max
            if attr == "citations":
                vals = [p.citations for p in ordered if p.citations is not None]
                return max(vals) if vals else None
            return best

        authors: List[str] = []
        seen_authors: set[str] = set()
        for p in ordered:
            for a in p.authors:
                key = _normalize_title(a)
                if key not in seen_authors:
                    seen_authors.add(key)
                    authors.append(a)

        keywords: List[str] = []
        seen_kw: set[str] = set()
        for p in ordered:
            for k in p.keywords:
                key = k.lower()
                if key not in seen_kw:
                    seen_kw.add(key)
                    keywords.append(k)

        evidence = self._merge_evidence([p.evidence for p in ordered])
        paper_id = next((p.id for p in ordered if p.id), None)

        return Paper(
            id=paper_id,
            title=base.title,
            authors=authors,
            year=pick_int("year"),
            venue=pick_str("venue"),
            abstract=pick_str("abstract"),
            doi=pick_str("doi"),
            url=pick_str("url"),
            pdf_url=pick_str("pdf_url"),
            citations=pick_int("citations"),
            keywords=keywords,
            evidence=evidence,
        )

    def _merge_authors(self, cluster: Sequence[Author]) -> Author:
        ordered = sorted(cluster, key=lambda a: a.evidence.confidence, reverse=True)
        base = ordered[0]

        def pick_str(attr: str) -> Optional[str]:
            for a in ordered:
                val = getattr(a, attr)
                if val:
                    return val  # type: ignore[no-any-return]
            return None

        def pick_int(attr: str) -> Optional[int]:
            vals = [getattr(a, attr) for a in ordered if getattr(a, attr) is not None]
            if not vals:
                return None
            if attr in ("citations", "h_index"):
                return max(vals)  # type: ignore[no-any-return]
            return vals[0]  # type: ignore[no-any-return]

        interests: List[str] = []
        seen: set[str] = set()
        for a in ordered:
            for i in a.interests:
                key = i.lower()
                if key not in seen:
                    seen.add(key)
                    interests.append(i)

        return Author(
            id=next((a.id for a in ordered if a.id), None),
            name=base.name,
            affiliation=pick_str("affiliation"),
            email=pick_str("email"),
            homepage=pick_str("homepage"),
            h_index=pick_int("h_index"),
            citations=pick_int("citations"),
            interests=interests,
            profile_url=pick_str("profile_url"),
            evidence=self._merge_evidence([a.evidence for a in ordered]),
        )

    def _merge_evidence(self, evidences: Sequence[Evidence]) -> Evidence:
        """Merge multiple evidence records into a single composite evidence."""
        if not evidences:
            raise ValueError("cannot merge empty evidence list")
        if len(evidences) == 1:
            return evidences[0]

        ordered = sorted(evidences, key=lambda e: e.confidence, reverse=True)
        primary = ordered[0]
        avg_conf = sum(e.confidence for e in ordered) / len(ordered)
        # Boost slightly for multi-source agreement
        conf = min(1.0, avg_conf + 0.05 * (len(ordered) - 1))

        sources = [e.source.value for e in ordered]
        raw: Dict[str, Any] = {
            "merged_from": sources,
            "source_urls": [e.source_url for e in ordered],
            "individual": [e.model_dump(mode="json") for e in ordered],
        }
        return Evidence(
            source=primary.source,
            source_url=primary.source_url,
            collected_at=max(e.collected_at for e in ordered),
            confidence=conf,
            raw_data=raw,
        )

    def get_stats(self) -> Dict[str, int]:
        """Return deduplication statistics."""
        return self._stats.copy()

    def reset_stats(self) -> None:
        """Reset deduplication statistics."""
        self._stats = {"compared": 0, "merged": 0, "clusters": 0}
