"""Graph expansion traversal with lazy loading, depth control and truncation.

``expand_from_graph`` performs a bounded BFS from a center entity:

1. The center's type is resolved from the session graph or storage (papers
   via ``get_paper``, authors via ``get_author``).
2. For each requested relation the neighbor list is first looked up in
   storage (``get_references`` / ``get_citations`` / ``get_author_papers`` /
   ``get_coauthors`` or the paper's ``authors``).  A storage hit builds the
   edges directly; neighbors without a full record become placeholder nodes
   with ``loaded=False``.
3. On a storage miss (and ``fetch_missing=True``) the data sources are
   queried through the collector; the fetched entities are persisted with
   ``storage.save_batch`` and their edges are added.  A failed fetch is
   counted in ``stats.failed`` with a human-readable reason appended to
   ``stats.failures`` (FIX-R F1) and never blocks the rest of the pass.
4. Discovery is bounded by ``max_nodes`` per pass and ``max_depth`` levels;
   when a limit is hit ``stats.truncated`` is set.

Relation semantics (kept consistent with the storage layer):

- ``references``: papers cited by the center (edge ``center --cites--> ref``)
- ``citations``: papers citing the center (edge ``citing --cites--> center``)
- ``authors``: authors of the center paper (edge ``paper --authored_by--> author``)
- ``papers``: papers authored by the center author (edge ``paper --authored_by--> author``)
- ``coauthors``: co-authors of the center author (two ``coauthor_with`` edges)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from academic_intelligence.core.models import Author, ExpandResult, ExpandStats, Paper
from academic_intelligence.graph.knowledge_graph import KnowledgeGraph

logger = logging.getLogger(__name__)

DEFAULT_MAX_NODES = 50
DEFAULT_MAX_DEPTH = 3

# Valid relation names
PAPER_RELATIONS = frozenset({"references", "citations", "authors"})
AUTHOR_RELATIONS = frozenset({"papers", "coauthors"})
ALL_RELATIONS = PAPER_RELATIONS | AUTHOR_RELATIONS

# Pseudo-author id prefix used for unresolved byline names (storage convention)
PSEUDO_AUTHOR_PREFIX = "~"


def _normalize_relations(relations: Sequence[str] | None) -> set[str] | None:
    """Validate and normalize the requested relation names.

    Returns ``None`` when *relations* is ``None`` (meaning "all applicable"),
    otherwise a set of validated relation names.  Raises ``ValueError`` for
    unknown relation names.
    """
    if relations is None:
        return None
    normalized = set(relations)
    unknown = normalized - ALL_RELATIONS
    if unknown:
        raise ValueError(
            f"unknown relation(s): {sorted(unknown)}; "
            f"expected one of {sorted(ALL_RELATIONS)}"
        )
    return normalized


def _srcs(sources: Sequence[Any] | None) -> Sequence[Any] | None:
    """Pass through resolved source instances (or ``None`` for all)."""
    return sources


def _record_failure(stats: Any, message: str) -> None:
    """Record one relation-expansion failure with a human-readable reason.

    Keeps ``stats.failed`` and ``stats.failures`` in lockstep so callers can
    use either the aggregate count or the detailed reasons (FIX-R F1).
    """
    stats.failed += 1
    stats.failures.append(message)


async def _resolve_entity_type(
    graph: KnowledgeGraph,
    storage: Any,
    entity_id: str,
) -> str | None:
    """Determine the type of *entity_id*: ``"paper"`` / ``"author"`` / None."""
    node = graph.get_node(entity_id)
    if node is not None:
        return node.get("type")
    if await storage.get_paper(entity_id) is not None:
        return "paper"
    if await storage.get_author(entity_id) is not None:
        return "author"
    return None


async def _ensure_center_node(
    graph: KnowledgeGraph,
    storage: Any,
    entity_id: str,
    entity_type: str,
) -> None:
    """Register the center entity in the session graph if not already present."""
    if graph.has_node(entity_id):
        # (FIX-V F5) A resident placeholder stub may have been backfilled into
        # storage since it was created; upgrade it so expanding a previously
        # stubbed center shows the loaded record.  Loaded nodes are left to
        # the expanders (their refresh happens where the record is read) so
        # cache-hit passes keep the I-14 zero-read fast path.
        node = graph.get_node(entity_id)
        if node is not None and not node.get("loaded"):
            if entity_type == "paper":
                paper = await storage.get_paper(entity_id)
                _refresh_paper_node(graph, entity_id, paper)
            else:
                author = await storage.get_author(entity_id)
                _refresh_author_node(graph, entity_id, author)
        return
    if entity_type == "paper":
        paper = await storage.get_paper(entity_id)
        graph.add_node(
            entity_id,
            type="paper",
            loaded=paper is not None,
            title=paper.title if paper is not None else None,
            year=paper.year if paper is not None else None,
        )
    else:
        author = await storage.get_author(entity_id)
        graph.add_node(
            entity_id,
            type="author",
            loaded=author is not None,
            name=author.name if author is not None else None,
        )


async def _fetch_center(
    storage: Any,
    collector: Any,
    entity_id: str,
    sources: Sequence[Any] | None,
) -> tuple[str | None, str | None]:
    """Try to resolve an unknown center by fetching it from the sources.

    Returns ``(entity_type, failure_reason)``; *entity_type* is ``None`` when
    the entity cannot be resolved, in which case *failure_reason* explains why
    (FIX-R F1).
    """
    try:
        result = await collector.collect_paper(entity_id, sources=_srcs(sources))
    except Exception as exc:
        return None, f"failed to fetch center {entity_id}: {exc}"
    if result.papers:
        await storage.save_batch(authors=result.authors, papers=result.papers)
        return "paper", None
    return None, f"could not resolve {entity_id} from the sources (no matching record returned)"


# ---------------------------------------------------------------------------
# Neighbor builders
# ---------------------------------------------------------------------------


async def _neighbor_for_paper(
    graph: KnowledgeGraph,
    storage: Any,
    paper_id: str,
    relation: str,
    source: str,
    target: str,
) -> dict[str, Any]:
    # (I-14) Resident neighbors skip the storage read: the caller only needs
    # the edge bookkeeping (id/source/target/relation) for a cache hit, and
    # the resident node already carries its attributes.
    if graph.has_node(paper_id):
        return {
            "id": paper_id,
            "type": "paper",
            "source": source,
            "target": target,
            "relation": relation,
        }
    paper: Paper | None = await storage.get_paper(paper_id)
    if paper is not None:
        return {
            "id": paper_id,
            "type": "paper",
            "loaded": True,
            "title": paper.title,
            "year": paper.year,
            "source": source,
            "target": target,
            "relation": relation,
        }
    return {
        "id": paper_id,
        "type": "paper",
        "loaded": False,
        "title": None,
        "year": None,
        "source": source,
        "target": target,
        "relation": relation,
    }


async def _neighbor_for_author(
    graph: KnowledgeGraph,
    storage: Any,
    author_id: str,
    name: str | None,
    relation: str,
    source: str,
    target: str,
) -> dict[str, Any]:
    # (I-14) Resident neighbors skip the storage read (see _neighbor_for_paper).
    if graph.has_node(author_id):
        return {
            "id": author_id,
            "type": "author",
            "source": source,
            "target": target,
            "relation": relation,
        }
    author: Author | None = await storage.get_author(author_id)
    if author is not None:
        return {
            "id": author_id,
            "type": "author",
            "loaded": True,
            "name": author.name,
            "source": source,
            "target": target,
            "relation": relation,
        }
    return {
        "id": author_id,
        "type": "author",
        "loaded": False,
        "name": name,
        "source": source,
        "target": target,
        "relation": relation,
    }


def _node_attrs(neighbor: dict[str, Any]) -> dict[str, Any]:
    """Extract the serializable node attributes from a neighbor dict."""
    attrs: dict[str, Any] = {}
    for key in ("title", "name", "year"):
        if neighbor.get(key) is not None:
            attrs[key] = neighbor[key]
    return attrs


async def _upgrade_resident_stubs(
    graph: KnowledgeGraph,
    storage: Any,
    candidates: Sequence[str],
) -> None:
    """(FIX-V F5) Upgrade resident placeholder stubs whose records were
    backfilled into storage since the stub was created (P40 V-E).

    Runs on storage-only passes (``fetch_missing=False``) where the graph is
    meant to mirror storage: each still-unloaded resident candidate is
    re-checked and refreshed via ``_refresh_paper_node`` /
    ``_refresh_author_node``.  Fetch-enabled cache-hit passes never reach this
    helper (the caller gates it on ``not fetch_missing``), preserving the I-14
    zero-read fast path.
    """
    for candidate in candidates:
        node = graph.get_node(candidate)
        if node is None or node.get("loaded"):
            continue
        if node.get("type") == "paper":
            paper = await storage.get_paper(candidate)
            if paper is not None:
                _refresh_paper_node(graph, candidate, paper)
        else:
            author = await storage.get_author(candidate)
            if author is not None:
                _refresh_author_node(graph, candidate, author)


async def _bounded_neighbor_list(
    graph: KnowledgeGraph,
    candidates: Sequence[str],
    make_neighbors: Callable[[str], Awaitable[Sequence[dict[str, Any]]]],
    max_nodes: int,
    stats: Any,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    """Materialize neighbor dicts, stopping at the node budget (FIX-M F3/M3).

    The BFS loop only checks the node budget *after* an expander has returned
    its full neighbor list, so with a large candidate set (e.g. 1000
    references) every candidate was materialized through
    ``storage.get_paper`` first — a 12.8s pass for a graph that truncates at
    50 nodes.  This helper walks the candidates in byline order and stops
    materializing as soon as the budget is exhausted, mirroring exactly which
    candidates the loop would add: cache hits (resident nodes) and within-pass
    duplicates cost nothing, each genuinely new node costs one slot.  When
    candidates remain past the budget, ``stats.truncated`` is set, matching
    the loop's truncation semantics.
    """
    neighbors: list[dict[str, Any]] = []
    new_budget = max_nodes - stats.nodes_found
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if graph.has_node(candidate) or candidate in pass_discovered:
            neighbors.extend(await make_neighbors(candidate))
            continue
        if new_budget <= 0:
            stats.truncated = True
            break
        new_budget -= 1
        neighbors.extend(await make_neighbors(candidate))
    return neighbors


# ---------------------------------------------------------------------------
# Per-relation expansion (storage first, fetch on miss)
# ---------------------------------------------------------------------------


async def _expand_references(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    paper_id: str,
    fetch_missing: bool,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    stored = await storage.get_references(paper_id)
    if stored:
        # (FIX-V F5) Upgrade resident reference stubs backfilled into storage
        # since the last expand (P40 V-E).
        if not fetch_missing:
            await _upgrade_resident_stubs(graph, storage, stored)
        # (FIX-M F3 / M3) Materialize only up to the node budget.
        async def make_reference(ref_id: str) -> list[dict[str, Any]]:
            return [
                await _neighbor_for_paper(
                    graph, storage, ref_id, "cites", paper_id, ref_id
                )
            ]

        return await _bounded_neighbor_list(
            graph,
            stored,
            make_reference,
            max_nodes,
            stats,
            pass_discovered,
        )
    if not fetch_missing:
        return []
    return await _fetch_references(
        graph, storage, collector, paper_id, sources, stats,
        max_nodes=max_nodes, pass_discovered=pass_discovered,
    )


async def _expand_citations(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    paper_id: str,
    fetch_missing: bool,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    stored = await storage.get_citations(paper_id)
    if stored:
        # (FIX-V F5) Upgrade resident citing-paper stubs backfilled into
        # storage since the last expand (P40 V-E).
        if not fetch_missing:
            await _upgrade_resident_stubs(graph, storage, stored)
        # (FIX-M F3 / M3) Materialize only up to the node budget.
        async def make_citing(citing_id: str) -> list[dict[str, Any]]:
            return [
                await _neighbor_for_paper(
                    graph, storage, citing_id, "cites", citing_id, paper_id
                )
            ]

        return await _bounded_neighbor_list(
            graph,
            stored,
            make_citing,
            max_nodes,
            stats,
            pass_discovered,
        )
    if not fetch_missing:
        return []
    return await _fetch_citations(
        graph, storage, collector, paper_id, sources, stats,
        max_nodes=max_nodes, pass_discovered=pass_discovered,
    )


async def _expand_authors(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    paper_id: str,
    fetch_missing: bool,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    paper = await storage.get_paper(paper_id)
    # (FIX-H F3 / H4) The stored record is in hand: refresh the resident
    # session-graph node so same-session expands show current metadata.
    _refresh_paper_node(graph, paper_id, paper)
    if paper is not None and paper.authors:
        candidate_ids = [
            author_ref.author_id or f"{PSEUDO_AUTHOR_PREFIX}{author_ref.name}"
            for author_ref in paper.authors
        ]
        name_by_id = {
            candidate_id: author_ref.name
            for candidate_id, author_ref in zip(
                candidate_ids, paper.authors, strict=False
            )
        }
        # (FIX-V F5) Upgrade resident author stubs whose profiles were
        # backfilled into storage since the last expand (P40 V-E).
        if not fetch_missing:
            await _upgrade_resident_stubs(graph, storage, candidate_ids)

        async def make_author(author_id: str) -> list[dict[str, Any]]:
            return [
                await _neighbor_for_author(
                    graph,
                    storage,
                    author_id,
                    name_by_id[author_id],
                    "authored_by",
                    paper_id,
                    author_id,
                )
            ]

        # (FIX-M F3 / M3) Materialize only up to the node budget.
        return await _bounded_neighbor_list(
            graph,
            candidate_ids,
            make_author,
            max_nodes,
            stats,
            pass_discovered,
        )
    if not fetch_missing:
        return []
    return await _fetch_authors(
        graph, storage, collector, paper_id, sources, stats,
        max_nodes=max_nodes, pass_discovered=pass_discovered,
    )


async def _expand_papers(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    author_id: str,
    fetch_missing: bool,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    stored = await storage.get_author_papers(author_id)
    if stored:
        # (FIX-V F5) Upgrade resident paper stubs backfilled into storage
        # since the last expand (P40 V-E).
        if not fetch_missing:
            await _upgrade_resident_stubs(graph, storage, stored)
        # (FIX-M F3 / M3) Materialize only up to the node budget.
        async def make_paper(paper_id: str) -> list[dict[str, Any]]:
            return [
                await _neighbor_for_paper(
                    graph,
                    storage,
                    paper_id,
                    "authored_by",
                    paper_id,
                    author_id,
                )
            ]

        return await _bounded_neighbor_list(
            graph,
            stored,
            make_paper,
            max_nodes,
            stats,
            pass_discovered,
        )
    if not fetch_missing:
        return []
    return await _fetch_papers(
        graph, storage, collector, author_id, sources, stats,
        max_nodes=max_nodes, pass_discovered=pass_discovered,
    )


async def _expand_coauthors(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    author_id: str,
    fetch_missing: bool,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    stored = await storage.get_coauthors(author_id)
    if stored:
        # (FIX-V F5) Upgrade resident coauthor stubs backfilled into storage
        # since the last expand (P40 V-E).
        if not fetch_missing:
            await _upgrade_resident_stubs(graph, storage, stored)
        async def make_coauthor(coauthor_id: str) -> list[dict[str, Any]]:
            forward = await _neighbor_for_author(
                graph,
                storage,
                coauthor_id,
                None,
                "coauthor_with",
                author_id,
                coauthor_id,
            )
            return [forward, dict(forward, source=coauthor_id, target=author_id)]

        # (FIX-M F3 / M3) Materialize only up to the node budget.
        return await _bounded_neighbor_list(
            graph,
            stored,
            make_coauthor,
            max_nodes,
            stats,
            pass_discovered,
        )
    if not fetch_missing:
        return []
    return await _fetch_coauthors(
        graph, storage, collector, author_id, sources, stats,
        max_nodes=max_nodes, pass_discovered=pass_discovered,
    )


# ---------------------------------------------------------------------------
# Fetch helpers (storage miss -> data sources)
# ---------------------------------------------------------------------------


# OpenAlex work id shape: placeholder nodes created from citation edges carry
# bare ``W\\d+`` ids.  Those can be backfilled by-id (FIX-B1 F3) instead of
# being treated as unfetchable dead ends.
_WORK_ID_RE = re.compile(r"^W\d+$")


async def _fetch_paper_record(
    storage: Any,
    collector: Any,
    paper_id: str,
    sources: Sequence[Any] | None,
    graph: KnowledgeGraph | None = None,
) -> tuple[Paper | None, str | None]:
    """Return the stored paper, backfilling a W-id placeholder if necessary.

    When *paper_id* is not in storage but looks like an OpenAlex work id
    (``W\\d+``), the record is fetched through ``collector.collect_paper``
    (which routes by-id for capable sources) and persisted.  Returns
    ``(None, reason)`` when the paper cannot be located, with *reason*
    explaining why (FIX-R F1).

    When *graph* is given and the entity is resident as a placeholder stub
    (``loaded=False``, created from citation edges), the node is refreshed
    with the loaded record so the session graph stops showing a stub
    (FIX-E-6).
    """
    paper = await storage.get_paper(paper_id)
    if paper is not None:
        _refresh_paper_node(graph, paper_id, paper)
        return cast(Paper, paper), None
    if not _WORK_ID_RE.match(paper_id):
        return (
            None,
            f"no stored record for {paper_id} and it is not a backfillable work id",
        )
    try:
        result = await collector.collect_paper(paper_id, sources=_srcs(sources))
    except Exception as exc:
        return None, f"failed to fetch {paper_id}: {exc}"
    await storage.save_batch(authors=result.authors, papers=result.papers)
    fetched = await storage.get_paper(paper_id)
    if fetched is None:
        return None, f"fetch of {paper_id} returned no matching record"
    _refresh_paper_node(graph, paper_id, fetched)
    return cast(Paper, fetched), None


def _refresh_paper_node(
    graph: KnowledgeGraph | None,
    paper_id: str,
    paper: Paper,
) -> None:
    """Refresh a resident session-graph node with a loaded paper record.

    Placeholder stubs (``loaded=False``) created from citation edges are
    upgraded to full nodes carrying the record's title/year (FIX-E-6); loaded
    nodes whose stored attributes changed are refreshed in place (FIX-H F3 /
    H4).  No-op when the node is not resident, the record is missing, or the
    attributes are already current.
    """
    if graph is None or paper is None or not graph.has_node(paper_id):
        return
    node = graph.get_node(paper_id)
    if (
        node is not None
        and node.get("loaded")
        and node.get("title") == paper.title
        and node.get("year") == paper.year
    ):
        return
    graph.add_node(
        paper_id,
        type="paper",
        loaded=True,
        title=paper.title,
        year=paper.year,
    )


def _refresh_author_node(
    graph: KnowledgeGraph | None,
    author_id: str,
    author: Author,
) -> None:
    """Refresh a resident session-graph author node with a loaded record.

    Mirrors :func:`_refresh_paper_node` for author nodes (FIX-H F3 / H4):
    stubs are upgraded and loaded nodes get their ``name`` refreshed when the
    stored record changed.  No-op when the node is not resident or already
    current.
    """
    if graph is None or author is None or not graph.has_node(author_id):
        return
    node = graph.get_node(author_id)
    if (
        node is not None
        and node.get("loaded")
        and node.get("name") == author.name
    ):
        return
    graph.add_node(author_id, type="author", loaded=True, name=author.name)


async def _fetch_citations(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    paper_id: str,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    """Fetch citing papers via ``collector.collect_citations`` and persist."""
    try:
        result = await collector.collect_citations(paper_id, sources=_srcs(sources))
    except Exception as exc:
        logger.warning("Failed to fetch citations for %s: %s", paper_id, exc)
        _record_failure(stats, f"failed to fetch citations for {paper_id}: {exc}")
        return []
    await storage.save_batch(
        authors=result.authors,
        papers=result.papers,
        citations=result.citations,
    )
    candidates: list[str] = []
    seen: set[str] = set()
    for citation in result.citations:
        if citation.cited_paper_id != paper_id or citation.citing_paper_id in seen:
            continue
        seen.add(citation.citing_paper_id)
        candidates.append(citation.citing_paper_id)

    async def make(citing_id: str) -> list[dict[str, Any]]:
        return [
            await _neighbor_for_paper(
                graph,
                storage,
                citing_id,
                "cites",
                citing_id,
                paper_id,
            )
        ]

    # (FIX-M F3 / M3) Materialize only up to the node budget.
    neighbors = await _bounded_neighbor_list(
        graph, candidates, make, max_nodes, stats, pass_discovered
    )
    if neighbors:
        stats.fetched_new += len(neighbors)
    else:
        _record_failure(stats, f"no citing papers returned for {paper_id}")
    return neighbors


async def _neighbors_from_references(
    graph: KnowledgeGraph,
    storage: Any,
    paper_id: str,
    ref_ids: Sequence[str],
    seen: set[str] | None = None,
    *,
    max_nodes: int,
    stats: Any,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    """Build ``paper_id --cites--> ref_id`` neighbors from a reference list.

    (FIX-M F3 / M3) Materialization is bounded by the node budget — a
    truncated pass must not read every candidate record.
    """
    seen = set() if seen is None else seen
    candidates: list[str] = []
    for ref_id in ref_ids:
        if ref_id in seen or ref_id == paper_id:
            continue
        seen.add(ref_id)
        candidates.append(ref_id)

    async def make(ref_id: str) -> list[dict[str, Any]]:
        return [
            await _neighbor_for_paper(graph, storage, ref_id, "cites", paper_id, ref_id)
        ]

    return await _bounded_neighbor_list(
        graph, candidates, make, max_nodes, stats, pass_discovered
    )


async def _fetch_references(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    paper_id: str,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    """Fetch the paper record and turn its ``references`` list into edges.

    When the record (stored or backfilled from a W-id placeholder) already
    carries its ``references`` list (FIX-B1 F2), the edges are built directly.
    Otherwise a best-effort fallback re-collects the paper by DOI/title and
    uses any references found on the returned record.  A miss is recorded in
    ``stats.failed`` / ``stats.failures`` without blocking.
    """
    paper, fetch_reason = await _fetch_paper_record(
        storage, collector, paper_id, sources, graph
    )
    if paper is None:
        _record_failure(
            stats, fetch_reason or f"could not locate paper record for {paper_id}"
        )
        return []
    if paper.references:
        neighbors = await _neighbors_from_references(
            graph,
            storage,
            paper_id,
            paper.references,
            max_nodes=max_nodes,
            stats=stats,
            pass_discovered=pass_discovered,
        )
        if neighbors:
            stats.fetched_new += len(neighbors)
        else:
            _record_failure(stats, f"no references found for {paper_id}")
        return neighbors
    query = paper.doi or paper.title
    try:
        result = await collector.collect_paper(query, sources=_srcs(sources))
    except Exception as exc:
        logger.warning("Failed to fetch references for %s: %s", paper_id, exc)
        _record_failure(stats, f"failed to fetch references for {paper_id}: {exc}")
        return []
    await storage.save_batch(authors=result.authors, papers=result.papers)
    fallback_neighbors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fetched in result.papers:
        if not fetched.references:
            continue
        fallback_neighbors.extend(
            await _neighbors_from_references(
                graph,
                storage,
                paper_id,
                fetched.references,
                seen,
                max_nodes=max_nodes,
                stats=stats,
                pass_discovered=pass_discovered,
            )
        )
    if fallback_neighbors:
        stats.fetched_new += len(fallback_neighbors)
    else:
        _record_failure(stats, f"no references found for {paper_id}")
    return fallback_neighbors


async def _fetch_authors(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    paper_id: str,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    """Fetch the paper record and derive its authors."""
    paper, fetch_reason = await _fetch_paper_record(
        storage, collector, paper_id, sources, graph
    )
    if paper is None:
        _record_failure(
            stats, fetch_reason or f"could not locate paper record for {paper_id}"
        )
        return []
    query = paper.doi or paper.title
    try:
        result = await collector.collect_paper(query, sources=_srcs(sources))
    except Exception as exc:
        logger.warning("Failed to fetch authors for %s: %s", paper_id, exc)
        _record_failure(stats, f"failed to fetch authors for {paper_id}: {exc}")
        return []
    await storage.save_batch(authors=result.authors, papers=result.papers)
    candidates: list[str] = []
    name_by_id: dict[str, str] = {}
    seen: set[str] = set()
    for fetched in result.papers:
        for author_ref in fetched.authors:
            author_id = author_ref.author_id or f"{PSEUDO_AUTHOR_PREFIX}{author_ref.name}"
            if author_id in seen:
                continue
            seen.add(author_id)
            candidates.append(author_id)
            name_by_id[author_id] = author_ref.name

    async def make(author_id: str) -> list[dict[str, Any]]:
        return [
            await _neighbor_for_author(
                graph,
                storage,
                author_id,
                name_by_id[author_id],
                "authored_by",
                paper_id,
                author_id,
            )
        ]

    # (FIX-M F3 / M3) Materialize only up to the node budget.
    neighbors = await _bounded_neighbor_list(
        graph, candidates, make, max_nodes, stats, pass_discovered
    )
    if neighbors:
        stats.fetched_new += len(neighbors)
    else:
        _record_failure(stats, f"no authors found for {paper_id}")
    return neighbors


async def _fetch_papers(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    author_id: str,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    """Fetch an author's papers via ``collector.collect_author_papers``."""
    author = await storage.get_author(author_id)
    # (FIX-H F3 / H4) The stored record is in hand: refresh the resident
    # session-graph node so same-session expands show current metadata.
    _refresh_author_node(graph, author_id, author)
    if author is None:
        _record_failure(stats, f"could not locate author record for {author_id}")
        return []
    try:
        result = await collector.collect_author_papers(author.name, sources=_srcs(sources))
    except Exception as exc:
        logger.warning("Failed to fetch papers for %s: %s", author_id, exc)
        _record_failure(stats, f"failed to fetch papers for {author_id}: {exc}")
        return []
    await storage.save_batch(authors=result.authors, papers=result.papers)
    candidates = [paper.id for paper in result.papers if paper.id]

    async def make(paper_id: str) -> list[dict[str, Any]]:
        return [
            await _neighbor_for_paper(
                graph, storage, paper_id, "authored_by", paper_id, author_id
            )
        ]

    # (FIX-M F3 / M3) Materialize only up to the node budget.
    neighbors = await _bounded_neighbor_list(
        graph, candidates, make, max_nodes, stats, pass_discovered
    )
    if neighbors:
        stats.fetched_new += len(neighbors)
    else:
        _record_failure(stats, f"no papers found for {author_id}")
    return neighbors


async def _fetch_coauthors(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    author_id: str,
    sources: Sequence[Any] | None,
    stats: Any,
    *,
    max_nodes: int,
    pass_discovered: set[str],
) -> list[dict[str, Any]]:
    """Derive coauthors from the author's papers (fetched if necessary)."""
    author = await storage.get_author(author_id)
    # (FIX-H F3 / H4) The stored record is in hand: refresh the resident
    # session-graph node so same-session expands show current metadata.
    _refresh_author_node(graph, author_id, author)
    if author is None:
        _record_failure(stats, f"could not locate author record for {author_id}")
        return []

    async def make_coauthor(coauthor_id: str) -> list[dict[str, Any]]:
        forward = await _neighbor_for_author(
            graph, storage, coauthor_id, None, "coauthor_with", author_id, coauthor_id
        )
        return [forward, dict(forward, source=coauthor_id, target=author_id)]

    stored_papers = await storage.get_author_papers(author_id)
    if stored_papers:
        # Coauthors are derived from the stored paper bylines.
        coauthor_ids: set[str] = set()
        for paper_id in stored_papers:
            paper = await storage.get_paper(paper_id)
            if paper is None:
                continue
            for author_ref in paper.authors:
                candidate = author_ref.author_id or (
                    f"{PSEUDO_AUTHOR_PREFIX}{author_ref.name}"
                )
                if candidate != author_id:
                    coauthor_ids.add(candidate)
        if coauthor_ids:
            # (FIX-M F3 / M3) Materialize only up to the node budget.
            neighbors = await _bounded_neighbor_list(
                graph,
                sorted(coauthor_ids),
                make_coauthor,
                max_nodes,
                stats,
                pass_discovered,
            )
            stats.fetched_new += len(coauthor_ids)
            return neighbors
    try:
        result = await collector.collect_author_papers(author.name, sources=_srcs(sources))
    except Exception as exc:
        logger.warning("Failed to fetch coauthors for %s: %s", author_id, exc)
        _record_failure(stats, f"failed to fetch coauthors for {author_id}: {exc}")
        return []
    await storage.save_batch(authors=result.authors, papers=result.papers)
    coauthor_ids = set()
    for paper in result.papers:
        for author_ref in paper.authors:
            candidate = author_ref.author_id or (f"{PSEUDO_AUTHOR_PREFIX}{author_ref.name}")
            if candidate != author_id:
                coauthor_ids.add(candidate)
    if not coauthor_ids:
        _record_failure(stats, f"no coauthors found for {author_id}")
        return []
    # (FIX-M F3 / M3) Materialize only up to the node budget.
    neighbors = await _bounded_neighbor_list(
        graph,
        sorted(coauthor_ids),
        make_coauthor,
        max_nodes,
        stats,
        pass_discovered,
    )
    stats.fetched_new += len(coauthor_ids)
    return neighbors


# ---------------------------------------------------------------------------
# Relation dispatch
# ---------------------------------------------------------------------------

_RELATION_EXPANDERS = {
    "references": _expand_references,
    "citations": _expand_citations,
    "authors": _expand_authors,
    "papers": _expand_papers,
    "coauthors": _expand_coauthors,
}


def _relations_for(entity_type: str, relations: set[str] | None) -> list[str]:
    applicable = PAPER_RELATIONS if entity_type == "paper" else AUTHOR_RELATIONS
    if relations is None:
        return sorted(applicable)
    return sorted(applicable & relations)


def _graph_levels(graph: KnowledgeGraph, center_id: str) -> dict[str, int]:
    """Undirected BFS level of every resident node reachable from *center_id*.

    Records how deep each node has already been reached in this session
    (FIX-H F2 / H3).  A later ``expand`` that asks for more levels than the
    deepest recorded node can then drill only the unprobed part instead of
    re-expanding everything; the traversal follows undirected reachability
    because the expander adds both directed and quasi-undirected edges
    (``coauthor_with`` is stored as two directed edges).
    """
    levels: dict[str, int] = {center_id: 0}
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges():
        adjacency.setdefault(edge["source"], []).append(edge["target"])
        adjacency.setdefault(edge["target"], []).append(edge["source"])
    frontier = [center_id]
    while frontier:
        next_frontier: list[str] = []
        for node_id in frontier:
            depth = levels[node_id] + 1
            for neighbor_id in adjacency.get(node_id, ()):
                if neighbor_id in levels:
                    continue
                levels[neighbor_id] = depth
                next_frontier.append(neighbor_id)
        frontier = next_frontier
    return levels


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def expand_from_graph(
    graph: KnowledgeGraph,
    storage: Any,
    collector: Any,
    entity_id: str,
    *,
    relations: Sequence[str] | None = None,
    depth: int = 1,
    fetch_missing: bool = True,
    sources: Sequence[Any] | None = None,
    max_nodes: int | None = None,
    max_depth: int | None = None,
) -> ExpandResult:
    """Expand *entity_id*'s relationships in *graph* (see module docstring).

    Args:
        graph: The session knowledge graph (mutated in place).
        storage: Storage backend implementing the graph relationship queries.
        collector: Collector used to fetch missing entities from data sources.
        entity_id: Center entity id (paper or author).
        relations: Relation names to expand; ``None`` = all applicable.
        depth: Number of BFS levels (clamped to ``max_depth``).
        fetch_missing: Whether to fetch storage misses from the sources.
        sources: Optional resolved source instances to restrict fetching.
        max_nodes: Per-pass discovery budget (default 50).
        max_depth: Hard depth ceiling (default 3, mirrors
            ``Config.max_expand_depth``).

    Returns:
        An :class:`ExpandResult` with the newly discovered nodes/edges and
        :class:`ExpandStats`.
    """
    stats = ExpandStats()
    max_nodes = max_nodes if max_nodes is not None else DEFAULT_MAX_NODES
    max_depth = max_depth if max_depth is not None else DEFAULT_MAX_DEPTH
    depth = max(1, min(int(depth), max_depth))
    normalized = _normalize_relations(relations)

    # Resolve (or fetch) the center entity type.
    entity_type = await _resolve_entity_type(graph, storage, entity_id)
    fetch_reason: str | None = None
    if entity_type is None and fetch_missing:
        entity_type, fetch_reason = await _fetch_center(
            storage, collector, entity_id, sources
        )
    if entity_type is None:
        _record_failure(
            stats,
            fetch_reason or f"could not resolve entity type for {entity_id}",
        )
        return ExpandResult(center_id=entity_id, stats=stats)

    center_was_resident = graph.has_node(entity_id)
    await _ensure_center_node(graph, storage, entity_id, entity_type)
    # (FIX-V F4) A same-session expand of an already-resident center refreshes
    # its node from storage, so an incremental update that changed the stored
    # title/year is reflected instead of the stale cached metadata (P40 V-D —
    # the pre-fix code only refreshed on the authors path).  Gated on storage-
    # only passes so cache-hit passes keep the I-14 zero-read fast path; a
    # first pass skips it because the center record was just read by
    # ``_ensure_center_node`` (keeps the M3 materialization budget).
    if center_was_resident and not fetch_missing:
        if entity_type == "paper":
            paper = await storage.get_paper(entity_id)
            if paper is not None:
                _refresh_paper_node(graph, entity_id, paper)
        else:
            author = await storage.get_author(entity_id)
            if author is not None:
                _refresh_author_node(graph, entity_id, author)

    nodes_found: list[dict[str, Any]] = []
    edges_found: list[dict[str, Any]] = []
    seen: set[str] = {entity_id}
    frontier: list[str] = [entity_id]
    pass_discovered: set[str] = set()
    # (FIX-H F2 / H3) Per-node depth already reached in this session.  When
    # the request goes deeper than the deepest recorded node, resident nodes
    # are re-queued at their recorded level so the BFS drills only the
    # unprobed part; ``depth_reached`` then reflects the actual deepest level
    # probed.
    node_levels = _graph_levels(graph, entity_id)
    achieved_depth = max(node_levels.values()) if node_levels else 0
    drill_deeper = depth > achieved_depth
    queued: set[str] = set()

    for level in range(1, depth + 1):
        next_frontier: list[str] = []
        for current in frontier:
            node = graph.get_node(current)
            current_type = node.get("type") if node is not None else None
            if current_type is None:
                continue
            for relation in _relations_for(current_type, normalized):
                # (FIX-M F3 / M3) An expander may set ``stats.truncated``
                # itself while still returning the budgeted neighbors (it
                # stopped materializing early).  Only a truncation that
                # predates this expander call — i.e. a previous relation —
                # discards its output; the loop's own per-neighbor budget
                # check below remains the backstop for un-bounded lists.
                truncated_before = stats.truncated
                neighbors = await _RELATION_EXPANDERS[relation](
                    graph,
                    storage,
                    collector,
                    current,
                    fetch_missing,
                    sources,
                    stats,
                    # (FIX-M F3 / M3) The node budget travels into the
                    # expanders so they stop materializing neighbors once it
                    # is exhausted (a truncated pass must not read every
                    # candidate record first).
                    max_nodes=max_nodes,
                    pass_discovered=pass_discovered,
                )
                for neighbor in neighbors:
                    if stats.truncated and truncated_before:
                        break
                    neighbor_id: str = neighbor["id"]
                    # (FIX-H F1 / H1) Node budget check BEFORE the edge
                    # write: a truncated pass must never leave an edge that
                    # points at a node which was not added to the graph
                    # (ghost edge polluting subgraph/exports).
                    if (
                        neighbor_id not in pass_discovered
                        and not graph.has_node(neighbor_id)
                        and stats.nodes_found >= max_nodes
                    ):
                        stats.truncated = True
                        break
                    # Edge bookkeeping (record every new edge once).
                    if not graph.has_edge(neighbor["source"], neighbor["target"]):
                        graph.add_edge(neighbor["source"], neighbor["target"], neighbor["relation"])
                        edges_found.append(
                            {
                                "source": neighbor["source"],
                                "target": neighbor["target"],
                                "relation": neighbor["relation"],
                            }
                        )
                        stats.edges_found += 1
                    # Node bookkeeping.
                    if neighbor_id in pass_discovered:
                        continue  # duplicate within this pass
                    if graph.has_node(neighbor_id):
                        stats.cache_hits += 1  # already resident (previous pass)
                        # (FIX-H F2 / H3) Deepen: re-queue a resident neighbor
                        # at its recorded level so the next BFS level probes
                        # beneath it.  Only when the request reaches deeper
                        # than this session has achieved, and only once per
                        # level.
                        if (
                            drill_deeper
                            and node_levels.get(neighbor_id) == level
                            and neighbor_id not in queued
                        ):
                            queued.add(neighbor_id)
                            next_frontier.append(neighbor_id)
                        continue
                    pass_discovered.add(neighbor_id)
                    graph.add_node(
                        neighbor_id,
                        neighbor["type"],
                        loaded=neighbor["loaded"],
                        **_node_attrs(neighbor),
                    )
                    added_node = graph.get_node(neighbor_id)
                    if added_node is not None:
                        nodes_found.append(added_node)
                    stats.nodes_found += 1
                    if neighbor_id not in seen:
                        seen.add(neighbor_id)
                        next_frontier.append(neighbor_id)
            if stats.truncated:
                break
        stats.depth_reached = level
        if stats.truncated or not next_frontier:
            break
        frontier = next_frontier

    return ExpandResult(
        center_id=entity_id,
        nodes=nodes_found,
        edges=edges_found,
        stats=stats,
    )
