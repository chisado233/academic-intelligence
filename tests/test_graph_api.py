"""Integration-style tests for AcademicIntelligence.expand / subgraph / path.

Storage and collector are fakes injected into the instance; no network.
"""

from __future__ import annotations

from pathlib import Path

from academic_intelligence import AcademicIntelligence, ExpandResult
from academic_intelligence.core.models import AuthorRef

# Reuse the fakes from the traversal test module
from tests.test_graph_traversal import FakeCollector, FakeStorage, _author, _paper


def _make_ai(storage: FakeStorage, collector: FakeCollector) -> AcademicIntelligence:
    ai = AcademicIntelligence(
        {
            "storage_type": "sqlite",
            "storage_path": ":memory:",
            "sources": ["openalex"],
            # Allow chains longer than the default depth cap for path tests.
            "max_expand_depth": 10,
        }
    )
    ai._storage = storage  # type: ignore[assignment]
    ai._collector = collector  # type: ignore[assignment]
    ai._connected = True
    return ai


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_public_imports() -> None:
    from academic_intelligence import AcademicIntelligence, ExpandResult  # noqa: F401, N817

    assert AcademicIntelligence is not None
    assert ExpandResult is not None


async def test_expand_returns_expand_result() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1", "r2"]
    ai = _make_ai(storage, FakeCollector())

    result = await ai.expand("p1", relations=["references"])

    assert isinstance(result, ExpandResult)
    assert result.center_id == "p1"
    assert result.stats.nodes_found == 2
    assert {n["id"] for n in result.nodes} == {"r1", "r2"}


async def test_expand_second_call_hits_graph_cache(tmp_path: Path) -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1", "r2"]
    collector = FakeCollector()
    ai = _make_ai(storage, collector)

    first = await ai.expand("p1", relations=["references"])
    assert first.stats.cache_hits == 0

    second = await ai.expand("p1", relations=["references"])
    assert second.stats.cache_hits >= 2
    assert collector.calls == []


async def test_expand_with_sources_parameter() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["r1"]
    ai = _make_ai(storage, FakeCollector())

    result = await ai.expand("p1", relations=["references"], sources=["openalex"])

    assert result.stats.nodes_found == 1


# ---------------------------------------------------------------------------
# subgraph
# ---------------------------------------------------------------------------


async def test_subgraph_returns_serialized_dict() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["p2"]
    storage.papers["p2"] = _paper("p2", "Beta")
    storage.refs["p2"] = ["p3"]
    storage.papers["p3"] = _paper("p3", "Gamma")
    ai = _make_ai(storage, FakeCollector())

    await ai.expand("p1", relations=["references"], depth=2)

    sub = await ai.subgraph("p2", radius=1)
    assert isinstance(sub, dict)
    assert sub["center"] == "p2"
    node_ids = {n["id"] for n in sub["nodes"]}
    assert node_ids == {"p1", "p2", "p3"}
    assert sub["node_count"] == 3


async def test_subgraph_missing_center_returns_empty() -> None:
    ai = _make_ai(FakeStorage(), FakeCollector())
    sub = await ai.subgraph("ghost", radius=2)
    assert sub["nodes"] == []
    assert sub["node_count"] == 0


# ---------------------------------------------------------------------------
# path
# ---------------------------------------------------------------------------


async def test_path_shortest_chain() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.refs["p1"] = ["p2"]
    storage.papers["p2"] = _paper("p2", "Beta")
    storage.refs["p2"] = ["p3"]
    storage.papers["p3"] = _paper("p3", "Gamma")
    storage.refs["p3"] = ["p4"]
    storage.papers["p4"] = _paper("p4", "Delta")
    storage.refs["p4"] = ["p5"]
    storage.papers["p5"] = _paper("p5", "Epsilon")
    ai = _make_ai(storage, FakeCollector())

    await ai.expand("p1", relations=["references"], depth=4)

    path = await ai.path("p1", "p5")
    assert path == ["p1", "p2", "p3", "p4", "p5"]


async def test_path_returns_empty_when_unreachable() -> None:
    storage = FakeStorage()
    storage.papers["p1"] = _paper("p1", "Alpha")
    storage.papers["p2"] = _paper("p2", "Beta")
    ai = _make_ai(storage, FakeCollector())
    ai._graph = None

    # Ensure a graph exists with two disconnected nodes.
    graph = ai._ensure_graph()
    graph.add_node("p1", type="paper", title="Alpha", loaded=True)
    graph.add_node("p2", type="paper", title="Beta", loaded=True)

    assert await ai.path("p1", "p2") == []


async def test_path_requires_known_source(tmp_path: Path) -> None:
    storage = FakeStorage()
    ai = _make_ai(storage, FakeCollector())
    assert await ai.path("ghost", "other") == []


# ---------------------------------------------------------------------------
# Author expand through the public API
# ---------------------------------------------------------------------------


async def test_expand_author_papers_and_coauthors_via_api() -> None:
    storage = FakeStorage()
    storage.authors["a1"] = _author("a1", "Ada")
    storage.author_papers["a1"] = ["p1"]
    storage.papers["p1"] = _paper(
        "p1", "One", authors=[AuthorRef(author_id="a1", name="Ada")]
    )
    storage.coauthors["a1"] = ["a2"]
    storage.authors["a2"] = _author("a2", "Bob")
    ai = _make_ai(storage, FakeCollector())

    result = await ai.expand("a1", relations=["papers", "coauthors"])

    node_ids = {n["id"] for n in result.nodes}
    assert node_ids == {"p1", "a2"}
    relations = {e["relation"] for e in result.edges}
    assert relations == {"authored_by", "coauthor_with"}
