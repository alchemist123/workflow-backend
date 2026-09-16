"""There is exactly one place a workflow can execute.

The platform used to carry a second implementation alongside the generated
packages — `app/runtime/engine.py` traversing the IR, with an `execute` method on
every NodeDefinition. The two had already diverged (LOOP ran real iterations in
the platform and a single pass in a package), and keeping them in step by hand
was the bug source this migration exists to remove.

These tests fail if that second path grows back.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

from app.nodes.registry import NODE_REGISTRY

warnings.filterwarnings("ignore", category=UserWarning)

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"


# ── Node definitions declare, they do not execute ────────────────────────────


def test_no_node_definition_carries_behaviour():
    """A NodeDefinition is a declaration: palette, config contract, handles."""
    for node_type, definition in NODE_REGISTRY.items():
        assert not hasattr(definition, "execute"), (
            f"{node_type} has an execute method again; node behaviour belongs in "
            "app/packaging/render_templates/nodes/"
        )


def test_node_definitions_still_declare_what_the_ui_needs():
    """Removing behaviour must not have removed the contract."""
    for node_type, definition in NODE_REGISTRY.items():
        assert definition.config_schema is not None, node_type
        assert definition.palette is not None, node_type
        assert definition.palette.label, node_type
        assert definition.output_handles is not None, node_type


def test_node_definition_base_is_not_abstract():
    """It has no abstract method left, so it is a plain dataclass."""
    from app.nodes.base import NodeDefinition

    assert not getattr(NodeDefinition, "__abstractmethods__", None)


def test_node_modules_define_no_execution_helpers():
    """The old engine's node helpers went with it.

    An `async def` in a node definition module means behaviour has crept back.
    """
    allowed = {"__init__.py", "base.py", "registry.py"}
    for path in sorted((APP / "nodes").rglob("*.py")):
        if path.name in allowed or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        offenders = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        ]
        assert offenders == [], f"{path.name} defines async {offenders}"


# ── The old runtime is gone ──────────────────────────────────────────────────


@pytest.mark.parametrize("module", ["engine", "handlers", "context"])
def test_removed_runtime_modules_stay_removed(module):
    assert not (APP / "runtime" / f"{module}.py").exists(), (
        f"app/runtime/{module}.py is back; there should be one execution path"
    )


def test_nothing_imports_the_old_runtime():
    for path in sorted(BACKEND.rglob("*.py")):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        if path.name == Path(__file__).name:
            continue
        source = path.read_text()
        for forbidden in ("runtime.engine", "runtime.handlers", "runtime.context", "WorkflowEngine"):
            assert forbidden not in source, f"{path.relative_to(BACKEND)} references {forbidden}"


def test_runtime_exposes_only_the_package_runner():
    import app.runtime as runtime

    assert set(runtime.__all__) == {"run_workflow_package", "ensure_package", "RunResult"}


# ── One run endpoint ─────────────────────────────────────────────────────────


def test_execute_endpoint_is_gone_and_test_replaces_it():
    from app.main import app

    paths = set(app.openapi()["paths"])

    assert not any(p.endswith("/execute") for p in paths), (
        "/execute is back; /test drives the generated package instead"
    )

    # Actions on a version: exactly one *runs* the workflow, and it is the one
    # that drives the generated package. `package` builds one; `task` reads a
    # task back out of one with `tasks/get` and runs nothing.
    actions = {p.rsplit("/", 1)[-1] for p in paths if "/versions/{version_id}/" in p}
    assert actions == {"test", "package", "task"}, sorted(actions)
    assert actions & {"execute", "run", "invoke"} == set(), sorted(actions)


def test_run_history_endpoints_survive():
    """NodeExecutionLog remains the run-history store, now fed from ADK events."""
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/v1/workflows/{workflow_id}/executions" in paths
    assert "/api/v1/workflows/{workflow_id}/executions/{execution_id}" in paths
    assert "/api/v1/workflows/{workflow_id}/executions/{execution_id}/node_logs" in paths


# ── Policies are mapped, not dropped ─────────────────────────────────────────


def test_every_policy_field_has_somewhere_to_go():
    """NodePolicies must map onto ADK, or a canvas setting is silently ignored.

    timeout_seconds -> @node(timeout=), retry.max_attempts -> RetryConfig, and
    on_error -> the generated module's ON_ERROR for node types that declare
    support. A new policy field with no mapping should fail here.
    """
    from app.schemas.canvas import NodePolicies

    assert set(NodePolicies.model_fields) == {"timeout_seconds", "retry", "on_error"}


def test_on_error_continue_is_declared_per_node_type():
    """Structural nodes cannot honour it; the validator warns rather than
    accepting a setting that would be ignored."""
    supported = {t for t, d in NODE_REGISTRY.items() if d.supports_on_error_continue}
    structural = {t for t, d in NODE_REGISTRY.items() if not d.supports_on_error_continue}

    # Nodes that call out of the workflow can carry on with an error payload.
    assert {"TRANSFORM", "FUNCTION", "TOOL", "REMOTE_AGENT", "LLM_AGENT"} <= supported
    # Nodes where continuing is meaningless cannot.
    assert {"A2A_START", "CONDITION", "END", "MERGE", "PARALLEL_FORK"} <= structural


def test_validator_warns_when_on_error_cannot_be_honoured():
    from app.compiler.semantic_validator import validate_semantics
    from app.schemas.canvas import CanvasPayload

    def node(node_id: str, node_type: str, config: dict, on_error: str = "fail") -> dict:
        return {
            "id": node_id,
            "type": node_type,
            "version": "1",
            "position": {"x": 0, "y": 0},
            "metadata": {"title": "", "description": ""},
            "config": config,
            "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
            "policies": {
                "timeout_seconds": 60,
                "retry": {"max_attempts": 1},
                "on_error": on_error,
            },
        }

    def edge(source: str, target: str, handle: str = "output") -> dict:
        return {
            "id": f"e_{source}_{target}",
            "source": source,
            "source_handle": handle,
            "target": target,
            "target_handle": "input",
            "condition": None,
        }

    start_cfg = {
        "payload_schema": {
            "fields": [{"name": "text", "type": "string", "description": "", "required": True}]
        }
    }

    # A CONDITION set to continue: warned about.
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                node("start", "A2A_START", start_cfg),
                node(
                    "router",
                    "CONDITION",
                    {"branches": [{"name": "only", "expression": "True"}]},
                    on_error="continue",
                ),
                node("end", "END", {}),
            ],
            "edges": [edge("start", "router"), edge("router", "end", "only")],
        }
    )
    errors, warns = validate_semantics(canvas)
    assert errors == []
    assert any("continue on error" in w and "router" in w for w in warns)

    # A TRANSFORM set to continue: accepted silently, because it works.
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                node("start", "A2A_START", start_cfg),
                node(
                    "mid",
                    "TRANSFORM",
                    {"mode": "jmespath", "expression": "text"},
                    on_error="continue",
                ),
                node("end", "END", {}),
            ],
            "edges": [edge("start", "mid"), edge("mid", "end")],
        }
    )
    errors, warns = validate_semantics(canvas)
    assert errors == []
    assert not any("continue on error" in w for w in warns)
