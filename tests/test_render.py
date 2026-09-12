"""Rendering a graph plan into a package.

The end-to-end proof is `test_seed_packages_render_and_pass_their_own_tests`,
which renders each seed workflow and runs the generated package's own suite in a
subprocess. Everything above it isolates a single behaviour so a failure points
somewhere specific.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.packaging.render import (
    NODE_TEMPLATES,
    STATIC_FILES,
    RenderError,
    ascii_graph,
    render_package,
)
from app.schemas.canvas import CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)

SEED_DIR = Path(__file__).resolve().parent.parent.parent / "test-agents" / "workflows"


# ── Canvas fixtures ──────────────────────────────────────────────────────────


def _node(node_id: str, node_type: str, config: dict | None = None, **meta) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": meta.get("title", ""), "description": meta.get("description", "")},
        "config": config if config is not None else {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {
            "timeout_seconds": meta.get("timeout", 60),
            "retry": {"max_attempts": meta.get("retries", 1)},
            "on_error": meta.get("on_error", "fail"),
        },
    }


def _edge(source: str, target: str, handle: str = "output", target_handle: str = "input") -> dict:
    return {
        "id": f"e_{source}_{target}_{handle}",
        "source": source,
        "source_handle": handle,
        "target": target,
        "target_handle": target_handle,
        "condition": None,
    }


_START_CFG = {
    "payload_schema": {
        "fields": [{"name": "text", "type": "string", "description": "Input", "required": True}]
    }
}


def _plan(nodes: list[dict], edges: list[dict], *, name: str = "Test Workflow"):
    canvas = CanvasPayload.model_validate({"nodes": nodes, "edges": edges})
    errors, _warnings, ir = run_compiler(canvas, "render-test-0001")
    assert errors == [], f"canvas did not validate: {errors}"
    return build_graph_plan(ir, workflow_name=name, workflow_description="A test workflow")


def _linear_plan(transform_config: dict | None = None):
    return _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node(
                "mid",
                "TRANSFORM",
                transform_config or {"mode": "jmespath", "expression": "text"},
                title="Shape",
            ),
            _node("end", "END", {"output_mapping": {"result": "answer"}}),
        ],
        [_edge("start", "mid"), _edge("mid", "end")],
    )


# ── Structure ────────────────────────────────────────────────────────────────


def test_renders_the_expected_tree(tmp_path):
    plan = _linear_plan()
    result = render_package(plan, tmp_path / "pkg")
    root = tmp_path / "pkg"

    # One module per node, none for a MERGE.
    for node in plan.nodes:
        assert (root / f"nodes/{node.name}.py").is_file(), node.name

    for relative in STATIC_FILES:
        assert (root / relative).is_file(), relative

    for relative in ("main.py", "agent.py", "nodes/registry.py", "core/agent_card.py",
                     "requirements.txt", ".env", ".env.example", "README.md", "graph.json",
                     "tests/test_graph.py", "tests/test_a2a.py"):
        assert (root / relative).is_file(), relative

    assert set(result.files) >= set(STATIC_FILES)


def test_every_graph_node_type_has_a_template():
    """A node type the planner allows but the renderer cannot emit would
    silently become a pass-through.

    Three kinds of node legitimately have no module: MERGE (the registry builds
    a JoinNode), the tool groups (resolved into a consumer's tool list, like a
    TOOL is), and the types packaging refuses outright.
    """
    from app.compiler.graph_plan import _UNSUPPORTED_TYPES
    from app.nodes.registry import NODE_REGISTRY, TOOL_GROUP_TYPES

    without_modules = _UNSUPPORTED_TYPES | TOOL_GROUP_TYPES | {"MERGE"}
    for node_type in NODE_REGISTRY:
        if node_type in without_modules:
            continue
        assert node_type in NODE_TEMPLATES, f"{node_type} has no render template"


def test_tool_groups_have_no_module_of_their_own():
    """A group is a tool, not a graph node."""
    from app.nodes.registry import TOOL_GROUP_TYPES

    for node_type in TOOL_GROUP_TYPES:
        assert node_type not in NODE_TEMPLATES, (
            f"{node_type} has a render template, but a tool group is resolved "
            "into its consumer's tool list rather than generated as a node"
        )


def test_rendering_is_deterministic(tmp_path):
    """Re-rendering an unchanged workflow must produce a zero-line diff.

    Same directory name in both runs: the README names the directory in its
    deploy command, so that is an input, not non-determinism.
    """
    plan = _linear_plan()
    first, second = tmp_path / "one" / "pkg", tmp_path / "two" / "pkg"
    render_package(plan, first)
    render_package(plan, second)

    files = [p for p in sorted(first.rglob("*")) if p.is_file()]
    assert files, "nothing was rendered"
    for path in files:
        twin = second / path.relative_to(first)
        assert twin.exists(), path.name
        assert path.read_bytes() == twin.read_bytes(), path.name


def test_destination_is_replaced_not_merged(tmp_path):
    destination = tmp_path / "pkg"
    destination.mkdir()
    (destination / "stale.py").write_text("# left over from an earlier build\n")

    render_package(_linear_plan(), destination)

    assert not (destination / "stale.py").exists()


def test_graph_json_matches_the_plan(tmp_path):
    plan = _linear_plan()
    render_package(plan, tmp_path / "pkg")

    graph = json.loads((tmp_path / "pkg" / "graph.json").read_text())
    assert graph == plan.to_dict()


# ── Generated content ────────────────────────────────────────────────────────


def test_agent_py_wires_start_and_every_edge(tmp_path):
    plan = _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node(
                "router",
                "CONDITION",
                {
                    "branches": [
                        {"name": "yes", "expression": "data.get('n', 0) > 1"},
                        {"name": "no", "expression": "True"},
                    ]
                },
            ),
            _node("end", "END"),
        ],
        [
            _edge("start", "router"),
            _edge("router", "end", "yes"),
            _edge("router", "end", "no"),
        ],
    )
    render_package(plan, tmp_path / "pkg")
    source = (tmp_path / "pkg" / "agent.py").read_text()

    assert "(START, a2a_start)" in source

    # Count in the code, not the module docstring, which documents the form.
    code = source.split('"""', 2)[-1]
    # Both branches to one target must be merged onto a single Edge: ADK rejects
    # two edges sharing a (from, to) pair.
    assert code.count("Edge(from_node=") == 1, code
    assert '["yes", "no"]' in code


def test_agent_py_omits_the_edge_import_when_nothing_is_routed(tmp_path):
    """An unused import is a lint finding in every package that has no router."""
    render_package(_linear_plan(), tmp_path / "pkg")
    source = (tmp_path / "pkg" / "agent.py").read_text()

    assert "from google.adk.workflow import START\n" in source
    assert "Edge" not in source.split('"""')[-1]


def test_condition_default_route_is_a_python_literal(tmp_path):
    """Regression: `tojson` rendered None as `null`, which compiles but raises
    NameError on import."""
    plan = _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node("router", "CONDITION", {"branches": [{"name": "only", "expression": "True"}]}),
            _node("end", "END"),
        ],
        [_edge("start", "router"), _edge("router", "end", "only")],
    )
    render_package(plan, tmp_path / "pkg")
    router = next(n for n in plan.nodes if n.node_type == "CONDITION")
    source = (tmp_path / "pkg" / f"nodes/{router.name}.py").read_text()

    assert "DEFAULT_ROUTE = None" in source
    assert "null" not in source


def test_merge_becomes_a_join_node_in_the_registry(tmp_path):
    plan = _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node("fork", "PARALLEL_FORK", {"branches": ["a", "b"]}),
            _node("left", "TRANSFORM", {"mode": "jmespath", "expression": "text"}),
            _node("right", "TRANSFORM", {"mode": "jmespath", "expression": "text"}),
            _node("merge", "MERGE"),
            _node("end", "END"),
        ],
        [
            _edge("start", "fork"),
            _edge("fork", "left", "a"),
            _edge("fork", "right", "b"),
            _edge("left", "merge"),
            _edge("right", "merge"),
            _edge("merge", "end"),
        ],
    )
    render_package(plan, tmp_path / "pkg")

    merge = next(n for n in plan.nodes if n.is_join)
    # A JoinNode has no body, so no module is written for it.
    assert not (tmp_path / "pkg" / f"nodes/{merge.name}.py").exists()
    registry = (tmp_path / "pkg" / "nodes/registry.py").read_text()
    assert f'JoinNode(name="{merge.name}")' in registry


def test_payload_schema_reaches_the_entry_module(tmp_path):
    plan = _linear_plan()
    render_package(plan, tmp_path / "pkg")
    source = (tmp_path / "pkg" / f"nodes/{plan.entry_node}.py").read_text()

    assert "PAYLOAD_SCHEMA" in source
    assert "'text'" in source or '"text"' in source


def test_env_files_carry_every_key_with_attribution(tmp_path):
    plan = _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}, title="Brain"),
            _node("end", "END"),
        ],
        [_edge("start", "orch"), _edge("orch", "end")],
    )
    render_package(plan, tmp_path / "pkg")

    env = (tmp_path / "pkg" / ".env").read_text()
    example = (tmp_path / "pkg" / ".env.example").read_text()

    for key in plan.env_keys:
        assert f"{key.key}=" in env, key.key
        assert key.key in example, key.key

    # The example says which node needs each key.
    assert "Needed by:" in example
    assert "GOOGLE_CLOUD_PROJECT" in example
    # And explains Google auth only when there is a model node.
    assert "application-default login" in example


def test_env_example_omits_google_auth_without_a_model_node(tmp_path):
    render_package(_linear_plan(), tmp_path / "pkg")
    example = (tmp_path / "pkg" / ".env.example").read_text()

    assert "GOOGLE_CLOUD_PROJECT" not in example
    assert "application-default login" not in example


def test_requirements_track_the_node_set(tmp_path):
    render_package(_linear_plan(), tmp_path / "pkg")
    plain = (tmp_path / "pkg" / "requirements.txt").read_text()

    assert "google-adk>=2.8,<3" in plain
    assert "a2a-sdk[http-server]>=0.3.4,<1.0" in plain
    assert "jmespath" in plain          # the TRANSFORM is in jmespath mode
    assert "mcp>=" not in plain          # no MCP nodes
    assert "httpx" not in plain          # no remote agents

    plan = _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node("remote", "REMOTE_AGENT", {"endpoint": "http://a:1"}, title="Helper"),
            _node("tool", "TOOL", {"mcp_url": "http://m:9000", "tool_name": "search"}, title="S"),
            _node("end", "END"),
        ],
        [_edge("start", "remote"), _edge("remote", "tool"), _edge("tool", "end")],
    )
    render_package(plan, tmp_path / "pkg2")
    rich = (tmp_path / "pkg2" / "requirements.txt").read_text()

    assert "httpx" in rich
    assert "mcp>=1.9,<2" in rich


def test_readme_documents_both_invocation_modes(tmp_path):
    render_package(_linear_plan(), tmp_path / "pkg", port=8123)
    readme = (tmp_path / "pkg" / "README.md").read_text()

    assert "message/send" in readme
    assert "tasks/get" in readme
    assert '"blocking": true' in readme
    assert '"blocking": false' in readme
    assert "localhost:8123" in readme
    # The graph is drawn, not just described.
    assert "START" in readme


def test_ascii_graph_shows_routes_and_loops():
    plan = _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node("loop", "LOOP", {"mode": "while", "exit_condition": "i > 2", "max_iterations": 4}),
            _node("body", "TRANSFORM", {"mode": "jmespath", "expression": "text"}),
            _node("end", "END"),
        ],
        [
            _edge("start", "loop"),
            _edge("loop", "body", "loop_body"),
            _edge("body", "loop"),
            _edge("loop", "end", "done"),
        ],
    )
    drawing = ascii_graph(plan)

    assert "START" in drawing
    assert "loop_body" in drawing
    assert "loops back" in drawing


# ── Canvas code validation ───────────────────────────────────────────────────


def test_invalid_python_in_a_function_node_names_the_canvas_node(tmp_path):
    """A syntax error should fail packaging, not produce a package that cannot
    be imported."""
    plan = _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node("broken", "FUNCTION", {"name": "f", "code": "result = ((("}),
            _node("end", "END"),
        ],
        [_edge("start", "broken"), _edge("broken", "end")],
    )

    with pytest.raises(RenderError) as excinfo:
        render_package(plan, tmp_path / "pkg")

    message = str(excinfo.value)
    assert "'broken'" in message
    assert "invalid Python" in message
    # Nothing half-written left behind.
    assert not (tmp_path / "pkg").exists()


def test_invalid_condition_expression_names_the_canvas_node(tmp_path):
    plan = _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node("router", "CONDITION", {"branches": [{"name": "x", "expression": "a =="}]}),
            _node("end", "END"),
        ],
        [_edge("start", "router"), _edge("router", "end", "x")],
    )

    with pytest.raises(RenderError, match="'router'"):
        render_package(plan, tmp_path / "pkg")


def test_python_mode_transform_body_is_emitted_as_real_code(tmp_path):
    plan = _linear_plan(
        {"mode": "python", "expression": "result = {'doubled': data.get('n', 0) * 2}"}
    )
    render_package(plan, tmp_path / "pkg")
    mid = next(n for n in plan.nodes if n.node_type == "TRANSFORM")
    source = (tmp_path / "pkg" / f"nodes/{mid.name}.py").read_text()

    # Module-level Python, not a string handed to exec().
    assert "def _transform(data: dict) -> dict:" in source
    assert "result = {'doubled': data.get('n', 0) * 2}" in source
    assert "exec(" not in source


def test_unsupported_nodes_are_refused(tmp_path):
    from app.compiler.graph_plan import build_graph_plan as build
    from app.compiler.ir import compile_to_ir

    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", _START_CFG),
                _node("sub", "SUBWORKFLOW", {"workflow_id": "x"}),
                _node("end", "END"),
            ],
            "edges": [_edge("start", "sub"), _edge("sub", "end")],
        }
    )
    plan = build(compile_to_ir(canvas, "v"), workflow_name="Sub")

    with pytest.raises(RenderError, match="cannot be packaged yet"):
        render_package(plan, tmp_path / "pkg")


# ── End to end ───────────────────────────────────────────────────────────────


def _seed_files() -> list[Path]:
    if not SEED_DIR.is_dir():
        return []
    return sorted(p for p in SEED_DIR.glob("seed_*.py") if p.name != "seed_all.py")


@pytest.mark.skipif(not _seed_files(), reason="test-agents/workflows not present")
@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_seed_packages_render_and_pass_their_own_tests(path, tmp_path):
    """The Phase 4 gate: render a real workflow, then run the package's suite.

    The generated suite covers the graph's structure and an A2A round-trip
    including `tasks/get` polling, so this asserts the package actually works
    rather than merely that it was written.
    """
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    display = path.stem.removeprefix("seed_").replace("_", " ").title()
    canvas = CanvasPayload.model_validate(module.CANVAS)
    errors, _warnings, ir = run_compiler(canvas, f"{path.stem}-0001")
    assert errors == [], errors

    plan = build_graph_plan(ir, workflow_name=display, workflow_description=f"Seed: {display}")
    destination = tmp_path / "pkg"
    render_package(plan, destination)

    result = subprocess.run(
        # faulthandler_timeout makes a stalled test dump every thread's stack
        # into the output below instead of just timing out opaquely. This nested
        # run has been seen to crawl (8 of 47 tests in 330s) on a machine that
        # runs it in 7s otherwise, and without a stack there is nothing to chase.
        [
            sys.executable, "-m", "pytest", "-q", "--no-header",
            "-o", "faulthandler_timeout=90",
        ],
        cwd=str(destination),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"generated package for {path.stem} failed its own tests:\n"
        f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
    )


# ── on_error semantics ───────────────────────────────────────────────────────


def test_on_error_continue_flag_matches_the_templates():
    """`supports_on_error_continue` must agree with what the modules do.

    A node type declaring support whose template never reads ON_ERROR would
    silently ignore the setting; the reverse would make the validator warn about
    a setting that actually works.
    """
    from app.nodes.registry import NODE_REGISTRY
    from app.packaging.render import NODE_TEMPLATES, RENDER_TEMPLATE_DIR

    for node_type, definition in NODE_REGISTRY.items():
        template_name = NODE_TEMPLATES.get(node_type)
        if template_name is None:
            # No generated module, so nothing can honour the setting.
            assert not definition.supports_on_error_continue, node_type
            continue

        source = (RENDER_TEMPLATE_DIR / template_name).read_text()
        honours = "ON_ERROR" in source
        assert honours == definition.supports_on_error_continue, (
            f"{node_type}: template {'reads' if honours else 'ignores'} ON_ERROR but "
            f"supports_on_error_continue={definition.supports_on_error_continue}"
        )


def test_policies_reach_the_node_decorator(tmp_path):
    """timeout -> @node(timeout=), retries -> RetryConfig via NODE_KWARGS."""
    plan = _plan(
        [
            _node("start", "A2A_START", _START_CFG),
            _node(
                "slow",
                "TRANSFORM",
                {"mode": "jmespath", "expression": "text"},
                timeout=120,
                retries=3,
                on_error="continue",
            ),
            _node("end", "END"),
        ],
        [_edge("start", "slow"), _edge("slow", "end")],
    )
    render_package(plan, tmp_path / "pkg")
    slow = next(n for n in plan.nodes if n.canvas_id == "slow")
    source = (tmp_path / "pkg" / f"nodes/{slow.name}.py").read_text()

    assert "NODE_KWARGS(timeout=120, retries=3)" in source
    assert 'ON_ERROR = "continue"' in source


def test_a_single_attempt_emits_no_retry_config(tmp_path):
    render_package(_linear_plan(), tmp_path / "pkg")
    mid = next(n for n in _linear_plan().nodes if n.node_type == "TRANSFORM")
    source = (tmp_path / "pkg" / f"nodes/{mid.name}.py").read_text()

    assert "retries=" not in source, "one attempt means no RetryConfig"
    assert "NODE_KWARGS(timeout=60)" in source
