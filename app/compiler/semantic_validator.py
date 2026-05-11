"""Layer 2: semantic graph validation rules."""
import networkx as nx
from app.schemas.canvas import CanvasPayload
from app.nodes.registry import get_node_definition


def validate_semantics(canvas: CanvasPayload) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors block compilation; warnings are informational."""
    errors: list[str] = []
    warnings: list[str] = []
    nodes = {n.id: n for n in canvas.nodes}
    node_ids = set(nodes.keys())

    # Build directed graph
    G = nx.DiGraph()
    for node in canvas.nodes:
        G.add_node(node.id, data=node)
    for edge in canvas.edges:
        G.add_edge(edge.source, edge.target, data=edge)

    # Rule: edge endpoints must exist
    for edge in canvas.edges:
        if edge.source not in node_ids:
            errors.append(f"Edge '{edge.id}': source node '{edge.source}' does not exist")
        if edge.target not in node_ids:
            errors.append(f"Edge '{edge.id}': target node '{edge.target}' does not exist")

    # Rule: trigger nodes cannot have inbound edges
    for node in canvas.nodes:
        defn = get_node_definition(node.type)
        if defn and not defn.allows_inbound and G.in_degree(node.id) > 0:
            errors.append(f"Node '{node.id}' ({node.type}) is a trigger and must not have inbound edges")

    # Rule: END nodes cannot have outbound edges
    for node in canvas.nodes:
        defn = get_node_definition(node.type)
        if defn and not defn.allows_outbound and G.out_degree(node.id) > 0:
            errors.append(f"Node '{node.id}' ({node.type}) is a terminal node and must not have outbound edges")

    # Rule: at least one trigger node
    trigger_nodes = [n for n in canvas.nodes if (defn := get_node_definition(n.type)) and defn.is_trigger]
    if not trigger_nodes:
        errors.append("Workflow must have at least one trigger node (HTTP_TRIGGER, SCHEDULE_TRIGGER, WEBHOOK_TRIGGER, or QUEUE_TRIGGER)")

    # Rule: at least one END node
    end_nodes = [n for n in canvas.nodes if (defn := get_node_definition(n.type)) and defn.is_terminal]
    if not end_nodes:
        errors.append("Workflow must have at least one END node")

    # Warning (not error): orphaned nodes don't break execution — they're just unused.
    # Tool-provider types (TOOL, DATASOURCE, REMOTE_AGENT, FUNCTION) are intentionally
    # exempt: they wire to ORCHESTRATOR_AGENT via the bottom "tools" handle and may sit
    # unconnected while the canvas is still being built.
    _TOOL_PROVIDER_TYPES = {"TOOL", "DATASOURCE", "REMOTE_AGENT", "FUNCTION"}
    connected = set()
    for edge in canvas.edges:
        connected.add(edge.source)
        connected.add(edge.target)
    for node in canvas.nodes:
        if (node.id not in connected
                and len(canvas.nodes) > 1
                and node.type not in _TOOL_PROVIDER_TYPES):
            warnings.append(f"Node '{node.id}' ({node.type}) is not connected to any edge and will be skipped")

    # Rule: cycles only through LOOP nodes
    try:
        cycles = list(nx.simple_cycles(G))
        for cycle in cycles:
            cycle_nodes = [nodes[nid] for nid in cycle if nid in nodes]
            has_loop = any(
                (defn := get_node_definition(n.type)) and defn.allows_cycle
                for n in cycle_nodes
            )
            if not has_loop:
                cycle_str = " -> ".join(cycle)
                errors.append(f"Cycle detected without a LOOP node: {cycle_str}")
    except Exception:
        pass

    # Rule: CONDITION nodes must define at least one branch in config
    for node in canvas.nodes:
        if node.type == "CONDITION":
            if not node.config.get("branches"):
                errors.append(f"Node '{node.id}' (CONDITION) must define at least one branch")

    # Rule: PARALLEL_FORK must fan out to at least 2 edges (branches come from edges, not config)
    for node in canvas.nodes:
        if node.type == "PARALLEL_FORK":
            out_edges = [e for e in canvas.edges if e.source == node.id]
            if len(out_edges) < 2:
                errors.append(f"Node '{node.id}' (PARALLEL_FORK) must have at least 2 outbound edges")

    return errors, warnings
