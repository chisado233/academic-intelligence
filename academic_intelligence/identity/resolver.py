"""WP6 author identity resolver (upgrade technical-design.md §1.5 / §9).

``Resolver`` turns "who is this byline name in this paper?" into a
verifiable identity answer:

- :meth:`resolve` — locate the byline ``AuthorRef`` in a stored paper,
  then (1) return a confirmed ``author_identity_global`` row directly
  (I8 cross-paper reuse), (2) branch A: fetch the full source profile
  when the byline carries an authority id (OpenAlex / S2 / ORCID), or
  (3) branch B: score local-library + source candidates with the existing
  :class:`~academic_intelligence.processors.disambiguator.AuthorDisambiguator`
  (``>= 0.85`` 判同 / ``0.60 .. 0.85`` ambiguous, candidates listed but
  never hard-merged (D3) / ``< 0.60`` 不同人);
- :meth:`profile` — the full profile for one authority id (institution /
  h-index / homepage / interests / representative papers by citations);
- :meth:`search` — same-name candidates with optional disambiguation
  ranking (Q7 / D3);
- :meth:`confirm` — write a confirmed identity back to
  ``author_identity_global`` + the paper-level ``author_identity`` link;
  the next ``resolve`` of the same name hits it directly.

All source traffic flows through :class:`SourceFetcher` (polite HTTP
client); failures are fail-soft (logged) where the design allows a
fallback, and surface as :class:`IdentitySourceError` where a caller must
know the fetch failed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from academic_intelligence.core.models import Author, AuthorRef, Paper
from academic_intelligence.identity.exceptions import (
    AuthorNotFoundError,
    PaperNotFoundError,
)
from academic_intelligence.identity.fetcher import SourceFetcher, WorksContext
from academic_intelligence.identity.models import (
    AUTHOR_ID_SOURCES,
    AuthorCandidate,
    AuthorProfile,
    ConfirmResult,
    ResolveResult,
    evidence_entry,
)
from academic_intelligence.processors.disambiguator import (
    AuthorDisambiguator,
    name_similarity,
)
from academic_intelligence.utils.names import author_name_matches

logger = logging.getLogger(__name__)

# OpenAlex author id: ``A`` + 1..20 digits (``A5108093963``).
_OPENALEX_ID_RE = re.compile(r"^A\d{1,20}$")
# Semantic Scholar author id: 1..12 digits.
_S2_ID_RE = re.compile(r"^\d{1,12}$")
# ORCID iD: 4-4-4-4 digits, final char may be a check digit X.
_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[0-9X]$", re.IGNORECASE)

#: How many top candidates get their works fetched for coauthor / year /
#: venue context during disambiguation (polite cap — the works endpoints
#: are the expensive part of branch B).  Exact-name candidates are
#: prioritized within the budget (see :meth:`Resolver._enrich_top`).
_CONTEXT_ENRICH_TOP = 8


def _normalize_arxiv(value: str | None) -> str:
    """Normalize an arXiv id to the bare ``YYYY.NNNNN`` form (version-free)."""
    if not value:
        return ""
    cleaned = value.strip().lower()
    if cleaned.startswith("arxiv:"):
        cleaned = cleaned[len("arxiv:") :].strip()
    cleaned = re.sub(r"v\d+$", "", cleaned)
    return cleaned


def _normalize_title(value: str) -> str:
    """Normalize a title for comparison (lowercase, punctuation stripped)."""
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff ]", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", normalized)


def _classify_authority(author_id: str | None) -> tuple[str, str] | None:
    """Classify an ``AuthorRef.author_id`` into ``(source, id)``.

    ``AuthorRef.author_id`` may hold an OpenAlex id (``A123``), a Semantic
    Scholar id (``12345``), an ORCID, or the storage-internal uuid/pseudo
    key (``~name``).  Only real authority ids return a ``(source, id)``
    pair; everything else is ``None`` (→ branch B disambiguation).
    """
    if not author_id:
        return None
    value = author_id.strip()
    if value.startswith("https://openalex.org/"):
        return ("openalex", value.rstrip("/").rsplit("/", 1)[-1])
    if _OPENALEX_ID_RE.fullmatch(value):
        return ("openalex", value)
    if _ORCID_RE.fullmatch(value):
        return ("orcid", value.upper())
    if _S2_ID_RE.fullmatch(value):
        return ("s2", value)
    return None


def parse_candidate_id(candidate_id: str) -> tuple[str, str]:
    """Parse a source-qualified candidate id into ``(source, author_id)``.

    Accepts ``openalex:<id>`` / ``s2:<id>`` / ``orcid:<id>``, a full
    OpenAlex URL (``https://openalex.org/A123``) and a bare OpenAlex
    ``A...`` id.

    Raises:
        ValueError: When the id matches no supported form (the CLI reports
            it as a usage error, exit 2).
    """
    value = candidate_id.strip()
    if value.startswith("https://openalex.org/"):
        return "openalex", value.rstrip("/").rsplit("/", 1)[-1]
    if _OPENALEX_ID_RE.fullmatch(value):
        return "openalex", value
    if ":" in value:
        prefix, _, rest = value.partition(":")
        if prefix in AUTHOR_ID_SOURCES and rest:
            return prefix, rest
        raise ValueError(
            f"无法识别的候选 ID {candidate_id!r}：未知来源 {prefix!r}；"
            f"支持 {', '.join(AUTHOR_ID_SOURCES)} 来源"
        )
    raise ValueError(
        f"无法识别的候选 ID {candidate_id!r}；支持 openalex:<id> / s2:<id> / "
        "orcid:<id> / OpenAlex URL（如 https://openalex.org/A123）"
    )


class Resolver:
    """Author identity resolution service (WP6)."""

    def __init__(
        self,
        storage: Any,
        *,
        fetcher: SourceFetcher | None = None,
        disambiguator: AuthorDisambiguator | None = None,
        http_client: Any | None = None,
        openalex_email: str | None = None,
        s2_api_key: str | None = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            storage: The active storage backend (duck-typed; the SQLite
                backend implements the identity tables).
            fetcher: Optional custom :class:`SourceFetcher` (tests inject a
                fake).  When omitted, a real fetcher is created and owned by
                the resolver (``close()`` releases it).
            disambiguator: Optional custom disambiguator (defaults to
                :class:`AuthorDisambiguator` with the design thresholds
                0.85 / 0.60).
            http_client: Optional shared ``HTTPClient`` for the default
                fetcher (caller owns its lifecycle).
            openalex_email: OpenAlex polite-pool mailto for the default
                fetcher.
            s2_api_key: Semantic Scholar API key for the default fetcher.
        """
        self._storage = storage
        self._fetcher = fetcher or SourceFetcher(
            http_client=http_client,
            email=openalex_email,
            api_key=s2_api_key,
        )
        self._disambiguator = disambiguator or AuthorDisambiguator()
        self._owns_fetcher = fetcher is None

    async def __aenter__(self) -> Resolver:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_fetcher:
            await self._fetcher.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(self, paper_id: str, name: str) -> ResolveResult:
        """Resolve the identity of *name* inside the stored *paper_id*.

        Flow (technical-design.md §1.5): locate the byline ref → confirmed
        global identity short-circuit (I8) → branch A authority-id profile →
        branch B disambiguated candidate comparison.  Never merges
        candidates itself (D3); confirmation is a separate
        :meth:`confirm` call.

        Raises:
            PaperNotFoundError: *paper_id* is not in storage.
            AuthorNotFoundError: *name* is not in the paper's byline.
        """
        paper = await self._load_paper(paper_id)
        if paper is None:
            raise PaperNotFoundError(
                f"未找到论文 {paper_id!r}（请先采集入库，如 "
                "`paper source arxiv get <id> --persist`）"
            )
        ref = self._locate_author_ref(paper, name)
        if ref is None:
            byline = ", ".join(a.name for a in paper.authors[:10]) or "(空)"
            raise AuthorNotFoundError(
                f"论文 {paper_id!r} 的作者列表中未找到 {name!r}（作者：{byline}）"
            )
        byline_name = ref.name

        # I8: a confirmed global identity for this byline name returns
        # directly (cross-paper reuse) — no source fetch needed.
        global_rows = await self._storage.get_author_identities_for_name(
            byline_name
        )
        confirmed = [r for r in global_rows if r.get("status") == "confirmed"]
        if len(confirmed) == 1:
            row = confirmed[0]
            profile = await self._safe_profile(row["author_id"], row["source"])
            return ResolveResult(
                paper_id=paper_id,
                author_name=byline_name,
                match="confirmed",
                profile=profile,
                evidence_chain=self._identity_evidence(byline_name, row),
                message=(
                    f"命中已确认身份 {row['source']}:{row['author_id']} "
                    f"（confirmed_by={row.get('confirmed_by') or '?'}），"
                    "跨论文复用（I8）"
                ),
            )
        if len(confirmed) > 1:
            candidates = [self._candidate_from_identity(r) for r in confirmed]
            return ResolveResult(
                paper_id=paper_id,
                author_name=byline_name,
                match="ambiguous",
                candidates=candidates,
                evidence_chain=self._identity_evidence(byline_name, confirmed[0]),
                message=(
                    "该名字存在多个已确认身份（同名不同人），请用 "
                    "`paper author search \"<name>\" --disambiguate` 查看 "
                    "候选对比表后确认"
                ),
            )

        # Branch A: the byline carries an authority id → direct source profile.
        authority = _classify_authority(ref.author_id)
        if authority is not None:
            source, author_id = authority
            profile = await self._safe_profile(author_id, source)
            if profile is not None:
                return ResolveResult(
                    paper_id=paper_id,
                    author_name=byline_name,
                    match="id_linked",
                    profile=profile,
                    evidence_chain=self._profile_evidence(profile),
                    message=f"AuthorRef 携带 {source} 权威 ID，直连源档案",
                )
            logger.warning(
                "authority id %s:%s returned no profile, falling back to "
                "name disambiguation",
                source,
                author_id,
            )

        # Branch B: disambiguate local-library + source candidates.
        candidates = await self._search_candidates(byline_name)
        if not candidates:
            return ResolveResult(
                paper_id=paper_id,
                author_name=byline_name,
                match="not_found",
                evidence_chain=[],
                message=(
                    "源搜索未返回任何同名候选（源不可用或该名字无记录），"
                    "无法给出身份结论"
                ),
            )
        query_author = self._query_author(paper, ref)
        await self._enrich_top(candidates, byline_name, paper, _CONTEXT_ENRICH_TOP)
        scored = self._score_candidates(query_author, candidates)
        top = scored[0]
        top_score = top.score if top.score is not None else 0.0
        if top_score >= self._disambiguator.config.auto_merge_threshold:
            match = "auto"
            if top.paper_match:
                message = (
                    f"判同：候选 {top.candidate_id} 的著作列表包含本论文"
                    "（源作者归属直接匹配，作者身份级证据）。可执行 "
                    f"`paper author confirm {top.candidate_id} --for {paper_id} "
                    f'--name "{byline_name}"` 写回确认'
                )
            else:
                message = (
                    f"自动判同：最佳候选 {top.candidate_id} 综合分 "
                    f"{top_score:.2f} ≥ {self._disambiguator.config.auto_merge_threshold}"
                    "（机构/方向/合著者一致）。可执行 "
                    f"`paper author confirm {top.candidate_id} --for {paper_id} "
                    f'--name "{byline_name}"` 写回确认'
                )
        elif top_score >= self._disambiguator.config.ambiguous_threshold:
            match = "ambiguous"
            message = (
                f"待确认：最佳候选综合分 {top_score:.2f} 处于 "
                f"{self._disambiguator.config.ambiguous_threshold}–"
                f"{self._disambiguator.config.auto_merge_threshold} "
                "（ambiguous），未硬合并（D3）；请用 "
                "`paper author search --disambiguate` 查看候选对比表后确认"
            )
        else:
            match = "different"
            message = (
                f"最佳候选综合分 {top_score:.2f} < "
                f"{self._disambiguator.config.ambiguous_threshold}：判为不同人"
                "（该论文作者可能不在源库中）"
            )
        return ResolveResult(
            paper_id=paper_id,
            author_name=byline_name,
            match=match,
            candidates=scored,
            evidence_chain=[self._candidate_evidence(c) for c in scored],
            message=message,
        )

    async def profile(
        self,
        author_id: str,
        source: str = "openalex",
    ) -> AuthorProfile:
        """Fetch the complete profile for one authority id (Q3).

        ``representative_papers`` are sorted by citation count desc.

        Raises:
            ValueError: For an unsupported *source*.
            AuthorNotFoundError: When the source has no such author (404).
            IdentitySourceError: On a genuine source failure (rate limit /
                network), so it is never misreported as "not found".
        """
        if source == "orcid":
            profile = await self._fetcher.fetch_by_orcid(author_id)
        elif source in ("openalex", "s2"):
            profile = await self._fetcher.fetch_profile(author_id, source)
        else:
            raise ValueError(f"不支持的作者来源 {source!r}；支持 openalex / s2 / orcid")
        if profile is None:
            raise AuthorNotFoundError(f"未找到 {source} 作者 {author_id!r}")
        return profile

    async def search(
        self,
        name: str,
        *,
        disambiguate: bool = True,
        limit: int = 10,
    ) -> list[AuthorCandidate]:
        """Search same-name candidates, optionally with disambiguation order.

        With ``disambiguate=True`` each candidate carries the composite
        score against the queried name (Q7): name-match weight applied,
        non-name features neutral (the query has no paper context), and the
        result is ordered by score then prominence (citations / h-index).

        Raises:
            ValueError: For a non-positive *limit*.
        """
        if limit <= 0:
            raise ValueError("limit must be >= 1")
        candidates = await self._search_candidates(
            name, limit=min(max(limit * 3, 25), 50)
        )
        if not disambiguate:
            return candidates[:limit]
        await self._enrich_top(candidates, name, None, _CONTEXT_ENRICH_TOP)
        scored = self._score_by_name(name, candidates)
        return scored[:limit]

    async def confirm(
        self,
        candidate_id: str,
        paper_id: str,
        name: str,
        *,
        confirmed_by: str = "cli",
    ) -> ConfirmResult:
        """Confirm *candidate_id* as the identity of *name* in *paper_id*.

        Writes the ``author_identity_global`` row (``status="confirmed"``)
        plus the paper-level ``author_identity`` evidence link; a later
        :meth:`resolve` of the same name returns the confirmed identity
        directly (I8 cross-paper reuse).

        Raises:
            PaperNotFoundError: *paper_id* is not in storage.
            AuthorNotFoundError: *name* is not in the paper's byline.
            ValueError: When *candidate_id* matches no supported form.
        """
        paper = await self._load_paper(paper_id)
        if paper is None:
            raise PaperNotFoundError(f"未找到论文 {paper_id!r}，无法建立证据链接")
        ref = self._locate_author_ref(paper, name)
        if ref is None:
            byline = ", ".join(a.name for a in paper.authors[:10]) or "(空)"
            raise AuthorNotFoundError(
                f"论文 {paper_id!r} 的作者列表中未找到 {name!r}（作者：{byline}），"
                "无法建立论文级证据链接"
            )
        source, author_id = parse_candidate_id(candidate_id)
        await self._storage.save_author_identity_global(
            author_name=ref.name,
            author_id=author_id,
            source=source,
            status="confirmed",
            confidence=1.0,
            confirmed_by=confirmed_by,
        )
        await self._storage.save_author_identity(
            paper_id=paper_id,
            author_name=ref.name,
            author_id=author_id,
            source=source,
        )
        return ConfirmResult(
            author_name=ref.name,
            author_id=author_id,
            source=source,
            paper_id=paper_id,
            confirmed_by=confirmed_by,
            message=(
                f"已确认 {ref.name!r} = {source}:{author_id}，写回 "
                "author_identity_global（confirmed）+ 论文级证据链接；"
                "再次 resolve 同名作者将直接命中"
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _load_paper(self, paper_id: str) -> Paper | None:
        """Load a paper by id, tolerating the arXiv version suffix.

        ``paper source arxiv get`` stores new-style arXiv ids with their
        version (``2403.05525v2``), while callers commonly quote the
        version-free form (``2403.05525``); ``get_paper_by_arxiv_id``
        closes that gap.  On an id miss the lookup is always attempted —
        for a non-arXiv id it simply matches nothing.
        """
        paper = await self._storage.get_paper(paper_id)
        if paper is not None:
            return cast("Paper | None", paper)
        get_by_arxiv = getattr(self._storage, "get_paper_by_arxiv_id", None)
        if get_by_arxiv is not None:
            lookup: Callable[[str], Awaitable[Paper | None]] = get_by_arxiv
            return await lookup(paper_id)
        return None

    @staticmethod
    def _locate_author_ref(paper: Paper, name: str) -> AuthorRef | None:
        """Locate the byline ref for *name* (exact match first, then token match)."""
        for ref in paper.authors:
            if ref.name.strip().lower() == name.strip().lower():
                return ref
        for ref in paper.authors:
            if author_name_matches(name, ref.name):
                return ref
        return None

    @staticmethod
    def _query_author(paper: Paper, ref: AuthorRef) -> Author:
        """The identity-query author: paper byline context as features."""
        return Author(
            name=ref.name,
            affiliation=ref.affiliation,
            coauthors=[a.name for a in paper.authors if a.name != ref.name],
            active_years=[paper.year] if paper.year else None,
            venues=[paper.venue] if paper.venue else [],
        )

    def _candidate_as_author(self, candidate: AuthorCandidate) -> Author:
        """Convert a candidate into the :class:`Author` feature shape."""
        return Author(
            name=candidate.name,
            affiliation=candidate.affiliation,
            interests=candidate.interests,
            coauthors=candidate.coauthors,
            active_years=candidate.active_years or None,
            venues=candidate.venues,
        )

    async def _profile_for(
        self,
        author_id: str,
        source: str,
    ) -> AuthorProfile | None:
        if source == "orcid":
            return await self._fetcher.fetch_by_orcid(author_id)
        return await self._fetcher.fetch_profile(author_id, source)

    async def _safe_profile(
        self,
        author_id: str,
        source: str,
    ) -> AuthorProfile | None:
        """Profile fetch that degrades to ``None`` instead of raising.

        Used where the design allows a fallback (branch A stale id, I8
        confirmed-row re-fetch); :meth:`profile` keeps the raw error.
        """
        try:
            return await self._profile_for(author_id, source)
        except Exception as exc:
            logger.warning(
                "profile fetch %s:%s failed, treated as missing: %s",
                source,
                author_id,
                exc,
            )
            return None

    async def _search_candidates(
        self,
        name: str,
        limit: int = 25,
    ) -> list[AuthorCandidate]:
        """Same-name candidates from OpenAlex + S2, deduplicated."""
        candidates: list[AuthorCandidate] = []
        seen: set[str] = set()
        for source in ("openalex", "s2"):
            try:
                for candidate in await self._fetcher.search(name, source, limit=limit):
                    if candidate.candidate_id in seen:
                        continue
                    seen.add(candidate.candidate_id)
                    candidates.append(candidate)
            except Exception as exc:
                logger.warning(
                    "author search %s failed for %r: %s", source, name, exc
                )
        # Local library authors matching the name (design: 本地库 + 源候选).
        try:
            local = await self._storage.query_authors(name=name, limit=limit)
            for author in local:
                candidate = self._local_candidate(author)
                if candidate.candidate_id in seen:
                    continue
                seen.add(candidate.candidate_id)
                candidates.append(candidate)
        except Exception as exc:
            logger.warning("local author query failed for %r: %s", name, exc)
        return candidates

    @staticmethod
    def _local_candidate(author: Author) -> AuthorCandidate:
        """Convert a stored :class:`Author` into a ``local:`` candidate."""
        return AuthorCandidate(
            candidate_id=f"local:{author.id or author.name}",
            source="local",
            name=author.name,
            affiliation=author.affiliation,
            interests=author.interests,
            coauthors=author.coauthors,
            active_years=list(author.active_years or []),
            venues=author.venues,
            h_index=author.h_index,
            citations=author.citations,
            paper_count=None,
            profile_url=author.profile_url,
            evidence=[
                evidence_entry(
                    "local", author.profile_url or "", source_id=author.id
                )
            ],
        )

    async def _enrich_top(
        self,
        candidates: list[AuthorCandidate],
        query_name: str,
        paper: Paper | None,
        top: int,
    ) -> None:
        """Fetch works context for the candidates most likely to be the person.

        The works endpoints are the expensive part of disambiguation, so the
        enrichment budget (``top`` fetches) is spent on *exact-name*
        candidates first — those are the plausible same-person set — then on
        the remaining most-prominent candidates.  Every enriched candidate
        gets its coauthor / year / venue features filled and the
        ``paper_match`` flag set when its works contain the very paper being
        resolved (identity-grade evidence: the source's own authorship
        attribution).
        """
        def _prominence(candidate: AuthorCandidate) -> tuple[float, float]:
            return float(candidate.citations or 0), float(candidate.h_index or 0)

        def _rank_key(candidate: AuthorCandidate) -> tuple[int, float, float]:
            # 1 = exact-name (enriched first under reverse=True), then
            # prominence desc within each tier.
            exact = 1 if name_similarity(query_name, candidate.name) >= 1.0 else 0
            citations, h_index = _prominence(candidate)
            return exact, citations, h_index

        ranked = sorted(candidates, key=_rank_key, reverse=True)[:top]
        for candidate in ranked:
            _, _, author_id = candidate.candidate_id.partition(":")
            if not author_id or candidate.source not in ("openalex", "s2"):
                continue
            context = await self._fetcher.works_context(
                author_id, candidate.source
            )
            candidate.coauthors = [
                n for n in dict.fromkeys(context.coauthors) if n != candidate.name
            ]
            candidate.active_years = sorted(set(context.active_years))
            candidate.venues = list(dict.fromkeys(context.venues))
            if paper is not None and self._paper_in_works(paper, context):
                candidate.paper_match = True

    @staticmethod
    def _paper_in_works(paper: Paper, context: WorksContext) -> bool:
        """Whether the resolved paper appears in the candidate's works.

        Matches on arXiv id, DOI or normalized title — the source's own
        authorship attribution for the exact paper, i.e. identity-grade
        evidence (the same standing as an authority id on the byline).
        """
        if paper.arxiv_id and _normalize_arxiv(paper.arxiv_id) in context.arxiv_ids:
            return True
        if paper.doi and paper.doi.lower() in context.dois:
            return True
        if paper.title:
            target = _normalize_title(paper.title)
            if target and any(_normalize_title(t) == target for t in context.titles):
                return True
        return False

    def _score_candidates(
        self,
        query_author: Author,
        candidates: list[AuthorCandidate],
    ) -> list[AuthorCandidate]:
        """Score branch-B candidates against the paper-context author.

        A ``paper_match`` candidate scores a perfect 1.0 (the source asserts
        the candidate authored the very paper being resolved — the same
        authoritative standing the disambiguator gives ID-linked pairs);
        everything else uses the weighted feature score with the design
        thresholds (>= 0.85 判同 / 0.60..0.85 ambiguous / < 0.60 不同人).
        """
        scored: list[AuthorCandidate] = []
        for candidate in candidates:
            if candidate.paper_match:
                scored.append(
                    candidate.model_copy(
                        update={
                            "score": 1.0,
                            "verdict": "same",
                            "features": {"paper_match": 1.0},
                        }
                    )
                )
                continue
            score = self._disambiguator.score_pair(
                query_author, self._candidate_as_author(candidate)
            )
            scored.append(
                candidate.model_copy(
                    update={
                        "score": score.total,
                        "verdict": self._verdict_for(score.total),
                        "features": {
                            "name": score.name_similarity,
                            "affiliation": score.affiliation_overlap,
                            "topic": score.topic_similarity,
                            "coauthor": score.coauthor_overlap,
                            "year": score.year_range_overlap,
                            "venue": score.venue_overlap,
                        },
                    }
                )
            )
        scored.sort(
            key=lambda c: (
                c.score is None,
                -(c.score or 0.0),
                -(c.citations or 0),
                -(c.h_index or 0),
            )
        )
        return scored

    def _score_by_name(
        self,
        name: str,
        candidates: list[AuthorCandidate],
    ) -> list[AuthorCandidate]:
        """Search disambiguation: score candidates against the bare name.

        The query has no paper context, so non-name features are
        incomparable → neutral 0.5 (the honest value); the composite is
        ``name_weight * name_similarity + (1 - name_weight) * 0.5`` and the
        ordering tie-breaks by prominence.  An exact-name candidate lands in
        the ambiguous band — it is a candidate, never an unverified merge.
        """
        config = self._disambiguator.config
        scored: list[AuthorCandidate] = []
        for candidate in candidates:
            name_score = name_similarity(name, candidate.name)
            total = config.name_weight * name_score + (1 - config.name_weight) * 0.5
            scored.append(
                candidate.model_copy(
                    update={
                        "score": total,
                        "verdict": self._verdict_for(total),
                        "features": {"name": name_score},
                    }
                )
            )
        scored.sort(
            key=lambda c: (
                c.score is None,
                -(c.score or 0.0),
                -(c.citations or 0),
                -(c.h_index or 0),
            )
        )
        return scored

    def _verdict_for(self, total: float) -> str:
        if total >= self._disambiguator.config.auto_merge_threshold:
            return "same"
        if total >= self._disambiguator.config.ambiguous_threshold:
            return "ambiguous"
        return "different"

    # -- evidence ------------------------------------------------------------

    @staticmethod
    def _profile_evidence(profile: AuthorProfile) -> list[dict[str, Any]]:
        entries = list(profile.evidence)
        if profile.representative_papers:
            entries.append(
                evidence_entry(
                    profile.source,
                    profile.profile_url or "",
                    source_id=profile.author_id,
                    detail=(
                        f"代表作 {len(profile.representative_papers)} 篇"
                        "（按引用数排序）"
                    ),
                )
            )
        return entries

    @staticmethod
    def _candidate_evidence(candidate: AuthorCandidate) -> dict[str, Any]:
        if candidate.paper_match:
            detail = "著作列表包含本论文（源作者归属直接匹配，身份级证据）"
        elif candidate.score is not None:
            detail = f"综合分 {candidate.score:.2f}"
        else:
            detail = "未打分"
        return evidence_entry(
            candidate.source,
            candidate.profile_url or "",
            source_id=candidate.candidate_id,
            detail=detail,
        )

    @staticmethod
    def _identity_evidence(
        byline_name: str,
        row: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [
            evidence_entry(
                str(row.get("source") or ""),
                "",
                source_id=str(row.get("author_id")),
                confidence=float(row.get("confidence") or 0.9),
                detail=(
                    f"已确认身份（confirmed_by={row.get('confirmed_by') or '?'}），"
                    f"byline={byline_name!r}"
                ),
            )
        ]

    @staticmethod
    def _candidate_from_identity(row: dict[str, Any]) -> AuthorCandidate:
        source = str(row.get("source") or "")
        author_id = str(row.get("author_id") or "")
        return AuthorCandidate(
            candidate_id=f"{source}:{author_id}",
            source=source,
            name=str(row.get("author_name") or ""),
            score=float(row.get("confidence") or 0.9) if row.get("confidence") else None,
            verdict="confirmed",
            evidence=[
                evidence_entry(
                    source,
                    "",
                    source_id=author_id,
                    confidence=float(row.get("confidence") or 0.9),
                    detail=f"confirmed_by={row.get('confirmed_by') or '?'}",
                )
            ],
        )
