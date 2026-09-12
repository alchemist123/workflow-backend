from .canvas_migrations import (
    CURRENT_SCHEMA_VERSION,
    migrate_canvas,
    needs_migration,
)
from .schema_validator import validate_schemas
from .semantic_validator import validate_semantics
from .graph_plan import (
    GRAPH_SCHEMA_VERSION,
    GraphPlan,
    GraphPlanError,
    build_graph_plan,
)
from .ir import compile_to_ir, IR

__all__ = [
    "validate_schemas", "validate_semantics", "compile_to_ir", "IR",
    "migrate_canvas", "needs_migration", "CURRENT_SCHEMA_VERSION",
    "build_graph_plan", "GraphPlan", "GraphPlanError", "GRAPH_SCHEMA_VERSION",
]


def run_compiler(canvas, version_id: str) -> tuple[list[str], list[str], "IR | None"]:
    """Full compiler pipeline: schema -> semantic -> IR.
    Returns (errors, warnings, ir). Errors block compilation; warnings do not.
    """
    errors = validate_schemas(canvas)
    if errors:
        return errors, [], None

    errors, warnings = validate_semantics(canvas)
    if errors:
        return errors, warnings, None

    ir = compile_to_ir(canvas, version_id)
    return [], warnings, ir
