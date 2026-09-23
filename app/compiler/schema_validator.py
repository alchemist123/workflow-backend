"""Layer 1 of the compiler: validate every node's config against its registered schema."""
from app.compiler.findings import Findings
from app.nodes.registry import get_node_definition
from app.schemas.canvas import CanvasPayload


def collect_schema_findings(canvas: CanvasPayload) -> Findings:
    """Config errors, each anchored to the node that has one.

    Anchored for the same reason the semantic rules are: the canvas marks the
    node rather than printing a list. It matters more here than anywhere else,
    because `run_compiler` returns as soon as this layer finds anything — so a
    node with a bad config is the most common thing anyone sees, and it would
    have been the one class of error arriving with nothing to point at.
    """
    out = Findings()
    for node in canvas.nodes:
        definition = get_node_definition(node.type)
        if definition is None:
            out.error(f"Node '{node.id}': unknown node type '{node.type}'", node=node)
            continue
        for err in definition.validate_config(node.config):
            out.error(
                f"Node '{node.id}' ({node.type}) config error: {err}", node=node
            )
    return out


def validate_schemas(canvas: CanvasPayload) -> list[str]:
    """The plain list of messages. Unchanged for every existing caller."""
    return collect_schema_findings(canvas).as_strings(
        {n.id: n.type for n in canvas.nodes}
    )[0]
