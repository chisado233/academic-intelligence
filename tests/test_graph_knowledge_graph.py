"""Unit tests for the graph layer: KnowledgeGraph and GraphCache.

These tests cover the pure-Python session graph (no networkx dependency):
node/edge management, neighbor queries, subgraph extraction, shortest path,
JSON export, stub (placeholder) nodes, and LRU cache eviction.
"""

from __future__ import annotations

from academic_intelligence.graph import KnowledgeGraph
from academic_intelligence.graph.cache import GraphCache


def _build_chain(size: int = 5) -> KnowledgeGraph:
    """Build a directed chain n1 -> n2 -> ... -> n{size} of paper nodes."""
    g = KnowledgeGraph()
    ids = [f"n{i}" for i in range(1, size + 1)]
    for i in ids:
        g.add_node(i, type="paper", title=f"Paper {i}", year=2020 + int(i[1:]))
    for a, b in zip(ids, ids[1:], strict=False):
        g.add_edge(a, b, relation="cites")
    return g


# ---------------------------------------------------------------------------
# Node / edge management
# ---------------------------------------------------------------------------


def test_add_node_and_has_node() -> None:
    g = KnowledgeGraph()
    g.add_node("p1", type="paper", title="Alpha", year=2020)
    assert g.has_node("p1")
    assert not g.has_node("missing")
    assert g.number_of_nodes() == 1


def test_add_node_default_loaded_true() -> None:
    g = KnowledgeGraph()
    g.add_node("p1", type="paper", title="Alpha")
    node = g.get_node("p1")
    assert node is not None
    assert node["loaded"] is True


def test_stub_node_loaded_false() -> None:
    g = KnowledgeGraph()
    g.add_node("r1", type="paper", loaded=False)
    node = g.get_node("r1")
    assert node is not None
    assert node["loaded"] is False
    assert node["type"] == "paper"
    assert node["id"] == "r1"


def test_add_edge_and_get_neighbors() -> None:
    g = KnowledgeGraph()
    g.add_node("p1", type="paper")
    g.add_node("p2", type="paper")
    g.add_edge("p1", "p2", relation="cites")
    assert g.number_of_edges() == 1
    neighbors = g.get_neighbors("p1")
    assert [n["id"] for n in neighbors] == ["p2"]
    assert neighbors[0]["relation"] == "cites"


def test_get_neighbors_empty() -> None:
    g = KnowledgeGraph()
    g.add_node("p1", type="paper")
    assert g.get_neighbors("p1") == []
    assert g.get_neighbors("missing") == []


def test_has_edge() -> None:
    g = KnowledgeGraph()
    g.add_node("p1", type="paper")
    g.add_node("p2", type="paper")
    g.add_edge("p1", "p2", relation="cites")
    assert g.has_edge("p1", "p2")
    assert not g.has_edge("p2", "p1")
    assert not g.has_edge("p1", "p3")


def test_coauthor_with_bidirectional_edges() -> None:
    g = KnowledgeGraph()
    g.add_node("a1", type="author")
    g.add_node("a2", type="author")
    g.add_edge("a1", "a2", relation="coauthor_with")
    g.add_edge("a2", "a1", relation="coauthor_with")
    assert sorted(n["id"] for n in g.get_neighbors("a1")) == ["a2"]
    assert sorted(n["id"] for n in g.get_neighbors("a2")) == ["a1"]
    assert g.number_of_edges() == 2


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------


def test_export_json_structure() -> None:
    g = _build_chain(size=3)
    payload = g.export_json()
    assert payload["directed"] is True
    assert payload["node_count"] == 3
    assert payload["edge_count"] == 2
    node_ids = {n["id"] for n in payload["nodes"]}
    assert node_ids == {"n1", "n2", "n3"}
    for node in payload["nodes"]:
        assert "type" in node
        assert "loaded" in node
    edges = payload["edges"]
    assert {"source": "n1", "target": "n2", "relation": "cites"} in edges


def test_export_json_empty_graph() -> None:
    g = KnowledgeGraph()
    payload = g.export_json()
    assert payload["nodes"] == []
    assert payload["edges"] == []
    assert payload["node_count"] == 0


# ---------------------------------------------------------------------------
# Subgraph extraction
# ---------------------------------------------------------------------------


def test_to_subgraph_radius_2_includes_all_chain_nodes() -> None:
    g = _build_chain(size=5)
    sg = g.to_subgraph("n3", radius=2)
    assert {n["id"] for n in sg.nodes()} == {"n1", "n2", "n3", "n4", "n5"}
    assert sg.number_of_edges() == 4


def test_to_subgraph_radius_1() -> None:
    g = _build_chain(size=5)
    sg = g.to_subgraph("n3", radius=1)
    assert {n["id"] for n in sg.nodes()} == {"n2", "n3", "n4"}
    assert sg.number_of_edges() == 2


def test_to_subgraph_radius_0_is_center_only() -> None:
    g = _build_chain(size=5)
    sg = g.to_subgraph("n3", radius=0)
    assert {n["id"] for n in sg.nodes()} == {"n3"}
    assert sg.number_of_edges() == 0


def test_to_subgraph_missing_center_is_empty() -> None:
    g = _build_chain(size=5)
    sg = g.to_subgraph("missing", radius=2)
    assert sg.number_of_nodes() == 0


# ---------------------------------------------------------------------------
# Shortest path (BFS)
# ---------------------------------------------------------------------------


def test_shortest_path_along_chain() -> None:
    g = _build_chain(size=5)
    assert g.shortest_path("n1", "n5") == ["n1", "n2", "n3", "n4", "n5"]


def test_shortest_path_does_not_walk_backwards() -> None:
    g = _build_chain(size=5)
    # Edges point n1 -> n2 -> ...; there is no path from n5 back to n1.
    assert g.shortest_path("n5", "n1") == []


def test_shortest_path_source_equals_target() -> None:
    g = _build_chain(size=3)
    assert g.shortest_path("n2", "n2") == ["n2"]


def test_shortest_path_missing_node() -> None:
    g = _build_chain(size=3)
    assert g.shortest_path("n1", "ghost") == []


# ---------------------------------------------------------------------------
# LRU graph cache
# ---------------------------------------------------------------------------


def test_graph_cache_basic_put_get() -> None:
    cache = GraphCache(max_size=10)
    cache.put("a", {"id": "a"})
    assert cache.has("a")
    assert cache.get("a") == {"id": "a"}
    assert len(cache) == 1
    assert cache.get("missing") is None


def test_graph_cache_evicts_least_recently_used() -> None:
    cache = GraphCache(max_size=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # touch "a" -> "b" becomes LRU
    cache.put("c", 3)
    assert not cache.has("b")
    assert cache.has("a")
    assert cache.has("c")


def test_graph_cache_eviction_callback() -> None:
    evicted: list[str] = []
    cache = GraphCache(max_size=2, on_evict=lambda k, v: evicted.append(str(k)))
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert evicted == ["a"]


def test_graph_cache_peek_and_contains() -> None:
    cache = GraphCache(max_size=2)
    cache.put("a", 1)
    assert cache.peek("a") == 1
    assert cache.has("a")
    assert "a" in cache
    assert cache.peek("missing") is None


def test_graph_cache_put_refreshes_value_and_recency() -> None:
    cache = GraphCache(max_size=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)  # refresh "a"
    cache.put("c", 3)  # evicts "b" (LRU), not "a"
    assert cache.has("a")
    assert cache.get("a") == 10
    assert not cache.has("b")


def test_graph_cache_pop_invokes_callback() -> None:
    evicted: list[str] = []
    cache = GraphCache(max_size=2, on_evict=lambda k, v: evicted.append(str(k)))
    cache.put("a", 1)
    assert cache.pop("a") == 1
    assert cache.pop("missing") is None
    assert evicted == ["a"]
    assert not cache.has("a")


def test_graph_cache_clear_invokes_callback() -> None:
    evicted: list[str] = []
    cache = GraphCache(max_size=10, on_evict=lambda k, v: evicted.append(str(k)))
    cache.put("a", 1)
    cache.put("b", 2)
    cache.clear()
    assert len(cache) == 0
    assert set(evicted) == {"a", "b"}


def test_graph_cache_items() -> None:
    cache = GraphCache(max_size=10)
    cache.put("a", 1)
    cache.put("b", 2)
    assert list(cache.items()) == [("a", 1), ("b", 2)]


def test_knowledge_graph_capacity_evicts_oldest_node() -> None:
    g = KnowledgeGraph(cache_size=3)
    g.add_node("n1", type="paper")
    g.add_node("n2", type="paper")
    g.add_node("n3", type="paper")
    g.add_node("n4", type="paper")
    assert g.number_of_nodes() == 3
    assert not g.has_node("n1")
    assert g.has_node("n4")


def test_knowledge_graph_eviction_drops_edges() -> None:
    g = KnowledgeGraph(cache_size=2)
    g.add_node("a", type="paper")
    g.add_node("b", type="paper")
    g.add_edge("a", "b", relation="cites")
    g.add_node("c", type="paper")
    g.add_edge("b", "c", relation="cites")  # evicts "a"
    assert not g.has_node("a")
    assert g.number_of_edges() == 1


def test_knowledge_graph_default_capacity() -> None:
    g = KnowledgeGraph()
    assert g.number_of_nodes() == 0
    g.add_node("x", type="paper")
    assert g.has_node("x")
