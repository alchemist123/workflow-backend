"""Layer 1 of the compiler: validate every node's config against its registered schema."""
from app.schemas.canvas import CanvasPayload
from app.nodes.registry import get_node_definition


def validate_schemas(canvas: CanvasPayload) -> list[str]:
    errors: list[str] = []
    for node in canvas.nodes:
        definition = get_node_definition(node.type)
        if definition is None:
            errors.append(f"Node '{node.id}': unknown node type '{node.type}'")
            continue
        config_errors = definition.validate_config(node.config)
        for err in config_errors:
            errors.append(f"Node '{node.id}' ({node.type}) config error: {err}")
    return errors
