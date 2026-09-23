"""The graph builds and satisfies ADK's structural rules.

Generated for Variables. Runs offline: no model, no network.
"""

from __future__ import annotations

EXPECTED_NODES = ['a2a_start', 'n_condition_6', 'n_end_2', 'n_transform_1', 'n_transform_3', 'n_transform_4', 'n_transform_5']
# Nodes with a module of their own. A MERGE is a JoinNode built in the registry,
# so it has none.
MODULE_NODES = ['a2a_start', 'n_condition_6', 'n_end_2', 'n_transform_1', 'n_transform_3', 'n_transform_4', 'n_transform_5']
ENTRY_NODE = 'a2a_start'
TERMINAL_NODE = 'n_end_2'


def test_graph_builds():
    """Constructing the Workflow runs ADK's own graph validation."""
    from agent import root_agent

    assert root_agent.name
    assert root_agent.edges


def test_registry_matches_the_graph():
    from nodes.registry import CANVAS_IDS, ENTRY_NODE as entry, NODES, TERMINAL_NODE as terminal

    assert sorted(NODES) == EXPECTED_NODES
    assert entry == ENTRY_NODE
    assert terminal == TERMINAL_NODE
    assert set(CANVAS_IDS) == set(NODES)


def test_every_node_module_imports():
    """One file per canvas node, all importable.

    Except a MERGE: the registry builds it as a JoinNode, so it has no module
    of its own. Importing one raised ModuleNotFoundError for any workflow that
    joined parallel branches.
    """
    import importlib

    for name in MODULE_NODES:
        module = importlib.import_module(f"nodes.{name}")
        assert module is not None


def test_node_names_are_valid_identifiers():
    from nodes.registry import NODES

    for name in NODES:
        assert name.isidentifier(), name


def test_exactly_one_terminal_node():
    """ADK fails a run with 'multiple terminal nodes produced output'."""
    from agent import root_agent
    from nodes.registry import NODES

    has_outgoing = set()
    for edge in root_agent.edges:
        if hasattr(edge, "from_node"):
            has_outgoing.add(edge.from_node.name)
            continue
        source, *rest = edge
        current = source
        for item in rest:
            has_outgoing.add(getattr(current, "name", str(current)))
            current = item

    terminals = set(NODES) - has_outgoing
    assert terminals == {TERMINAL_NODE}, terminals


def test_no_duplicate_edges():
    """Two edges sharing a (from, to) pair are rejected by ADK as duplicates."""
    from agent import root_agent

    pairs = []
    for edge in root_agent.edges:
        if hasattr(edge, "from_node"):
            pairs.append((edge.from_node.name, edge.to_node.name))
            continue
        source, *rest = edge
        current = source
        for item in rest:
            pairs.append(
                (getattr(current, "name", str(current)), getattr(item, "name", str(item)))
            )
            current = item

    assert len(pairs) == len(set(pairs)), f"duplicate edges: {pairs}"


def test_graph_json_matches_the_registry():
    import json
    from pathlib import Path

    from nodes.registry import CANVAS_IDS

    graph = json.loads((Path(__file__).resolve().parent.parent / "graph.json").read_text())

    assert {n["name"] for n in graph["nodes"]} == set(CANVAS_IDS)
    assert graph["entry_node"] == ENTRY_NODE
    assert graph["terminal_node"] == TERMINAL_NODE
