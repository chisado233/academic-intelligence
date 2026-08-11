"""Session-level knowledge graph.

A pure-Python directed graph (no networkx) holding paper/author entities and
their relationships:

- nodes = paper / author entities, carrying ``type``, ``loaded`` (True for
  full records, False for placeholder stubs) plus optional attributes
  (``title`` / ``name`` / ``year`` ...)
- edges = ``cites`` (paper -> paper), ``authored_by`` (paper -> author) and
  ``coauthor_with`` (author <-> author, stored as two directed edges)

The node store is backed by :class:`GraphCache`, an LRU cache bounded by
``Config.graph_cache_size``; when the graph exceeds its capacity the
least-recently used node is evicted together with its incident edges.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from academic_intelligence.graph.cache import GraphCache

_NodeKey = str
_EdgeKey = tuple[str, str]
SNAPSHOT_VERSION = 1


class KnowledgeGraph:
    """Session-level knowledge graph of academic entities and relations.

    Args:
        cache_size: Maximum number of nodes resident in the session graph
            (defaults to ``Config.graph_cache_size`` = 5000).  When exceeded,
            the least-recently used node and its incident edges are evicted.
    """

    def __init__(self, cache_size: int = 5000) -> None:
        self._nodes: dict[_NodeKey, dict[str, Any]] = {}
        self._edges: dict[_EdgeKey, dict[str, Any]] = {}
        self._cache = GraphCache(max_size=cache_size, on_evict=self._evict_node)

    # ------------------------------------------------------------------
    # Node management
    # ------------------------------------------------------------------

    def add_node(self, entity_id: str, type: str, loaded: bool = True, **attrs: Any) -> dict[str, Any]:
        """Add or refresh a node.

        Args:
            entity_id: Unique entity identifier (paper or author id).
            type: ``"paper"`` or ``"author"``.
            loaded: ``True`` for full records, ``False`` for placeholder stubs.
            **attrs: Extra node attributes (``title`` / ``name`` / ``year`` ...).

        Returns:
            The stored node dictionary.
        """
        node: dict[str, Any] = {
            "id": entity_id,
            "type": type,
            "loaded": bool(loaded),
            **attrs,
        }
        self._nodes[entity_id] = node
        self._cache.put(entity_id, node)
        return node

    def get_node(self, entity_id: str) -> dict[str, Any] | None:
        """Return the node dict for *entity_id*, or ``None`` if absent."""
        return self._cache.get(entity_id)

    def has_node(self, entity_id: str) -> bool:
        """Return whether *entity_id* is currently resident in the graph."""
        return self._cache.has(entity_id)

    def nodes(self) -> list[dict[str, Any]]:
        """Return all resident nodes as dicts."""
        return list(self._nodes.values())

    def number_of_nodes(self) -> int:
        """Return the number of resident nodes."""
        return len(self._nodes)

    def _evict_node(self, entity_id: str, _value: Any) -> None:
        """Drop an evicted node and its incident edges (LRU capacity)."""
        self._nodes.pop(entity_id, None)
        stale = [key for key in self._edges if entity_id in key]
        for key in stale:
            del self._edges[key]

    # ------------------------------------------------------------------
    # Edge management
    # ------------------------------------------------------------------

    def add_edge(self, source: str, target: str, relation: str, **attrs: Any) -> dict[str, Any]:
        """Add or refresh a directed edge.

        Args:
            source: Source entity id.
            target: Target entity id.
            relation: ``"cites"`` / ``"authored_by"`` / ``"coauthor_with"``.
            **attrs: Extra edge attributes.

        Returns:
            The stored edge dictionary.
        """
        edge: dict[str, Any] = {
            "source": source,
            "target": target,
            "relation": relation,
            **attrs,
        }
        self._edges[(source, target)] = edge
        return edge

    def get_neighbors(self, entity_id: str) -> list[dict[str, Any]]:
        """Return outgoing neighbors of *entity_id*.

        Each entry is ``{"id": <neighbor>, "relation": <edge relation>,
        "type": <neighbor node type>}``.
        """
        result: list[dict[str, Any]] = []
        for (source, target), edge in self._edges.items():
            if source != entity_id:
                continue
            neighbor = self._nodes.get(target)
            result.append(
                {
                    "id": target,
                    "relation": edge.get("relation", ""),
                    "type": neighbor.get("type") if neighbor else None,
                }
            )
        # Stable ordering by neighbor id
        result.sort(key=lambda d: d["id"])
        return result

    def has_edge(self, source: str, target: str) -> bool:
        """Return whether a directed edge *source* -> *target* exists."""
        return (source, target) in self._edges

    def edges(self) -> list[dict[str, Any]]:
        """Return all resident edges as dicts."""
        return list(self._edges.values())

    def number_of_edges(self) -> int:
        """Return the number of resident edges."""
        return len(self._edges)

    # ------------------------------------------------------------------
    # Traversal helpers
    # ------------------------------------------------------------------

    def to_subgraph(self, center_id: str, radius: int = 2) -> KnowledgeGraph:
        """Return a new graph with nodes within *radius* of *center_id*.

        Reachability is undirected (edges are followed in both directions),
        which matches the ego-graph semantics of ``subgraph(center, radius)``.

        Returns:
            A new :class:`KnowledgeGraph` with the reachable nodes/edges, or
            an empty graph when the center is not resident.
        """
        sub = KnowledgeGraph(cache_size=self._cache.max_size)
        if not self.has_node(center_id):
            return sub

        reachable = self._bfs_undirected(center_id, radius)
        for node_id in reachable:
            node = self._nodes.get(node_id)
            if node is not None:
                # Keep the ``loaded`` flag (FIX-E-3): placeholder stubs must
                # stay distinguishable from full records on export, matching
                # the ExpandResult node contract (default would re-mark
                # everything loaded=True).
                sub.add_node(node_id, node.get("type", "paper"), **{
                    k: v for k, v in node.items() if k not in {"id", "type"}
                })
        for key, edge in self._edges.items():
            if key[0] in reachable and key[1] in reachable:
                sub.add_edge(
                    key[0],
                    key[1],
                    edge.get("relation", ""),
                    **{
                        k: v
                        for k, v in edge.items()
                        if k not in {"source", "target", "relation"}
                    },
                )
        return sub

    def shortest_path(self, source_id: str, target_id: str) -> list[str]:
        """Return the shortest directed path (BFS) from *source_id* to *target_id*.

        Returns a list of entity ids including both endpoints, or ``[]`` when
        no directed path exists (or either endpoint is missing).
        """
        if not self.has_node(source_id) or not self.has_node(target_id):
            return []
        if source_id == target_id:
            return [source_id]

        previous: dict[str, str | None] = {source_id: None}
        queue = deque([source_id])
        while queue:
            current = queue.popleft()
            if current == target_id:
                break
            for neighbor in self._outgoing_ids(current):
                if neighbor not in previous:
                    previous[neighbor] = current
                    queue.append(neighbor)

        if target_id not in previous:
            return []
        path: list[str] = []
        node: str | None = target_id
        while node is not None:
            path.append(node)
            node = previous[node]
        path.reverse()
        return path

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def export_json(self) -> dict[str, Any]:
        """Serialize the whole graph to a JSON-compatible dictionary."""
        return {
            "directed": True,
            "nodes": self.nodes(),
            "edges": self.edges(),
            "node_count": self.number_of_nodes(),
            "edge_count": self.number_of_edges(),
        }

    def save_snapshot(self, path: str | os.PathLike[str]) -> None:
        """Atomically persist the complete graph as a versioned JSON snapshot."""
        target = Path(path)
        temporary = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex}")
        payload = {"version": SNAPSHOT_VERSION, **self.export_json()}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def load_snapshot(
        cls,
        path: str | os.PathLike[str],
        *,
        cache_size: int | None = None,
    ) -> KnowledgeGraph:
        """Load and validate a graph snapshot created by :meth:`save_snapshot`."""
        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not load graph snapshot {source.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("graph snapshot must be a JSON object")
        version = payload.get("version")
        if type(version) is not int or version != SNAPSHOT_VERSION:
            raise ValueError(
                f"unsupported graph snapshot version {version!r}; "
                f"expected {SNAPSHOT_VERSION}"
            )
        nodes = payload.get("nodes")
        edges = payload.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("graph snapshot nodes and edges must be arrays")
        node_count = payload.get("node_count")
        edge_count = payload.get("edge_count")
        if type(node_count) is not int or node_count != len(nodes):
            raise ValueError(
                f"graph snapshot node_count must equal {len(nodes)}, got {node_count!r}"
            )
        if type(edge_count) is not int or edge_count != len(edges):
            raise ValueError(
                f"graph snapshot edge_count must equal {len(edges)}, got {edge_count!r}"
            )

        graph = cls(cache_size=max(cache_size or 5000, len(nodes), 1))
        node_ids: set[str] = set()
        for raw_node in nodes:
            if not isinstance(raw_node, dict):
                raise ValueError("graph snapshot contains an invalid node")
            entity_id = raw_node.get("id")
            entity_type = raw_node.get("type")
            if not isinstance(entity_id, str) or not entity_id:
                raise ValueError("graph snapshot node id must be a non-empty string")
            if not isinstance(entity_type, str) or not entity_type:
                raise ValueError("graph snapshot node type must be a non-empty string")
            if entity_id in node_ids:
                raise ValueError(f"graph snapshot contains duplicate node id {entity_id!r}")
            attrs = {k: v for k, v in raw_node.items() if k not in {"id", "type"}}
            graph.add_node(entity_id, type=entity_type, **attrs)
            node_ids.add(entity_id)

        edge_ids: set[tuple[str, str]] = set()
        for raw_edge in edges:
            if not isinstance(raw_edge, dict):
                raise ValueError("graph snapshot contains an invalid edge")
            source_id = raw_edge.get("source")
            target_id = raw_edge.get("target")
            relation = raw_edge.get("relation")
            if not isinstance(source_id, str) or not source_id:
                raise ValueError("graph snapshot edge source must be a non-empty string")
            if not isinstance(target_id, str) or not target_id:
                raise ValueError("graph snapshot edge target must be a non-empty string")
            if source_id not in node_ids or target_id not in node_ids:
                raise ValueError("graph snapshot edge references a missing node")
            if not isinstance(relation, str) or not relation:
                raise ValueError("graph snapshot edge relation must be a non-empty string")
            edge_id = (source_id, target_id)
            if edge_id in edge_ids:
                raise ValueError(
                    f"graph snapshot contains duplicate edge {source_id!r}->{target_id!r}"
                )
            edge_ids.add(edge_id)
            edge_attrs = {
                k: v
                for k, v in raw_edge.items()
                if k not in {"source", "target", "relation"}
            }
            graph.add_edge(str(source_id), str(target_id), relation, **edge_attrs)
        return graph

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _outgoing_ids(self, entity_id: str) -> list[str]:
        return [target for (source, target) in self._edges if source == entity_id]

    def _bfs_undirected(self, start: str, radius: int) -> list[str]:
        visited: dict[str, int] = {start: 0}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            distance = visited[current]
            if distance >= radius:
                continue
            for neighbor in self._all_neighbor_ids(current):
                if neighbor not in visited:
                    visited[neighbor] = distance + 1
                    queue.append(neighbor)
        return list(visited.keys())

    def _all_neighbor_ids(self, entity_id: str) -> Iterator[str]:
        for (source, target) in self._edges:
            if source == entity_id:
                yield target
            elif target == entity_id:
                yield source
