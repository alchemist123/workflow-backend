"""The graph builds, satisfies ADK's structural rules, and routes correctly.

These run offline: no model, no network, no MCP server. Every generated package
gets this file, so a packaging bug that produces an invalid graph fails here
rather than at deploy time.
"""

from __future__ import annotations

import pytest


def test_graph_builds():
    """Constructing the Workflow runs ADK's own graph validation."""
    from agent import root_agent

    assert root_agent.name
    assert root_agent.edges


def test_registry_matches_the_graph():
    from nodes.registry import CANVAS_IDS, ENTRY_NODE, NODE_TYPES, NODES, TERMINAL_NODE

    assert ENTRY_NODE in NODES
    assert TERMINAL_NODE in NODES
    # Every node is traceable back to a canvas node and a canvas type.
    assert set(CANVAS_IDS) == set(NODES)
    assert set(NODE_TYPES) == set(NODES)


def test_node_names_are_unique_and_valid_identifiers():
    """Node names become ADK node names and appear in event paths."""
    from nodes.registry import NODES

    for name in NODES:
        assert name.isidentifier(), name
    assert len(set(NODES)) == len(NODES)


def test_no_duplicate_edges():
    """Two edges sharing a (from, to) pair are rejected by ADK as duplicates.

    The compiler merges such pairs into one Edge(route=[...]); this asserts it did.
    """
    from agent import root_agent

    pairs: list[tuple[str, str]] = []
    for edge in root_agent.edges:
        if hasattr(edge, "from_node"):  # explicit Edge
            pairs.append((edge.from_node.name, edge.to_node.name))
            continue

        # Tuple form: (a, b, c) is a chain; (a, {route: target}) is a dispatch.
        source, *rest = edge
        current = source
        for item in rest:
            if isinstance(item, dict):
                for target in item.values():
                    pairs.append((_name(current), _name(target)))
            else:
                pairs.append((_name(current), _name(item)))
                current = item

    assert len(pairs) == len(set(pairs)), f"duplicate edges: {pairs}"


def _name(node) -> str:
    return getattr(node, "name", str(node))


def test_exactly_one_terminal_node():
    """ADK fails a run with 'multiple terminal nodes produced output'.

    A node is terminal when nothing leaves it, so this is checked structurally
    rather than by running the graph.
    """
    from agent import root_agent
    from nodes.registry import NODES, TERMINAL_NODE

    has_outgoing: set[str] = set()
    for edge in root_agent.edges:
        if hasattr(edge, "from_node"):
            has_outgoing.add(edge.from_node.name)
            continue
        source, *rest = edge
        current = source
        for item in rest:
            if isinstance(item, dict):
                has_outgoing.add(_name(current))
            else:
                has_outgoing.add(_name(current))
                current = item

    terminals = set(NODES) - has_outgoing
    assert terminals == {TERMINAL_NODE}, terminals


# ── Behaviour ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_short_branch(run_graph):
    output, paths = await run_graph({"text": "Just a few words here."})

    assert output["branch"] == "short"
    assert output["word_count"] == 5
    assert output["summary"] == "Just a few words here."

    visited = [p.rsplit("/", 1)[-1].rsplit("@", 1)[0] for p in paths]
    assert "n_passthrough_4" in visited
    assert "n_summarise_3" not in visited


@pytest.mark.asyncio
async def test_long_branch(run_graph):
    text = (
        "FastAPI is a modern web framework for building APIs with Python. "
        "It is based on standard Python type hints. The key features are that "
        "it is fast to code and ready for production use."
    )
    output, paths = await run_graph({"text": text})

    assert output["branch"] == "long"
    assert output["word_count"] > 20
    # Extractive summary keeps the first two sentences.
    assert output["summary"].startswith("FastAPI is a modern web framework")
    assert "key features" not in output["summary"]

    visited = [p.rsplit("/", 1)[-1].rsplit("@", 1)[0] for p in paths]
    assert "n_summarise_3" in visited
    assert "n_passthrough_4" not in visited


@pytest.mark.asyncio
async def test_every_node_reports_a_resolvable_path(run_graph):
    """Event paths must map back to registry nodes, or canvas status breaks."""
    from nodes.registry import node_name_from_path

    _, paths = await run_graph({"text": "hello"})

    resolved = [node_name_from_path(p) for p in paths]
    assert any(name == "a2a_start" for name in resolved)
    assert any(name == "n_end_5" for name in resolved)


@pytest.mark.asyncio
async def test_plain_text_message_still_runs(run_graph):
    """An A2A caller may send prose rather than JSON.

    coerce_input wraps non-JSON text as {"text": ...}, which this workflow's
    start node is shaped to accept.
    """
    from core.state import coerce_input

    assert coerce_input("not json at all") == {"text": "not json at all"}

    output, _ = await run_graph({"text": "hello"})
    assert output["summary"] == "hello"
