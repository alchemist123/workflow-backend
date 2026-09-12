"""Build a graph plan under ADK to prove it is legal before packaging.

The semantic validator and the graph planner both encode ADK's structural rules,
but encoding a rule is not the same as satisfying it.  This module closes that
gap: it materialises the plan as a real `google.adk.Workflow` whose nodes are
stubs, and lets ADK's own validator judge it.

That makes the check authoritative rather than a second opinion — if ADK changes
a rule, this fails on the next package attempt instead of shipping a broken
container.  It costs nothing at runtime: the stub nodes are never executed, and
construction is pure Python.

Used as the final gate in the packaging path, and by the compiler tests to prove
that every ADK error is one our own validator reports first.
"""

from __future__ import annotations

from typing import Any

from app.compiler.graph_plan import GraphPlan

# Stub node bodies deliberately carry no return annotation: under ADK's
# 'node_input' binding a `-> Event` hint is inferred as the node's output_schema
# and every downstream edge then fails schema validation. Generated nodes follow
# the same rule; see tests/test_adk_contract.py.


def build_adk_workflow(plan: GraphPlan) -> Any:
    """Construct a real ADK Workflow from the plan, using stub node bodies.

    Raises whatever ADK raises when the graph is illegal.
    """
    from google.adk import Event, Workflow
    from google.adk.agents.context import Context
    from google.adk.workflow import START, Edge, JoinNode, node

    def _stub(node_name: str):
        async def impl(ctx: Context, node_input=None):
            return Event(output=node_input)

        impl.__name__ = node_name
        return node(impl, name=node_name)

    adk_nodes: dict[str, Any] = {}
    for planned in plan.nodes:
        # MERGE compiles to a JoinNode, which has no body of its own.
        adk_nodes[planned.name] = (
            JoinNode(name=planned.name) if planned.is_join else _stub(planned.name)
        )

    edges: list[Any] = []
    for planned_edge in plan.edges:
        source = START if planned_edge.from_node == "START" else adk_nodes[planned_edge.from_node]
        target = adk_nodes[planned_edge.to_node]
        if planned_edge.route is None:
            edges.append((source, target))
        else:
            edges.append(Edge(from_node=source, to_node=target, route=planned_edge.route))

    return Workflow(
        # The slug, not the display name: ADK validates this as a node name and
        # rejects anything that is not a Python identifier.
        name=plan.workflow_slug or "workflow",
        description=plan.workflow_description or "",
        edges=edges,
    )


def verify_plan_builds(plan: GraphPlan) -> list[str]:
    """Return ADK's complaints about this plan, or an empty list.

    Messages are rewritten to name the canvas node the user has to fix, since an
    ADK error names generated node names they have never seen.
    """
    try:
        build_adk_workflow(plan)
    except Exception as exc:  # noqa: BLE001 - any ADK failure is a finding
        return [_with_canvas_ids(str(exc), plan)]
    return []


def _with_canvas_ids(message: str, plan: GraphPlan) -> str:
    """Append the canvas ids behind any generated node names in an ADK message."""
    mentioned = [
        node for node in plan.nodes
        if node.name in message and node.name != node.canvas_id
    ]
    if not mentioned:
        return f"ADK rejected the workflow graph: {message}"

    mapping = ", ".join(
        f"{node.name} = canvas node '{node.canvas_id}'"
        + (f" ({node.title})" if node.title else "")
        for node in mentioned
    )
    return f"ADK rejected the workflow graph: {message} — where {mapping}"
