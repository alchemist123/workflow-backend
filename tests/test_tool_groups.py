"""Tool groups on the canvas: SEQUENTIAL_AGENT and PARALLEL_AGENT.

A group collects the tools wired into its `tools` handle and hands its consumer
one tool that runs them in order or all at once. It replaces the old
`tool_execution_mode` setting on ORCHESTRATOR_AGENT — the ordering is now drawn
rather than chosen from a dropdown.

The runtime behaviour of a group lives in the package template and is covered by
`app/packaging/template/tests/test_groups.py`. This file covers the canvas side:
declaration, validation, resolution and rendering.
"""

from __future__ import annotations

import json
import warnings

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.compiler.semantic_validator import validate_semantics
from app.nodes.registry import (
    NODE_REGISTRY,
    TOOL_CONSUMER_TYPES,
    TOOL_GROUP_TYPES,
    TOOL_PROVIDER_TYPES,
)
from app.packaging.render import render_package
from app.schemas.canvas import CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)


def _node(node_id: str, node_type: str, config: dict | None = None, **meta) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": meta.get("title", ""), "description": ""},
        "config": config if config is not None else {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1}, "on_error": "fail"},
    }


def _edge(source: str, target: str, handle: str = "output", target_handle: str = "input") -> dict:
    return {
        "id": f"e_{source}_{target}_{target_handle}_{handle}",
        "source": source,
        "source_handle": handle,
        "target": target,
        "target_handle": target_handle,
        "condition": None,
    }


_START = {
    "payload_schema": {
        "fields": [{"name": "q", "type": "string", "description": "", "required": True}]
    }
}


def _tools_edge(source: str, target: str) -> dict:
    return _edge(source, target, "output", "tools")


def _canvas(nodes: list[dict], edges: list[dict]) -> CanvasPayload:
    return CanvasPayload.model_validate({"nodes": nodes, "edges": edges})


def _grouped_canvas(group_type: str = "SEQUENTIAL_AGENT", group_config: dict | None = None):
    """A2A_START -> ORCHESTRATOR_AGENT -> END, with two tools inside a group."""
    return _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}, title="Brain"),
            _node("group", group_type, group_config or {"name": "pipeline"}, title="Pipeline"),
            _node("fetch", "TOOL", {"mcp_url": "http://mcp:9000", "tool_name": "fetch"}, title="Fetch"),
            _node("sum", "TOOL", {"mcp_url": "http://mcp:9000", "tool_name": "summarise"}, title="Sum"),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("fetch", "group"),
            _tools_edge("sum", "group"),
            _tools_edge("group", "orch"),
            _edge("orch", "end"),
        ],
    )


def _plan(canvas: CanvasPayload, version_id: str = "groups-0001"):
    errors, warns, ir = run_compiler(canvas, version_id)
    assert errors == [], errors
    return build_graph_plan(ir, workflow_name="Group Demo"), warns


# ── Declaration ──────────────────────────────────────────────────────────────


def test_both_group_types_are_registered():
    assert TOOL_GROUP_TYPES == {"SEQUENTIAL_AGENT", "PARALLEL_AGENT"}
    for node_type in TOOL_GROUP_TYPES:
        definition = NODE_REGISTRY[node_type]
        # A group takes children on its own tools handle...
        assert definition.accepts_tools
        assert definition.is_tool_group
        # ...and is itself something that can be wired into a tools handle.
        assert node_type in TOOL_PROVIDER_TYPES
        assert node_type in TOOL_CONSUMER_TYPES


def test_the_agent_types_accept_tools():
    assert TOOL_CONSUMER_TYPES >= {"ORCHESTRATOR_AGENT", "AGENT", "LLM_AGENT"}
    # An LLM_AGENT with tools becomes an agent loop; see the node's docstring.
    assert NODE_REGISTRY["LLM_AGENT"].accepts_tools


def test_tool_execution_mode_is_gone():
    """Replaced by drawing a group, which the old flag could never express: it
    said nothing about which tools to group, or in what order."""
    schema = json.dumps(NODE_REGISTRY["ORCHESTRATOR_AGENT"].config_schema)
    assert "tool_execution_mode" not in schema


def test_sequential_declares_an_order_and_parallel_a_concurrency_limit():
    sequential = NODE_REGISTRY["SEQUENTIAL_AGENT"].config_schema["properties"]
    parallel = NODE_REGISTRY["PARALLEL_AGENT"].config_schema["properties"]

    assert "order" in sequential
    assert sequential["stop_on_error"]["default"] is True

    assert "max_concurrency" in parallel
    # Off by default: one dead source should not lose the others.
    assert parallel["stop_on_error"]["default"] is False
    assert "order" not in parallel


# ── Validation ───────────────────────────────────────────────────────────────


def test_a_valid_grouped_canvas_passes():
    errors, warns = validate_semantics(_grouped_canvas(group_config={
        "name": "pipeline", "order": ["fetch", "sum"],
    }))
    assert errors == []
    assert warns == []


def test_a_group_with_no_order_warns():
    """Canvas order is a fallback, not an intention."""
    _errors, warns = validate_semantics(_grouped_canvas())
    assert any("no execution order" in w for w in warns)


def test_a_group_wired_into_the_flow_is_an_error():
    """A group is a tool. In the flow it would be a graph node with no
    behaviour, which is a silent no-op."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("group", "SEQUENTIAL_AGENT", {"name": "g"}),
            _node("tool", "TOOL", {"mcp_url": "http://m", "tool_name": "t"}),
            _node("end", "END", {}),
        ],
        [_edge("start", "group"), _tools_edge("tool", "group"), _edge("group", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("tool group" in e and "not into the workflow flow" in e for e in errors)


def test_a_group_with_no_children_is_an_error():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "m"}),
            _node("group", "PARALLEL_AGENT", {"name": "g"}),
            _node("end", "END", {}),
        ],
        [_edge("start", "orch"), _tools_edge("group", "orch"), _edge("orch", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("has no tools connected" in e for e in errors)


def test_a_group_connected_to_nothing_is_an_error():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("group", "SEQUENTIAL_AGENT", {"name": "g"}),
            _node("tool", "TOOL", {"mcp_url": "http://m", "tool_name": "t"}),
            _node("end", "END", {}),
        ],
        [_edge("start", "end"), _tools_edge("tool", "group")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("not connected to anything" in e for e in errors)


def test_a_group_with_one_child_warns():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "m"}),
            _node("group", "SEQUENTIAL_AGENT", {"name": "g", "order": ["tool"]}),
            _node("tool", "TOOL", {"mcp_url": "http://m", "tool_name": "t"}),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("tool", "group"),
            _tools_edge("group", "orch"),
            _edge("orch", "end"),
        ],
    )
    errors, warns = validate_semantics(canvas)
    assert errors == []
    assert any("only one tool" in w for w in warns)


def test_a_stale_order_entry_is_an_error():
    """The user removed a tool but left its id in the order."""
    canvas = _grouped_canvas(group_config={"name": "p", "order": ["fetch", "removed"]})
    errors, _ = validate_semantics(canvas)
    assert any("'removed'" in e and "not connected to it" in e for e in errors)


def test_a_duplicated_order_entry_is_an_error():
    canvas = _grouped_canvas(group_config={"name": "p", "order": ["fetch", "fetch", "sum"]})
    errors, _ = validate_semantics(canvas)
    assert any("more than once" in e for e in errors)


def test_a_tool_wired_into_a_node_with_no_tools_input_is_an_error():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("mid", "TRANSFORM", {"mode": "jmespath", "expression": "q"}),
            _node("tool", "TOOL", {"mcp_url": "http://m", "tool_name": "t"}),
            _node("end", "END", {}),
        ],
        [_edge("start", "mid"), _tools_edge("tool", "mid"), _edge("mid", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("has no tools input" in e for e in errors)


def test_a_loop_in_the_tool_wiring_is_an_error():
    """The flow-graph cycle check cannot see this: tool edges are excluded from
    that graph, so an unchecked loop would recurse until the compiler gave up."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "m"}),
            _node("a", "SEQUENTIAL_AGENT", {"name": "a"}),
            _node("b", "SEQUENTIAL_AGENT", {"name": "b"}),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("a", "b"),
            _tools_edge("b", "a"),
            _tools_edge("a", "orch"),
            _edge("orch", "end"),
        ],
    )
    errors, _ = validate_semantics(canvas)
    assert any("wired in a loop" in e for e in errors)


# ── Resolution ───────────────────────────────────────────────────────────────


def test_a_group_is_not_a_graph_node():
    plan, _ = _plan(_grouped_canvas(group_config={"name": "p", "order": ["fetch", "sum"]}))

    types = {n.node_type for n in plan.nodes}
    assert types == {"A2A_START", "ORCHESTRATOR_AGENT", "END"}
    # And it is not reported as unreachable — being a tool is its purpose.
    assert not any("unreachable" in w for w in plan.warnings)


def test_the_order_config_decides_execution_order():
    plan, _ = _plan(_grouped_canvas(group_config={"name": "p", "order": ["sum", "fetch"]}))

    orch = next(n for n in plan.nodes if n.node_type == "ORCHESTRATOR_AGENT")
    group = orch.config["resolved_tools"]["groups"][0]
    assert [child["name"] for child in group["children"]] == ["Sum", "Fetch"]


def test_an_unlisted_child_runs_last():
    """A half-filled order still produces a usable group."""
    plan, _ = _plan(_grouped_canvas(group_config={"name": "p", "order": ["sum"]}))

    orch = next(n for n in plan.nodes if n.node_type == "ORCHESTRATOR_AGENT")
    group = orch.config["resolved_tools"]["groups"][0]
    assert [child["name"] for child in group["children"]] == ["Sum", "Fetch"]


def test_mode_and_defaults_come_from_the_node_type():
    for group_type, mode, stop in (
        ("SEQUENTIAL_AGENT", "sequential", True),
        ("PARALLEL_AGENT", "parallel", False),
    ):
        plan, _ = _plan(_grouped_canvas(group_type, {"name": "p", "order": ["fetch", "sum"]}))
        orch = next(n for n in plan.nodes if n.node_type == "ORCHESTRATOR_AGENT")
        group = orch.config["resolved_tools"]["groups"][0]
        assert group["mode"] == mode
        assert group["stop_on_error"] is stop


def test_a_group_inside_a_group_resolves_recursively():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "m"}),
            _node("outer", "SEQUENTIAL_AGENT", {"name": "outer", "order": ["prep", "inner"]}),
            _node("prep", "FUNCTION", {"name": "prep", "code": "result = data"}),
            _node("inner", "PARALLEL_AGENT", {"name": "inner"}),
            _node("x", "REMOTE_AGENT", {"endpoint": "http://x", "name": "x"}),
            _node("y", "REMOTE_AGENT", {"endpoint": "http://y", "name": "y"}),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("prep", "outer"),
            _tools_edge("inner", "outer"),
            _tools_edge("x", "inner"),
            _tools_edge("y", "inner"),
            _tools_edge("outer", "orch"),
            _edge("orch", "end"),
        ],
    )
    plan, _ = _plan(canvas, "nested-0001")
    orch = next(n for n in plan.nodes if n.node_type == "ORCHESTRATOR_AGENT")
    outer = orch.config["resolved_tools"]["groups"][0]

    assert outer["mode"] == "sequential"
    # Order is preserved across a mix of leaf tools and sub-groups.
    assert [c.get("name") for c in outer["children"]] == ["prep", "inner"]
    nested = outer["children"][1]
    assert nested["kind"] == "group"
    assert nested["mode"] == "parallel"
    assert sorted(c["name"] for c in nested["children"]) == ["x", "y"]


def test_a_group_child_gets_its_own_env_keys():
    """Regression: credentials inside a group must leave the plan the same way
    a directly-wired tool's do."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "m"}),
            _node("group", "PARALLEL_AGENT", {"name": "g"}),
            _node("a", "REMOTE_AGENT", {"endpoint": "http://a", "auth_token": "SECRET-A", "name": "alpha"}),
            _node("b", "REMOTE_AGENT", {"endpoint": "http://b", "auth_token": "SECRET-B", "name": "beta"}),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("a", "group"),
            _tools_edge("b", "group"),
            _tools_edge("group", "orch"),
            _edge("orch", "end"),
        ],
    )
    plan, _ = _plan(canvas, "groupsecrets-0001")

    keys = {k.key for k in plan.env_keys}
    assert {"A2A_ALPHA_URL", "A2A_ALPHA_TOKEN", "A2A_BETA_URL", "A2A_BETA_TOKEN"} <= keys

    # And the credentials are out of the plan entirely.
    blob = json.dumps(plan.to_dict())
    assert "SECRET-A" not in blob
    assert "SECRET-B" not in blob


# ── Rendering ────────────────────────────────────────────────────────────────


def test_groups_reach_the_generated_agent_module(tmp_path):
    plan, _ = _plan(_grouped_canvas(group_config={"name": "pipeline", "order": ["fetch", "sum"]}))
    render_package(plan, tmp_path / "pkg")

    orch = next(n for n in plan.nodes if n.node_type == "ORCHESTRATOR_AGENT")
    source = (tmp_path / "pkg" / f"nodes/{orch.name}.py").read_text()

    assert "TOOL_GROUPS: list[dict] = [" in source
    assert "'mode': 'sequential'" in source
    assert "groups=_resolve_env(TOOL_GROUPS)" in source
    # Env references, not values.
    assert "MCP_FETCH_URL" in source
    assert "http://mcp:9000" not in source


def test_an_llm_agent_without_tools_stays_a_direct_call(tmp_path):
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("model", "LLM_AGENT", {"provider": "google", "model": "gemini-2.5-flash"}),
            _node("end", "END", {}),
        ],
        [_edge("start", "model"), _edge("model", "end")],
    )
    plan, _ = _plan(canvas, "model-plain-0001")
    render_package(plan, tmp_path / "pkg")

    model = next(n for n in plan.nodes if n.node_type == "LLM_AGENT")
    source = (tmp_path / "pkg" / f"nodes/{model.name}.py").read_text()

    assert "generate_content" in source
    assert "LlmAgent" not in source, "no tools and no output structure needs no agent loop"


def test_an_llm_agent_with_tools_becomes_an_agent_loop(tmp_path):
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("model", "LLM_AGENT", {"provider": "google", "model": "gemini-2.5-flash"}),
            _node("tool", "TOOL", {"mcp_url": "http://m", "tool_name": "t"}, title="T"),
            _node("end", "END", {}),
        ],
        [_edge("start", "model"), _tools_edge("tool", "model"), _edge("model", "end")],
    )
    plan, _ = _plan(canvas, "model-tools-0001")
    render_package(plan, tmp_path / "pkg")

    model = next(n for n in plan.nodes if n.node_type == "LLM_AGENT")
    source = (tmp_path / "pkg" / f"nodes/{model.name}.py").read_text()

    assert "LlmAgent" in source
    assert "collect_tools" in source
    assert "MCP_T_URL" in source


def test_a_group_pulls_in_the_mcp_dependency(tmp_path):
    """Requirements must see a tool nested inside a group."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "m"}),
            _node("group", "PARALLEL_AGENT", {"name": "g"}),
            _node("t1", "TOOL", {"mcp_url": "http://m", "tool_name": "a"}, title="T1"),
            _node("t2", "TOOL", {"mcp_url": "http://m", "tool_name": "b"}, title="T2"),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("t1", "group"),
            _tools_edge("t2", "group"),
            _tools_edge("group", "orch"),
            _edge("orch", "end"),
        ],
    )
    plan, _ = _plan(canvas, "groupdeps-0001")
    render_package(plan, tmp_path / "pkg")

    requirements = (tmp_path / "pkg" / "requirements.txt").read_text()
    assert "mcp>=" in requirements, "an MCP tool inside a group still needs the client"


# ── Migration ────────────────────────────────────────────────────────────────


def test_tool_execution_mode_is_migrated_away():
    from app.compiler.canvas_migrations import migrate_canvas

    canvas = {
        "schema_version": 2,
        "nodes": [
            {
                "id": "o",
                "type": "ORCHESTRATOR_AGENT",
                "config": {"model": "m", "tool_execution_mode": "parallel"},
                "metadata": {"title": "Brain", "description": ""},
            }
        ],
        "edges": [],
    }
    migrated, notes = migrate_canvas(canvas)

    assert migrated["schema_version"] >= 3
    assert "tool_execution_mode" not in migrated["nodes"][0]["config"]
    assert any("tool_execution_mode" in note for note in notes)
    # The old flag cannot be turned into a wiring automatically, so the user is
    # told what to do instead.
    assert "Parallel Tools" in migrated["nodes"][0]["metadata"]["description"]

    assert migrate_canvas(migrated)[1] == [], "migration must be idempotent"


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
def test_a_migrated_canvas_still_compiles(mode):
    from app.compiler.canvas_migrations import migrate_canvas

    canvas = {
        "schema_version": 2,
        "nodes": [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "m", "tool_execution_mode": mode}),
            _node("end", "END", {}),
        ],
        "edges": [_edge("start", "orch"), _edge("orch", "end")],
    }
    migrated, _notes = migrate_canvas(canvas)
    errors, _warns, ir = run_compiler(CanvasPayload.model_validate(migrated), "mig-0001")

    assert errors == [], errors
    assert ir is not None
