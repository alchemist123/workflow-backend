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
    "collect_all_findings",
    "migrate_canvas", "needs_migration", "CURRENT_SCHEMA_VERSION",
    "build_graph_plan", "GraphPlan", "GraphPlanError", "GRAPH_SCHEMA_VERSION",
]


def collect_all_findings(canvas) -> "findings.Findings":
    """Every validation result, anchored to the node or edge it is about.

    The same two layers `run_compiler` runs, in the same order and with the
    same early return -- schema errors stop the pipeline, because every
    semantic rule below reads a config that has just been shown to be wrong.

    Separate from `run_compiler` rather than folded into it: that function's
    3-tuple has twenty callers across the test suite, and a signature change
    would be churn in service of nothing. Running the checks twice costs 0.5 ms.
    """
    from app.compiler import findings
    from app.compiler.findings import Findings
    from app.compiler.schema_validator import collect_schema_findings
    from app.compiler.semantic_validator import collect_findings

    schema = collect_schema_findings(canvas)
    if schema.has_errors:
        return schema

    semantic = collect_findings(canvas)
    return Findings(items=[*schema.items, *semantic.items])


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
