"""Strict validation tests for persisted graph snapshots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from academic_intelligence.graph import KnowledgeGraph


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "version": 1,
                "node_count": 2,
                "edge_count": 0,
                "nodes": [{"id": "p1", "type": "paper"}],
                "edges": [],
            },
            "node_count",
        ),
        (
            {
                "version": 1,
                "node_count": 1,
                "edge_count": 1,
                "nodes": [{"id": "p1", "type": "paper"}],
                "edges": [],
            },
            "edge_count",
        ),
        (
            {
                "version": 1,
                "node_count": 2,
                "edge_count": 0,
                "nodes": [
                    {"id": "p1", "type": "paper"},
                    {"id": "p1", "type": "author"},
                ],
                "edges": [],
            },
            "duplicate node",
        ),
        (
            {
                "version": 1,
                "node_count": 2,
                "edge_count": 2,
                "nodes": [
                    {"id": "p1", "type": "paper"},
                    {"id": "p2", "type": "paper"},
                ],
                "edges": [
                    {"source": "p1", "target": "p2", "relation": "cites"},
                    {"source": "p1", "target": "p2", "relation": "extends"},
                ],
            },
            "duplicate edge",
        ),
    ],
)
def test_graph_snapshot_rejects_inconsistent_or_duplicate_records(
    tmp_path: Path,
    payload: dict[str, object],
    message: str,
) -> None:
    path = tmp_path / "invalid-graph.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        KnowledgeGraph.load_snapshot(path)
