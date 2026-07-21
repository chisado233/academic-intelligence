"""Incremental update processor for academic paper records.

Detects new / updated / unchanged papers between a fresh collection and
existing storage, then applies only necessary writes with confidence-weighted
field merging.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

from academic_intelligence.core.models import (
    ChangeDetection,
    ChangeType,
    Evidence,
    IncrementalUpdateResult,
    Paper,
)
from academic_intelligence.storage.base import BaseStorage

# Fields compared during field-level change detection
CHANGED_FIELDS: List[str] = [
    "title",
    "authors",
    "year",
    "venue",
    "abstract",
    "doi",
    "url",
    "pdf_url",
    "citations",
    "keywords",
]


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


def _author_key(name: str) -> str:
    """Normalize author name for matching (drop initials, keep surnames)."""
    parts = [p for p in _normalize_title(name).split() if p and len(p) > 1]
    return " ".join(parts) if parts else _normalize_title(name)


class IncrementalProcessor:
    """Incremental update processor.

    Compares newly collected papers against stored records, classifies each
    as new / updated / unchanged, and applies only necessary storage writes.
    """

    CHANGED_FIELDS = CHANGED_FIELDS

    def __init__(self, storage: BaseStorage) -> None:
        self.storage = storage

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def detect_changes(
        self,
        new_papers: list[Paper],
        old_papers: list[Paper],
    ) -> IncrementalUpdateResult:
        """Detect changes between newly collected and previously stored papers.

        Matching priority: id → doi → url → content hash → fuzzy title/authors.
        """
        old_by_id: Dict[str, Paper] = {}
        old_by_doi: Dict[str, Paper] = {}
        old_by_url: Dict[str, Paper] = {}
        old_by_hash: Dict[str, Paper] = {}
        old_remaining: List[Paper] = []

        for p in old_papers:
            if p.id:
                old_by_id[p.id] = p
            if p.doi:
                old_by_doi[p.doi.lower()] = p
            if p.url:
                old_by_url[p.url.rstrip("/").lower()] = p
            h = self._calculate_hash(p)
            old_by_hash[h] = p
            old_remaining.append(p)

        matched_old_ids: set[str] = set()
        new_list: List[Paper] = []
        updated: List[ChangeDetection] = []
        unchanged: List[str] = []
        sources: set[str] = set()

        for new in new_papers:
            sources.add(new.evidence.source.value)
            old = self._find_match(
                new,
                old_by_id=old_by_id,
                old_by_doi=old_by_doi,
                old_by_url=old_by_url,
                old_by_hash=old_by_hash,
                old_remaining=old_remaining,
                matched_old_ids=matched_old_ids,
            )
            if old is None:
                new_list.append(new)
                continue

            old_key = old.id or id(old)
            matched_old_ids.add(str(old_key))

            # Fast path: content hash equal → treat as unchanged unless other
            # tracked fields differ (hash only covers title/authors/year/venue).
            old_hash = self._calculate_hash(old)
            new_hash = self._calculate_hash(new)
            if old_hash == new_hash:
                detection = self._compare_papers(old, new)
                if detection.change_type == ChangeType.UNCHANGED:
                    pid = detection.paper_id
                    unchanged.append(pid)
                    continue
                # Secondary fields (abstract, citations, …) changed
                detection.old_paper = old
                detection.new_paper = new
                updated.append(detection)
                continue

            detection = self._compare_papers(old, new)
            detection.old_paper = old
            detection.new_paper = new
            if detection.change_type == ChangeType.UNCHANGED:
                unchanged.append(detection.paper_id)
            else:
                updated.append(detection)

        return IncrementalUpdateResult(
            new=new_list,
            updated=updated,
            unchanged=unchanged,
            total_checked=len(new_papers),
            sources_used=sorted(sources),
        )

    async def apply_changes(
        self,
        result: IncrementalUpdateResult,
    ) -> dict[str, int]:
        """Apply detected changes to storage.

        - New papers are inserted.
        - Updated papers are confidence-merged then written.
        - Unchanged papers are skipped (no write).

        Returns:
            Counts: ``{"new": n, "updated": n, "unchanged": n, "skipped": n}``.
        """
        counts = {
            "new": 0,
            "updated": 0,
            "unchanged": len(result.unchanged),
            "skipped": 0,
        }

        for paper in result.new:
            paper_id = await self.storage.save_paper(paper)
            content_hash = self._calculate_hash(
                paper.model_copy(update={"id": paper_id})
            )
            await self.storage.save_paper_hash(paper_id, content_hash)
            counts["new"] += 1

        for change in result.updated:
            if change.change_type != ChangeType.UPDATED:
                counts["skipped"] += 1
                continue
            old = change.old_paper
            new = change.new_paper
            if old is None or new is None:
                # Reconstruct minimal merge from field deltas when payloads missing
                existing = await self.storage.get_paper(change.paper_id)
                if existing is None:
                    counts["skipped"] += 1
                    continue
                merged = self._apply_field_deltas(existing, change)
            else:
                merged = self._merge_papers_confidence(old, new)

            paper_id = (
                change.paper_id
                or merged.id
                or (old.id if old is not None else None)
            )
            if not paper_id:
                paper_id = await self.storage.save_paper(merged)
            else:
                merged = merged.model_copy(update={"id": paper_id})
                updated_ok = await self.storage.update_paper(paper_id, merged)
                if not updated_ok:
                    # Fall back to save (upsert semantics)
                    paper_id = await self.storage.save_paper(merged)

            content_hash = self._calculate_hash(merged)
            await self.storage.save_paper_hash(paper_id, content_hash)
            counts["updated"] += 1

        return counts

    # ------------------------------------------------------------------
    # Comparison helpers
    # ------------------------------------------------------------------

    def _compare_papers(self, old: Paper, new: Paper) -> ChangeDetection:
        """Compare two papers field-by-field and return a ChangeDetection."""
        changed_fields: List[str] = []
        old_values: Dict[str, Any] = {}
        new_values: Dict[str, Any] = {}

        for field in self.CHANGED_FIELDS:
            old_val = getattr(old, field)
            new_val = getattr(new, field)
            if not self._values_equal(old_val, new_val):
                changed_fields.append(field)
                old_values[field] = old_val
                new_values[field] = new_val

        paper_id = new.id or old.id or ""
        conf_delta = float(new.evidence.confidence) - float(old.evidence.confidence)

        return ChangeDetection(
            paper_id=paper_id,
            change_type=(
                ChangeType.UPDATED if changed_fields else ChangeType.UNCHANGED
            ),
            changed_fields=changed_fields,
            old_values=old_values,
            new_values=new_values,
            confidence_delta=conf_delta,
            old_paper=old,
            new_paper=new,
        )

    def _calculate_hash(self, paper: Paper) -> str:
        """Compute a short SHA-256 content hash (title|authors|year|venue)."""
        authors = "|".join(paper.authors or [])
        year = "" if paper.year is None else str(paper.year)
        venue = paper.venue or ""
        content = f"{paper.title}|{authors}|{year}|{venue}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _find_match(
        self,
        new: Paper,
        *,
        old_by_id: Dict[str, Paper],
        old_by_doi: Dict[str, Paper],
        old_by_url: Dict[str, Paper],
        old_by_hash: Dict[str, Paper],
        old_remaining: List[Paper],
        matched_old_ids: set[str],
    ) -> Optional[Paper]:
        def _accept(candidate: Optional[Paper]) -> Optional[Paper]:
            if candidate is None:
                return None
            key = str(candidate.id or id(candidate))
            if key in matched_old_ids:
                return None
            return candidate

        if new.id:
            hit = _accept(old_by_id.get(new.id))
            if hit is not None:
                return hit

        if new.doi:
            hit = _accept(old_by_doi.get(new.doi.lower()))
            if hit is not None:
                return hit

        if new.url:
            hit = _accept(old_by_url.get(new.url.rstrip("/").lower()))
            if hit is not None:
                return hit

        h = self._calculate_hash(new)
        hit = _accept(old_by_hash.get(h))
        if hit is not None:
            return hit

        # Fuzzy title + author fallback
        best: Optional[Paper] = None
        best_score = 0.0
        new_tokens = _token_set(new.title)
        new_authors = {_author_key(a) for a in new.authors}
        for old in old_remaining:
            key = str(old.id or id(old))
            if key in matched_old_ids:
                continue
            title_sim = _jaccard(new_tokens, _token_set(old.title))
            if title_sim < 0.80:
                continue
            old_authors = {_author_key(a) for a in old.authors}
            author_sim = (
                _jaccard(new_authors, old_authors)
                if new_authors or old_authors
                else 0.5
            )
            year_ok = True
            if new.year is not None and old.year is not None:
                year_ok = abs(new.year - old.year) <= 1
            if not year_ok:
                continue
            # Near-identical titles can match even with weak author overlap
            # (different sources often format names differently).
            if title_sim >= 0.95:
                score = 0.9 * title_sim + 0.1 * author_sim
                threshold = 0.85
            else:
                score = 0.7 * title_sim + 0.3 * author_sim
                threshold = 0.85
            if score > best_score and score >= threshold:
                best_score = score
                best = old
        return best

    # ------------------------------------------------------------------
    # Merge strategies
    # ------------------------------------------------------------------

    def _merge_papers_confidence(self, old: Paper, new: Paper) -> Paper:
        """Merge two paper versions, preferring higher-confidence field values.

        Multi-source conflict resolution: for each field, take the non-empty
        value from the paper with higher evidence.confidence. Citations keep
        the maximum observed count. Authors/keywords are unioned.
        """
        if new.evidence.confidence >= old.evidence.confidence:
            primary, secondary = new, old
        else:
            primary, secondary = old, new

        def pick(field: str) -> Any:
            p_val = getattr(primary, field)
            s_val = getattr(secondary, field)
            if p_val is not None and p_val != "" and p_val != []:
                return p_val
            return s_val

        # Authors: union preserving order (primary first)
        authors: List[str] = []
        seen_a: set[str] = set()
        for a in list(primary.authors) + list(secondary.authors):
            key = _normalize_title(a)
            if key not in seen_a:
                seen_a.add(key)
                authors.append(a)

        keywords: List[str] = []
        seen_k: set[str] = set()
        for k in list(primary.keywords) + list(secondary.keywords):
            key = k.lower()
            if key not in seen_k:
                seen_k.add(key)
                keywords.append(k)

        citations: Optional[int] = None
        cands = [c for c in (old.citations, new.citations) if c is not None]
        if cands:
            citations = max(cands)

        evidence = self._merge_evidence(old.evidence, new.evidence)
        paper_id = old.id or new.id

        return Paper(
            id=paper_id,
            title=pick("title") or primary.title,
            authors=authors,
            year=pick("year"),
            venue=pick("venue"),
            abstract=pick("abstract"),
            doi=pick("doi"),
            url=pick("url"),
            pdf_url=pick("pdf_url"),
            citations=citations,
            keywords=keywords,
            evidence=evidence,
        )

    def _apply_field_deltas(
        self,
        existing: Paper,
        change: ChangeDetection,
    ) -> Paper:
        """Apply new_values from a ChangeDetection onto an existing paper."""
        updates: Dict[str, Any] = {}
        for field, new_val in change.new_values.items():
            if field in self.CHANGED_FIELDS:
                updates[field] = new_val
        if change.new_paper is not None:
            # Prefer full confidence merge when available
            return self._merge_papers_confidence(existing, change.new_paper)
        return existing.model_copy(update=updates)

    def _merge_evidence(self, a: Evidence, b: Evidence) -> Evidence:
        """Keep the higher-confidence evidence, boost slightly for multi-source."""
        primary, secondary = (a, b) if a.confidence >= b.confidence else (b, a)
        conf = min(1.0, (a.confidence + b.confidence) / 2.0 + 0.05)
        raw: Dict[str, Any] = {
            "merged_from": [a.source.value, b.source.value],
            "source_urls": [a.source_url, b.source_url],
            "individual": [
                a.model_dump(mode="json"),
                b.model_dump(mode="json"),
            ],
        }
        return Evidence(
            source=primary.source,
            source_url=primary.source_url,
            collected_at=max(a.collected_at, b.collected_at),
            confidence=conf,
            raw_data=raw,
        )

    @staticmethod
    def _values_equal(a: Any, b: Any) -> bool:
        """Equality that treats list order-insensitively for authors/keywords."""
        if isinstance(a, list) and isinstance(b, list):
            if all(isinstance(x, str) for x in a) and all(
                isinstance(x, str) for x in b
            ):
                return [x.lower().strip() for x in a] == [
                    x.lower().strip() for x in b
                ]
            return a == b
        if isinstance(a, str) and isinstance(b, str):
            return a.strip() == b.strip()
        return a == b
