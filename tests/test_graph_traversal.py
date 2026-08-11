"""Unit tests for graph expansion traversal (lazy loading, cache, truncation).

Uses in-memory fakes for storage and collector so no network is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from academic_intelligence.collectors.base import MultiSourceCollector
from academic_intelligence.core.models import Author, AuthorRef, Citation, Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.graph import ExpandResult, KnowledgeGraph
from academic_intelligence.graph.traversal import expand_from_graph


def _citation(citing: str, cited: str) -> Citation:
    return Citation(
        citing_paper_id=citing,
        cited_paper_id=cited,
        evidence=Evidence(
            source=SourceType.OPENALEX,
            source_url="https://openalex.org/W1",
            confidence=0.8,
        ),
    )


class FakeStorage:
    """Minimal in-memory storage exposing only what traversal needs."""

    def __init__(self) -> None:
        self.papers: dict[str, Paper] = {}
        self.authors: dict[str, Author] = {}
        self.refs: dict[str, list[str]] = {}
        self.cits: dict[str, list[str]] = {}
        self.author_papers: dict[str, list[str]] = {}
        self.coauthors: dict[str, list[str]] = {}
        self.saved_batches: list[dict[str, Any]] = []

    async def get_references(self, paper_id: str) -> list[str]:
        return list(self.refs.get(paper_id, []))

    async def get_citations(self, paper_id: str) -> list[str]:
        return list(self.cits.get(paper_id, []))

    async def get_author_papers(self, author_id: str) -> list[str]:
        return list(self.author_papers.get(author_id, []))

    async def get_coauthors(self, author_id: str) -> list[str]:
        return list(self.coauthors.get(author_id, []))

    async def get_paper(self, paper_id: str) -> Paper | None:
        return self.papers.get(paper_id)

    async def get_author(self, author_id: str) -> Author | None:
        return self.authors.get(author_id)

    async def save_batch(
        self,
        *,
        authors: list[Author] | None = None,
        papers: list[Paper] | None = None,
        citations: list[Citation] | None = None,
    ) -> dict[str, list[str]]:
        authors = authors or []
        papers = papers or []
        citations = citations or []
        self.saved_batches.append(
            {"authors": authors, "papers": papers, "citations": citations}
        )
        for p in papers:
            if p.id:
                self.papers[p.id] = p
        for a in authors:
            if a.id:
                self.authors[a.id] = a
        for c in citations:
            self.cits.setdefault(c.cited_paper_id, []).append(c.citing_paper_id)
        return {
            "authors": [a.id or "" for a in authors],
            "papers": [p.id or "" for p in papers],
            "citations": [str(i) for i in range(len(citations))],
        }


class CountingStorage(FakeStorage):
    """FakeStorage that counts ``get_paper`` reads (I-14 regression check)."""

    def __init__(self) -> None:
        super().__init__()
        self.get_paper_calls = 0

    async def get_paper(self, paper_id: str) -> Paper | None:
        self.get_paper_calls += 1
        return await super().get_paper(paper_id)


class FakeCollector:
    """Fake MultiSourceCollector; records calls and returns canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.citation_results: list[Citation] = []
        self.paper_results: list[Paper] = []
        self.author_papers_results: list[Paper] = []
        self.author_results: list[Author] = []
        self.error_on: str | None = None

    async def collect_citations(
        self,
        paper_id: str,
        *,
        sources: list[Any] | None = None,
    ) -> Any:
        self.calls.append(("collect_citations", paper_id))
        if self.error_on == "collect_citations":
            raise RuntimeError("source unavailable")
        from academic_intelligence.core.models import CollectionResult

        return CollectionResult(citations=self.citation_results)

    async def collect_paper(
        self,
        query: str,
        *,
        sources: list[Any] | None = None,
        limit: int = 10,
    ) -> Any:
        self.calls.append(("collect_paper", query))
        if self.error_on == "collect_paper":
            raise RuntimeError("source unavailable")
        from academic_intelligence.core.models import CollectionResult

        return CollectionResult(papers=self.paper_results, authors=self.author_results)

    async def collect_author_papers(
        self,
        name: str,
        *,
        sources: list[Any] | None = None,
    ) -> Any:
        self.calls.append(("collect_author_papers", name))
        if self.error_on == "collect_author_papers":
            raise RuntimeError("source unavailable")
        from academic_intelligence.core.models import CollectionResult

        return CollectionResult(
            papers=self.author_papers_results,
            authors=self.author_results,
        )


def _paper(
    pid: str,
    title: str = "Untitled",
    *,
    authors: list[AuthorRef] | None = None,
    references: list[str] | None = None,
) -> Paper:
    return Paper(
        id=pid,
        title=title,
        authors=authors or [],
        references=references,
    )


def _author(aid: str, name: str) -> Author:
    return Author(id=aid, name=name)


# ---------------------------------------------------------------------------
# Storage-hit lazy loading
# ---------------------------------------------------------------------------


async def test_expand_references_storage_hit() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1", "r2"]
    storage.papers["r1"] = _paper("r1", "Ref One")
    # r2 intentionally missing -> stub node
    graph = KnowledgeGraph()
    collector = FakeCollector()

    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"]
    )

    assert isinstance(result, ExpandResult)
    assert result.center_id == "p1"
    assert result.stats.nodes_found == 2
    node_ids = {n["id"] for n in result.nodes}
    assert node_ids == {"r1", "r2"}
    edges = {(e["source"], e["target"]) for e in result.edges}
    assert ("p1", "r1") in edges
    assert ("p1", "r2") in edges
    # No source request was made (storage hit)
    assert collector.calls == []


async def test_expand_loaded_vs_stub_nodes() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1", "r2"]
    storage.papers["r1"] = _paper("r1", "Ref One")  # full record
    graph = KnowledgeGraph()

    result = await expand_from_graph(graph, storage, FakeCollector(), "p1")

    by_id = {n["id"]: n for n in result.nodes}
    assert by_id["r1"]["loaded"] is True
    assert by_id["r2"]["loaded"] is False  # only ID known -> stub
    # Stubs are still registered in the session graph
    assert graph.has_node("r2")
    stub = graph.get_node("r2")
    assert stub is not None
    assert stub["loaded"] is False


async def test_expand_citations_storage_hit_edge_direction() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.cits["p1"] = ["c1", "c2"]
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, FakeCollector(), "p1", relations=["citations"]
    )

    edges = {(e["source"], e["target"], e["relation"]) for e in result.edges}
    assert edges == {
        ("c1", "p1", "cites"),
        ("c2", "p1", "cites"),
    }


async def test_expand_authors_relation_from_stored_paper() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper(
        "p1",
        "Alpha",
        authors=[AuthorRef(author_id="a1", name="Ada"), AuthorRef(name="Bob")],
    )
    storage.authors["a1"] = _author("a1", "Ada")
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, FakeCollector(), "p1", relations=["authors"]
    )

    by_id = {n["id"]: n for n in result.nodes}
    assert set(by_id) == {"a1", "~Bob"}
    assert by_id["a1"]["loaded"] is True
    assert by_id["~Bob"]["loaded"] is False  # unresolved name -> stub
    edges = {(e["source"], e["target"], e["relation"]) for e in result.edges}
    assert ("p1", "a1", "authored_by") in edges
    assert ("p1", "~Bob", "authored_by") in edges


async def test_expand_author_papers_relation() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    storage.author_papers["a1"] = ["p1", "p2"]
    storage.papers["p1"] = _paper("p1", "One")
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, FakeCollector(), "a1", relations=["papers"]
    )

    node_ids = {n["id"] for n in result.nodes}
    assert node_ids == {"p1", "p2"}
    edges = {(e["source"], e["target"], e["relation"]) for e in result.edges}
    assert edges == {
        ("p1", "a1", "authored_by"),
        ("p2", "a1", "authored_by"),
    }


async def test_expand_coauthors_relation_bidirectional() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    storage.coauthors["a1"] = ["a2", "a3"]
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, FakeCollector(), "a1", relations=["coauthors"]
    )

    edges = {(e["source"], e["target"], e["relation"]) for e in result.edges}
    assert edges == {
        ("a1", "a2", "coauthor_with"),
        ("a2", "a1", "coauthor_with"),
        ("a1", "a3", "coauthor_with"),
        ("a3", "a1", "coauthor_with"),
    }


async def test_expand_default_relations_for_paper() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1"]
    storage.cits["p1"] = ["c1"]
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, FakeCollector(), "p1", relations=None
    )

    node_ids = {n["id"] for n in result.nodes}
    assert node_ids == {"r1", "c1"}


# ---------------------------------------------------------------------------
# Fetch (miss) path
# ---------------------------------------------------------------------------


async def test_expand_fetches_citations_when_storage_misses() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = []  # no stored citations at all -> miss
    collector = FakeCollector()
    collector.citation_results = [
        _citation("c1", "p1"),
    ]
    collector.paper_results = [_paper("c1", "Citing One")]

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph,
        storage,
        collector,
        "p1",
        relations=["citations"],
        fetch_missing=True,
    )

    assert ("collect_citations", "p1") in collector.calls
    # Fetched data was persisted so a later expand can hit storage
    assert storage.saved_batches
    assert result.stats.fetched_new >= 1
    node_ids = {n["id"] for n in result.nodes}
    assert "c1" in node_ids
    edges = {(e["source"], e["target"], e["relation"]) for e in result.edges}
    assert ("c1", "p1", "cites") in edges


async def test_expand_fetches_references_from_paper_record() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = []
    collector = FakeCollector()
    collector.paper_results = [
        _paper("p1", "Alpha", references=["r1", "r2"]),
        _paper("r1", "Ref One"),
    ]

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"]
    )

    assert ("collect_paper", "Alpha") in collector.calls
    node_ids = {n["id"] for n in result.nodes}
    assert {"r1", "r2"} <= node_ids
    edges = {(e["source"], e["target"]) for e in result.edges}
    assert ("p1", "r1") in edges
    assert ("p1", "r2") in edges


async def test_expand_fetch_failure_counts_failed_not_raised() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = []
    collector = FakeCollector()
    collector.error_on = "collect_paper"

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"]
    )

    assert result.stats.failed == 1
    assert result.nodes == []
    assert result.stats.nodes_found == 0


async def test_expand_unknown_center_reports_failed() -> None:
    storage = FakeStorage()
    graph = KnowledgeGraph()
    result = await expand_from_graph(graph, storage, FakeCollector(), "ghost")

    assert result.stats.failed == 1
    assert result.nodes == []


# ---------------------------------------------------------------------------
# Cache hits on second expand
# ---------------------------------------------------------------------------


async def test_second_expand_hits_cache_without_refetch() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1", "r2"]
    collector = FakeCollector()

    graph = KnowledgeGraph()
    first = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"]
    )
    assert first.stats.cache_hits == 0

    second = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"]
    )
    assert second.stats.cache_hits >= 2  # r1 and r2 already in the graph
    assert second.nodes == []  # nothing new discovered
    assert collector.calls == []  # never hit the sources


async def test_fetched_citations_not_refetched_on_second_expand() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = []
    collector = FakeCollector()
    collector.citation_results = [_citation("c1", "p1")]

    graph = KnowledgeGraph()
    first = await expand_from_graph(
        graph, storage, collector, "p1", relations=["citations"]
    )
    assert first.stats.fetched_new >= 1
    collector.calls.clear()

    # Second expand: storage now returns the citations -> no collector call.
    second = await expand_from_graph(
        graph, storage, collector, "p1", relations=["citations"]
    )
    assert collector.calls == []
    assert second.stats.cache_hits > 0


async def test_second_expand_cache_hits_skip_storage_reads() -> None:
    """(I-14) Resident neighbors must not trigger ``storage.get_paper``.

    Before the fix every neighbor — cache hit or not — was materialized via
    ``_neighbor_for_paper``, which reads the full record from storage; a
    second expand with 100 cache hits still issued 100 storage reads.
    Resident nodes must be detected in-memory (``graph.has_node``) first.
    """
    storage = CountingStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = [f"r{i}" for i in range(1, 101)]
    graph = KnowledgeGraph()
    collector = FakeCollector()

    first = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"], max_nodes=200
    )
    assert first.stats.cache_hits == 0
    assert storage.get_paper_calls >= 100  # first pass reads every record

    storage.get_paper_calls = 0
    second = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"], max_nodes=200
    )
    assert second.stats.cache_hits == 100
    assert second.nodes == []  # nothing new discovered
    assert storage.get_paper_calls == 0  # no storage reads for resident nodes


# ---------------------------------------------------------------------------
# Fetch: authors / papers / coauthors / center
# ---------------------------------------------------------------------------


async def test_expand_fetches_authors_from_paper_record() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha", authors=[])
    collector = FakeCollector()
    collector.paper_results = [
        _paper(
            "p1",
            "Alpha",
            authors=[
                AuthorRef(author_id="a1", name="Ada"),
                AuthorRef(name="Bob"),
            ],
        )
    ]
    collector.author_results = [_author("a1", "Ada")]

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["authors"]
    )

    assert ("collect_paper", "Alpha") in collector.calls
    by_id = {n["id"]: n for n in result.nodes}
    assert set(by_id) == {"a1", "~Bob"}
    assert by_id["a1"]["loaded"] is True
    assert by_id["~Bob"]["loaded"] is False
    assert result.stats.fetched_new == 2


async def test_expand_fetches_papers_for_author() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    collector = FakeCollector()
    collector.author_papers_results = [_paper("p1", "One"), _paper("p2", "Two")]

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "a1", relations=["papers"]
    )

    assert ("collect_author_papers", "Ada") in collector.calls
    assert {n["id"] for n in result.nodes} == {"p1", "p2"}
    assert result.stats.fetched_new == 2
    edges = {(e["source"], e["target"], e["relation"]) for e in result.edges}
    assert edges == {
        ("p1", "a1", "authored_by"),
        ("p2", "a1", "authored_by"),
    }


async def test_expand_coauthors_derived_from_stored_papers() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    storage.author_papers["a1"] = ["p1"]
    storage.papers["p1"] = _paper(
        "p1",
        "One",
        authors=[AuthorRef(author_id="a1", name="Ada"), AuthorRef(author_id="a2", name="Bob")],
    )
    storage.authors["a2"] = _author("a2", "Bob")
    collector = FakeCollector()

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "a1", relations=["coauthors"]
    )

    assert collector.calls == []
    assert {n["id"] for n in result.nodes} == {"a2"}
    assert result.stats.fetched_new == 1
    edges = {(e["source"], e["target"], e["relation"]) for e in result.edges}
    assert edges == {
        ("a1", "a2", "coauthor_with"),
        ("a2", "a1", "coauthor_with"),
    }


async def test_expand_coauthors_fetched_via_collector() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    collector = FakeCollector()
    collector.author_papers_results = [
        _paper(
            "p1",
            "One",
            authors=[AuthorRef(author_id="a1", name="Ada"), AuthorRef(author_id="a2", name="Bob")],
        )
    ]

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "a1", relations=["coauthors"]
    )

    assert ("collect_author_papers", "Ada") in collector.calls
    assert {n["id"] for n in result.nodes} == {"a2"}
    assert result.stats.fetched_new == 1


async def test_expand_fetches_unknown_center_via_collector() -> None:
    storage = FakeStorage()
    collector = FakeCollector()
    collector.paper_results = [_paper("c1", "Collected Center")]

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "c1", relations=[]
    )

    assert ("collect_paper", "c1") in collector.calls
    assert result.stats.failed == 0
    assert graph.has_node("c1")
    center = graph.get_node("c1")
    assert center is not None
    assert center["type"] == "paper"


async def test_expand_center_fetch_failure_reports_failed() -> None:
    storage = FakeStorage()
    collector = FakeCollector()
    collector.error_on = "collect_paper"

    graph = KnowledgeGraph()
    result = await expand_from_graph(graph, storage, collector, "ghost")

    assert result.stats.failed == 1
    assert result.nodes == []


# ---------------------------------------------------------------------------
# Fetch failures and no-fetch behavior
# ---------------------------------------------------------------------------


async def test_expand_no_fetch_missing_returns_empty() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")  # no relations stored at all
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph,
        storage,
        FakeCollector(),
        "p1",
        relations=["references", "citations", "authors"],
        fetch_missing=False,
    )

    assert result.nodes == []
    assert result.edges == []
    assert result.stats.nodes_found == 0


async def test_expand_fetch_authors_failure_counts_failed() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha", authors=[])
    collector = FakeCollector()
    collector.error_on = "collect_paper"

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["authors"]
    )

    assert result.stats.failed == 1
    assert result.nodes == []


async def test_expand_fetch_papers_failure_counts_failed() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    collector = FakeCollector()
    collector.error_on = "collect_author_papers"

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "a1", relations=["papers"]
    )

    assert result.stats.failed == 1
    assert result.nodes == []


async def test_expand_fetch_coauthors_failure_counts_failed() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    collector = FakeCollector()
    collector.error_on = "collect_author_papers"

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "a1", relations=["coauthors"]
    )

    assert result.stats.failed == 1
    assert result.nodes == []


async def test_expand_references_fetch_without_reference_capability_fails() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    collector = FakeCollector()
    collector.paper_results = [_paper("p1", "Alpha")]  # record without references

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"]
    )

    # The fetch succeeded but no adapter exposed references -> counted as failed
    assert result.stats.failed == 1
    assert result.nodes == []


async def test_expand_citations_fetch_with_unmatched_citations_fails() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    collector = FakeCollector()
    collector.citation_results = [_citation("x1", "other")]  # unrelated paper

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["citations"]
    )

    assert result.stats.failed == 1
    assert result.nodes == []


async def test_expand_references_fetch_skips_self_reference() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    collector = FakeCollector()
    collector.paper_results = [
        _paper("p1", "Alpha", references=["p1", "r1"])  # self-reference ignored
    ]

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"]
    )

    assert {n["id"] for n in result.nodes} == {"r1"}
    assert result.stats.fetched_new == 1


async def test_expand_fetch_papers_skips_paper_without_id() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    collector = FakeCollector()
    collector.author_papers_results = [Paper(title="No ID Yet")]

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "a1", relations=["papers"]
    )

    assert result.stats.failed == 1
    assert result.nodes == []


# ---------------------------------------------------------------------------
# Same-pass deduplication and relation filtering
# ---------------------------------------------------------------------------


async def test_same_node_reachable_via_two_relations_added_once() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["x"]
    storage.cits["p1"] = ["x"]
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph,
        storage,
        FakeCollector(),
        "p1",
        relations=["references", "citations"],
    )

    assert result.stats.nodes_found == 1
    assert {n["id"] for n in result.nodes} == {"x"}
    # Two distinct edges are still recorded
    assert result.stats.edges_found == 2


async def test_relations_not_applicable_to_entity_type_are_filtered() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    graph = KnowledgeGraph()

    # "references" is a paper-only relation -> filtered out for an author
    result = await expand_from_graph(
        graph, storage, FakeCollector(), "a1", relations=["references"]
    )

    assert result.nodes == []
    assert graph.has_node("a1")


# ---------------------------------------------------------------------------
# Depth and node-count truncation
# ---------------------------------------------------------------------------


async def test_depth_control() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["p2"]
    storage.papers["p2"] = _paper("p2", "Beta")
    storage.refs["p2"] = ["p3"]
    storage.papers["p3"] = _paper("p3", "Gamma")

    result = await expand_from_graph(
        KnowledgeGraph(), storage, FakeCollector(), "p1", depth=1
    )
    assert {n["id"] for n in result.nodes} == {"p2"}
    assert result.stats.depth_reached == 1

    result = await expand_from_graph(
        KnowledgeGraph(), storage, FakeCollector(), "p1", depth=2
    )
    assert {n["id"] for n in result.nodes} == {"p2", "p3"}
    assert result.stats.depth_reached == 2


async def test_max_nodes_truncation() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = [f"r{i}" for i in range(1, 11)]
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, FakeCollector(), "p1", max_nodes=3
    )

    assert result.stats.nodes_found == 3
    assert result.stats.truncated is True


async def test_depth_clamped_to_max_expand_depth() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["p2"]
    storage.papers["p2"] = _paper("p2", "Beta")
    storage.refs["p2"] = ["p3"]
    storage.papers["p3"] = _paper("p3", "Gamma")
    storage.refs["p3"] = ["p4"]
    storage.papers["p4"] = _paper("p4", "Delta")
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, FakeCollector(), "p1", depth=99, max_depth=3
    )

    assert result.stats.depth_reached <= 3
    assert {n["id"] for n in result.nodes} == {"p2", "p3", "p4"}


# ---------------------------------------------------------------------------
# Relations / type plumbing
# ---------------------------------------------------------------------------


async def test_invalid_relation_name_raises() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    graph = KnowledgeGraph()
    with pytest.raises(ValueError):
        await expand_from_graph(
            graph, storage, FakeCollector(), "p1", relations=["bogus"]
        )


async def test_expand_center_added_to_graph() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1"]
    graph = KnowledgeGraph()

    await expand_from_graph(graph, storage, FakeCollector(), "p1")

    assert graph.has_node("p1")
    center_node = graph.get_node("p1")
    assert center_node is not None
    assert center_node["loaded"] is True
    assert center_node["type"] == "paper"


async def test_accepts_real_collector_protocol() -> None:
    """Traversal accepts a real MultiSourceCollector instance (no crash)."""
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1"]
    graph = KnowledgeGraph()
    collector = MultiSourceCollector(config={"sources": ["openalex"]})

    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"]
    )

    assert result.stats.nodes_found == 1


# ---------------------------------------------------------------------------
# FIX-H F1 (H1): truncated passes must not leave ghost edges
# ---------------------------------------------------------------------------


async def test_truncation_leaves_no_ghost_edges() -> None:
    """H1: when the node budget is exhausted mid-pass, the edge that would
    point at the un-added node must not be written.

    The pre-fix code added the edge *before* the budget check, so a truncated
    pass left ``graph_edge_count > graph_node_count`` with an edge whose
    target was never registered as a node (P25: edges_found=51 > nodes_found=50
    with ghost edge ``C0 -> W-ref-51``).
    """
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = [f"r{i:02d}" for i in range(1, 61)]  # 60 candidates
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, FakeCollector(), "p1", relations=["references"], max_nodes=50
    )

    assert result.stats.nodes_found == 50
    assert result.stats.truncated is True
    # Every recorded edge must point at nodes that are actually in the graph.
    for edge in graph.edges():
        assert graph.has_node(edge["source"]), f"ghost source {edge['source']}"
        assert graph.has_node(edge["target"]), f"ghost target {edge['target']}"
    # edges_found must match the real graph edge count (no dangling edge).
    assert result.stats.edges_found == graph.number_of_edges()
    assert result.stats.edges_found == result.stats.nodes_found


# ---------------------------------------------------------------------------
# FIX-H F2 (H3): same-session expand can deepen below the achieved depth
# ---------------------------------------------------------------------------


async def test_same_session_expand_deepens_below_achieved_depth() -> None:
    """H3: ``expand(depth=1)`` followed by ``expand(depth=3)`` in the same
    session must complete levels 2/3 instead of being a no-op.

    The pre-fix BFS only drilled along nodes newly discovered in the current
    pass, so resident level-1 neighbours never entered the next frontier and
    the second call reported ``nodes_found=0, depth_reached=1``.
    """
    storage = FakeStorage()
    storage.papers["c0"] = _paper("c0", "Chain 0")
    storage.cits["c0"] = ["c1"]
    storage.cits["c1"] = ["c2"]
    storage.cits["c2"] = ["c3"]
    graph = KnowledgeGraph()

    first = await expand_from_graph(
        graph, storage, FakeCollector(), "c0", relations=["citations"], depth=1
    )
    assert {n["id"] for n in first.nodes} == {"c1"}
    assert first.stats.depth_reached == 1

    second = await expand_from_graph(
        graph, storage, FakeCollector(), "c0", relations=["citations"], depth=3
    )
    assert second.stats.depth_reached == 3
    assert {n["id"] for n in second.nodes} == {"c2", "c3"}
    assert second.stats.cache_hits >= 1  # resident c1 still counts as a hit


async def test_same_session_expand_depth_at_or_below_achieved_is_noop() -> None:
    """H3: a second pass whose depth does not exceed the achieved depth stays
    a pure cache-hit pass (default ``depth=1`` must not re-expand residents)."""
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1", "r2"]
    graph = KnowledgeGraph()

    first = await expand_from_graph(
        graph, storage, FakeCollector(), "p1", relations=["references"], depth=2
    )
    assert {n["id"] for n in first.nodes} == {"r1", "r2"}
    assert first.stats.depth_reached == 2

    second = await expand_from_graph(
        graph, storage, FakeCollector(), "p1", relations=["references"], depth=2
    )
    assert second.nodes == []
    assert second.stats.cache_hits == 2


# ---------------------------------------------------------------------------
# FIX-H F3 (H4): expand refreshes resident node attributes from storage
# ---------------------------------------------------------------------------


async def test_expand_refreshes_resident_center_title_from_storage() -> None:
    """H4: after the stored record of a resident node changes, a same-session
    expand must refresh the session-graph attributes (title) instead of
    keeping the stale value (P25 V2.1: center kept "Old Title")."""
    storage = FakeStorage()
    storage.papers["p1"] = _paper(
        "p1", "Old Title", authors=[AuthorRef(author_id="a1", name="Ada")]
    )
    storage.refs["p1"] = ["r1"]
    graph = KnowledgeGraph()
    collector = FakeCollector()

    await expand_from_graph(graph, storage, collector, "p1")
    node = graph.get_node("p1")
    assert node is not None
    assert node["title"] == "Old Title"

    # Storage record is updated between passes (incremental flow).
    storage.papers["p1"] = _paper(
        "p1", "New Title", authors=[AuthorRef(author_id="a1", name="Ada")]
    )
    second = await expand_from_graph(graph, storage, collector, "p1")
    assert second.stats.cache_hits >= 1  # r1 resident
    refreshed = graph.get_node("p1")
    assert refreshed is not None
    assert refreshed["title"] == "New Title"


async def test_expand_refreshes_resident_author_name_from_storage() -> None:
    """H4: the same refresh applies to author nodes (name attribute)."""
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada Old")
    storage.author_papers["a1"] = ["p1"]
    storage.papers["p1"] = _paper(
        "p1",
        "One",
        authors=[
            AuthorRef(author_id="a1", name="Ada Old"),
            AuthorRef(author_id="a2", name="Bob"),
        ],
    )
    graph = KnowledgeGraph()

    await expand_from_graph(
        graph, storage, FakeCollector(), "a1", relations=["coauthors"]
    )
    node = graph.get_node("a1")
    assert node is not None
    assert node["name"] == "Ada Old"

    storage.authors["a1"] = _author("a1", "Ada New")
    await expand_from_graph(
        graph, storage, FakeCollector(), "a1", relations=["coauthors"]
    )
    refreshed = graph.get_node("a1")
    assert refreshed is not None
    assert refreshed["name"] == "Ada New"


# ---------------------------------------------------------------------------
# FIX-R F1 (R2): ExpandStats.failures carries per-relation failure reasons
# ---------------------------------------------------------------------------


async def test_expand_failure_records_reason_in_stats_failures() -> None:
    """R2: a failed relation fetch must surface the exception message in
    ``stats.failures`` (not just the ``failed`` count)."""
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = []
    collector = FakeCollector()
    collector.error_on = "collect_paper"

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "p1", relations=["references"]
    )

    assert result.stats.failed == 1
    assert len(result.stats.failures) == 1
    assert "source unavailable" in result.stats.failures[0]


async def test_expand_success_leaves_failures_empty() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1"]
    storage.papers["r1"] = _paper("r1", "Ref One")
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, FakeCollector(), "p1", relations=["references"]
    )

    assert result.stats.failed == 0
    assert result.stats.failures == []


async def test_expand_failure_count_matches_failures_length() -> None:
    """R2: every counted failure has a corresponding reason (``len(failures)
    == failed``), so integrators can trust the count and the reasons stay in
    lockstep."""
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    collector = FakeCollector()
    collector.error_on = "collect_author_papers"

    graph = KnowledgeGraph()
    result = await expand_from_graph(
        graph, storage, collector, "a1", relations=["papers", "coauthors"]
    )

    assert result.stats.failed == 2
    assert len(result.stats.failures) == result.stats.failed
    assert all("source unavailable" in msg for msg in result.stats.failures)


async def test_expand_unpersisted_center_reports_reason() -> None:
    """R2 (P36): expanding an entity that was collected but never persisted
    must surface a reason for the ``failed=1`` pass instead of staying
    opaque (0 nodes, failed=1, no explanation)."""
    storage = FakeStorage()
    collector = FakeCollector()  # collect_paper returns nothing for the id
    graph = KnowledgeGraph()

    result = await expand_from_graph(
        graph, storage, collector, "a1", relations=["papers"]
    )

    assert result.stats.failed == 1
    assert len(result.stats.failures) == 1
    assert "a1" in result.stats.failures[0]
