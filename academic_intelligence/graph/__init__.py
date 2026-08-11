"""Graph layer: session knowledge graph, expansion traversal and LRU cache.

The graph layer (3A v2 design §7) provides:

- :class:`KnowledgeGraph` — a pure-Python (networkx-free) session-level
  directed graph of paper/author entities and their relationships, bounded by
  an LRU node cache (``Config.graph_cache_size``).
- :func:`expand_from_graph` — recursive/lazy expansion from any entity with
  storage-first lookup, source fetching on miss, depth and node-count
  truncation, and placeholder (``loaded=False``) stub nodes.
- :class:`~academic_intelligence.core.models.ExpandResult` /
  :class:`~academic_intelligence.core.models.ExpandStats` — the expand
  result container and per-pass statistics.

Public API::

    from academic_intelligence import AcademicIntelligence, ExpandResult
"""

from __future__ import annotations

from academic_intelligence.core.models import ExpandResult, ExpandStats
from academic_intelligence.graph.cache import GraphCache
from academic_intelligence.graph.knowledge_graph import KnowledgeGraph
from academic_intelligence.graph.traversal import expand_from_graph

__all__ = [
    "KnowledgeGraph",
    "ExpandResult",
    "ExpandStats",
    "GraphCache",
    "expand_from_graph",
]
