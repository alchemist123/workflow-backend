"""IR -> ADK graph plan.

The important assertions here do not check the plan's shape in the abstract —
they hand it to ADK and let ADK judge it (`verify_plan_builds`). That keeps the
compiler honest about the constraints rather than encoding a second opinion.
"""

from __future__ import annotations

import warnings

import pytest

from app.compiler import run_compiler
from app.compiler.graph_check import build_adk_workflow, verify_plan_builds
from app.compiler.graph_plan import (
    ENTRY_NODE_NAME,
    GRAPH_SCHEMA_VERSION,
    GraphPlanError,
    assign_names,
    build_graph_plan,
    workflow_identifier,
)
from app.compiler.ir import compile_to_ir
from app.schemas.canvas import CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)


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
        "fields": [{"name": "text", "type": "string", "description": "", "required": True}]
    }
}


def _plan(nodes: list[dict], edges: list[dict], *, name: str = "wf", compile_first: bool = True):
    canvas = CanvasPayload.model_validate({"nodes": nodes, "edges": edges})
    if compile_first:
        errors, _warnings, ir = run_compiler(canvas, "v-test")
        assert errors == [], f"canvas did not validate: {errors}"
    else:
        # Bypass validation to exercise the planner's own backstops.
        ir = compile_to_ir(canvas, "v-test")
    return build_graph_plan(ir, workflow_name=name, workflow_description="d")


def _linear() -> tuple[list[dict], list[dict]]:
    return (
        [
            _node("start", "A2A_START", _START_CFG),
            _node("mid", "TRANSFORM", {"mode": "jmespath", "expression": "text"}),
            _node("end", "END"),
        ],
        [_edge("start", "mid"), _edge("mid", "end")],
    )


# ── Shape ────────────────────────────────────────────────────────────────────


def test_linear_workflow_plan():
    plan = _plan(*_linear())

    assert plan.entry_node == ENTRY_NODE_NAME
    assert plan.terminal_node.startswith("n_end_")
    assert [e.from_node for e in plan.edges][0] == "START"
    assert plan.warnings == []
    assert verify_plan_builds(plan) == []


def test_plan_serialises_to_the_graph_json_contract():
    plan = _plan(*_linear())
    data = plan.to_dict()

    assert data["schema_version"] == GRAPH_SCHEMA_VERSION
    assert set(data) >= {
        "workflow", "entry_node", "terminal_node", "nodes", "edges",
        "join_nodes", "env_keys",
    }
    assert data["workflow"]["name"] == "wf"
    assert data["workflow"]["version_id"] == "v-test"
    for node in data["nodes"]:
        assert set(node) >= {"name", "canvas_id", "type", "module", "timeout", "retries", "on_error"}
        assert node["module"] == f"nodes.{node['name']}"
    for edge in data["edges"]:
        assert set(edge) == {"from", "to", "route"}


def test_policies_carry_into_the_plan():
    nodes, edges = _linear()
    nodes[1] = _node(
        "mid", "TRANSFORM", {"mode": "jmespath", "expression": "text"},
        timeout=120, retries=3, on_error="continue",
    )
    plan = _plan(nodes, edges)

    mid = next(n for n in plan.nodes if n.canvas_id == "mid")
    assert mid.timeout == 120
    assert mid.retries == 3
    assert mid.on_error == "continue"


def test_titles_and_descriptions_are_carried():
    nodes, edges = _linear()
    nodes[1] = _node(
        "mid", "TRANSFORM", {"mode": "jmespath", "expression": "text"},
        title="Shape it", description="Pulls the text field out",
    )
    plan = _plan(nodes, edges)

    mid = next(n for n in plan.nodes if n.canvas_id == "mid")
    assert mid.title == "Shape it"
    assert mid.description == "Pulls the text field out"


# ── Naming ───────────────────────────────────────────────────────────────────


def test_entry_node_is_named_a2a_start():
    plan = _plan(*_linear())
    entry = next(n for n in plan.nodes if n.is_entry)
    assert entry.name == ENTRY_NODE_NAME


def test_names_are_valid_unique_identifiers():
    nodes = [
        _node("node_1757312094821_1", "A2A_START", _START_CFG),
        _node("node_1757312094821_2", "TRANSFORM", {"mode": "jmespath", "expression": "a"}),
        _node("node_1757312094821_3", "TRANSFORM", {"mode": "jmespath", "expression": "b"}),
        _node("node_1757312094821_4", "END"),
    ]
    edges = [
        _edge("node_1757312094821_1", "node_1757312094821_2"),
        _edge("node_1757312094821_2", "node_1757312094821_3"),
        _edge("node_1757312094821_3", "node_1757312094821_4"),
    ]
    plan = _plan(nodes, edges)

    names = [n.name for n in plan.nodes]
    assert len(set(names)) == len(names)
    for name in names:
        assert name.isidentifier(), name


def test_adding_a_node_does_not_rename_existing_ones():
    """Canvas ids are creation-ordered, so a new node takes the next ordinal.

    Ordering by graph position instead would renumber everything downstream of
    an insertion and rewrite most of the package on a one-node change.
    """
    ids = ["node_100_1", "node_100_2", "node_100_3"]
    nodes = [
        _node(ids[0], "A2A_START", _START_CFG),
        _node(ids[1], "TRANSFORM", {"mode": "jmespath", "expression": "a"}),
        _node(ids[2], "END"),
    ]
    edges = [_edge(ids[0], ids[1]), _edge(ids[1], ids[2])]
    before = {n.canvas_id: n.name for n in _plan(nodes, edges).nodes}

    # Insert a node created later (higher id) in the middle of the flow.
    new_id = "node_999_4"
    nodes.append(_node(new_id, "TRANSFORM", {"mode": "jmespath", "expression": "b"}))
    edges = [_edge(ids[0], ids[1]), _edge(ids[1], new_id), _edge(new_id, ids[2])]
    after = {n.canvas_id: n.name for n in _plan(nodes, edges).nodes}

    for canvas_id, name in before.items():
        assert after[canvas_id] == name, f"{canvas_id} was renamed {name} -> {after[canvas_id]}"
    assert after[new_id] not in before.values()


def test_naming_is_deterministic():
    nodes, edges = _linear()
    first = {n.canvas_id: n.name for n in _plan(nodes, edges).nodes}
    second = {n.canvas_id: n.name for n in _plan(nodes, edges).nodes}
    assert first == second


def test_assign_names_survives_a_keyword_like_type():
    """A node type that slugs to a Python keyword must not produce a bad name."""
    canvas = CanvasPayload.model_validate({"nodes": _linear()[0], "edges": _linear()[1]})
    ir = compile_to_ir(canvas, "v")
    ir.nodes["mid"].node_type = "class"  # not a real type; exercises the guard

    names = assign_names(ir, list(ir.nodes))
    for name in names.values():
        assert name.isidentifier()
        import keyword

        assert not keyword.iskeyword(name)


# ── Conditional routing ──────────────────────────────────────────────────────


def _condition_canvas(same_target: bool = False) -> tuple[list[dict], list[dict]]:
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node(
            "router",
            "CONDITION",
            {
                "branches": [
                    {"name": "yes", "expression": "True"},
                    {"name": "no", "expression": "False"},
                ]
            },
        ),
        _node("end", "END"),
    ]
    edges = [_edge("start", "router")]
    if same_target:
        edges += [_edge("router", "end", "yes"), _edge("router", "end", "no")]
    else:
        nodes.insert(2, _node("left", "TRANSFORM", {"mode": "jmespath", "expression": "a"}))
        nodes.insert(3, _node("right", "TRANSFORM", {"mode": "jmespath", "expression": "b"}))
        edges += [
            _edge("router", "left", "yes"),
            _edge("router", "right", "no"),
            _edge("left", "end"),
            _edge("right", "end"),
        ]
    return nodes, edges


def test_condition_branches_become_routes():
    plan = _plan(*_condition_canvas())

    router = next(n for n in plan.nodes if n.node_type == "CONDITION")
    assert sorted(router.routes) == ["no", "yes"]

    routed = {e.route for e in plan.edges if e.from_node == router.name}
    assert routed == {"yes", "no"}
    assert verify_plan_builds(plan) == []


def test_default_branch_is_recorded():
    nodes, edges = _condition_canvas()
    edges.append(_edge("router", "left", "default"))
    plan = _plan(nodes, edges)

    router = next(n for n in plan.nodes if n.node_type == "CONDITION")
    assert router.default_route == "default"


def test_two_branches_to_one_target_merge_into_a_single_edge():
    """ADK: 'Duplicate edge found: from=X, to=Y' even when routes differ."""
    plan = _plan(*_condition_canvas(same_target=True))

    router = next(n for n in plan.nodes if n.node_type == "CONDITION")
    router_edges = [e for e in plan.edges if e.from_node == router.name]

    assert len(router_edges) == 1
    assert sorted(router_edges[0].route) == ["no", "yes"]
    assert any("merged into one edge" in w for w in plan.warnings)
    # The real proof: ADK accepts the merged form.
    assert verify_plan_builds(plan) == []


def test_unmerged_duplicate_would_have_been_rejected_by_adk():
    """Show the merge is load-bearing, not defensive."""
    from google.adk import Workflow
    from google.adk.workflow import Edge

    plan = _plan(*_condition_canvas(same_target=True))
    workflow = build_adk_workflow(plan)  # merged form builds

    # Now rebuild with the routes split across two edges, as the canvas had them.
    router = next(n for n in plan.nodes if n.node_type == "CONDITION")
    nodes = {}
    for edge in workflow.edges:
        for candidate in (getattr(edge, "from_node", None), getattr(edge, "to_node", None)):
            if candidate is not None:
                nodes[candidate.name] = candidate
        if isinstance(edge, tuple):
            for item in edge:
                if hasattr(item, "name"):
                    nodes[item.name] = item

    split = [
        e for e in workflow.edges
        if not (hasattr(e, "from_node") and e.from_node.name == router.name)
    ]
    target = nodes[plan.terminal_node]
    split += [
        Edge(from_node=nodes[router.name], to_node=target, route="yes"),
        Edge(from_node=nodes[router.name], to_node=target, route="no"),
    ]

    with pytest.raises(Exception, match="Duplicate edge"):
        Workflow(name="split", edges=split)


def test_plain_duplicate_edges_collapse_to_one():
    nodes, edges = _linear()
    edges.append(_edge("start", "mid", "error"))  # a second plain edge, same pair
    plan = _plan(nodes, edges)

    pairs = [(e.from_node, e.to_node) for e in plan.edges]
    assert len(pairs) == len(set(pairs))
    assert any("collapsed into one" in w for w in plan.warnings)
    assert verify_plan_builds(plan) == []


# ── Fan-out and join ─────────────────────────────────────────────────────────


def test_parallel_fork_fans_out_with_plain_edges_and_joins_on_merge():
    """PARALLEL_FORK handles are fan-out labels, not route values: ADK dispatches
    a plain edge to every successor."""
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        # PARALLEL_FORK names its branches in config; the edges then use those
        # names as source handles.
        _node("fork", "PARALLEL_FORK", {"branches": ["a", "b"]}),
        _node("a", "TRANSFORM", {"mode": "jmespath", "expression": "a"}),
        _node("b", "TRANSFORM", {"mode": "jmespath", "expression": "b"}),
        _node("merge", "MERGE"),
        _node("end", "END"),
    ]
    edges = [
        _edge("start", "fork"),
        _edge("fork", "a", "a"),
        _edge("fork", "b", "b"),
        _edge("a", "merge"),
        _edge("b", "merge"),
        _edge("merge", "end"),
    ]
    plan = _plan(nodes, edges)

    fork = next(n for n in plan.nodes if n.node_type == "PARALLEL_FORK")
    fork_edges = [e for e in plan.edges if e.from_node == fork.name]
    assert len(fork_edges) == 2
    assert all(e.route is None for e in fork_edges), "fan-out edges carry no route"

    merge = next(n for n in plan.nodes if n.node_type == "MERGE")
    assert merge.is_join
    assert plan.join_nodes == [merge.name]

    assert verify_plan_builds(plan) == []


# ── Loops ────────────────────────────────────────────────────────────────────


def test_loop_compiles_to_a_back_edge():
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node("loop", "LOOP", {"mode": "while", "exit_condition": "i > 3", "max_iterations": 5}),
        _node("body", "TRANSFORM", {"mode": "jmespath", "expression": "a"}),
        _node("end", "END"),
    ]
    edges = [
        _edge("start", "loop"),
        _edge("loop", "body", "loop_body"),
        _edge("body", "loop"),
        _edge("loop", "end", "done"),
    ]
    plan = _plan(nodes, edges)

    loop = next(n for n in plan.nodes if n.node_type == "LOOP")
    body = next(n for n in plan.nodes if n.canvas_id == "body")
    assert sorted(loop.routes) == ["done", "loop_body"]

    # The back edge body -> loop is what makes it a loop.
    assert any(e.from_node == body.name and e.to_node == loop.name for e in plan.edges)
    assert verify_plan_builds(plan) == []


# ── Terminal detection ───────────────────────────────────────────────────────


def test_two_terminals_are_reported_with_canvas_ids():
    """The planner is the backstop; the validator reports this first."""
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node(
            "router", "CONDITION",
            {"branches": [{"name": "a", "expression": "True"}, {"name": "b", "expression": "False"}]},
        ),
        _node("end_a", "END"),
        _node("end_b", "END"),
    ]
    edges = [_edge("start", "router"), _edge("router", "end_a", "a"), _edge("router", "end_b", "b")]

    with pytest.raises(GraphPlanError) as excinfo:
        _plan(nodes, edges, compile_first=False)

    message = str(excinfo.value)
    assert "2 terminal nodes" in message
    assert "'end_a'" in message and "'end_b'" in message
    assert "MERGE" in message


def test_no_entry_node_is_reported():
    nodes = [_node("mid", "TRANSFORM", {"mode": "jmespath", "expression": "a"}), _node("end", "END")]
    with pytest.raises(GraphPlanError, match="no A2A_START"):
        _plan(nodes, [_edge("mid", "end")], compile_first=False)


def test_two_entry_nodes_are_reported():
    nodes = [
        _node("s1", "A2A_START", _START_CFG),
        _node("s2", "A2A_START", _START_CFG),
        _node("end", "END"),
    ]
    with pytest.raises(GraphPlanError, match="2 entry nodes"):
        _plan(nodes, [_edge("s1", "end"), _edge("s2", "end")], compile_first=False)


def test_terminal_that_is_not_an_end_node_warns():
    """Only an END node emits content, and without content the A2A task hangs."""
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node("tail", "TRANSFORM", {"mode": "jmespath", "expression": "a"}),
    ]
    plan = _plan(nodes, [_edge("start", "tail")], compile_first=False)

    assert plan.terminal_node.startswith("n_transform_")
    assert any("never complete" in w for w in plan.warnings)


# ── Tool providers are tools, not nodes ──────────────────────────────────────


def test_orchestrator_tool_providers_are_excluded_from_the_graph():
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}, title="Brain"),
        _node("tool", "TOOL", {"mcp_url": "http://mcp:9000", "tool_name": "search"}, title="Search"),
        _node("remote", "REMOTE_AGENT", {"endpoint": "http://agent:8001", "name": "helper"}),
        _node("end", "END"),
    ]
    edges = [
        _edge("start", "orch"),
        _edge("tool", "orch", "output", "tools"),
        _edge("remote", "orch", "output", "tools"),
        _edge("orch", "end"),
    ]
    plan = _plan(nodes, edges)

    types = {n.node_type for n in plan.nodes}
    assert types == {"A2A_START", "ORCHESTRATOR_AGENT", "END"}
    assert "TOOL" not in types and "REMOTE_AGENT" not in types
    # They are not reported as unreachable — being tools is their purpose.
    assert not any("unreachable" in w for w in plan.warnings)
    assert verify_plan_builds(plan) == []


def test_tool_providers_still_get_env_keys():
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
        _node("tool", "TOOL", {"mcp_url": "http://mcp:9000", "tool_name": "s"}, title="Search API"),
        _node("remote", "REMOTE_AGENT", {"endpoint": "http://a:1", "name": "Diet Advisor"}),
        _node("end", "END"),
    ]
    edges = [
        _edge("start", "orch"),
        _edge("tool", "orch", "output", "tools"),
        _edge("remote", "orch", "output", "tools"),
        _edge("orch", "end"),
    ]
    plan = _plan(nodes, edges)
    keys = {k.key for k in plan.env_keys}

    assert "MCP_SEARCH_API_URL" in keys
    assert "A2A_DIET_ADVISOR_URL" in keys
    assert "A2A_DIET_ADVISOR_TOKEN" in keys
    assert "MCP_TRANSPORT" in keys


# ── Environment keys ─────────────────────────────────────────────────────────


def test_baseline_env_keys_are_always_present():
    plan = _plan(*_linear())
    keys = {k.key for k in plan.env_keys}

    assert {
        "PORT", "AGENT_NAME", "AGENT_DESCRIPTION", "AGENT_VERSION",
        "CLOUD_RUN_URL", "A2A_AUTH_TOKEN", "TASK_STORE_DSN",
    } <= keys
    # A workflow with no model node should not demand GCP settings.
    assert "GOOGLE_CLOUD_PROJECT" not in keys


def test_llm_env_keys_appear_only_with_an_agent_node():
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
        _node("end", "END"),
    ]
    plan = _plan(nodes, [_edge("start", "orch"), _edge("orch", "end")])
    keys = {k.key: k for k in plan.env_keys}

    assert "GOOGLE_CLOUD_PROJECT" in keys
    assert keys["VERTEX_AI_LOCATION"].default == "us-central1"
    assert keys["LLM_MODEL"].default == "gemini-2.5-flash"
    orch = next(n for n in plan.nodes if n.node_type == "ORCHESTRATOR_AGENT")
    assert orch.name in keys["GOOGLE_CLOUD_PROJECT"].needed_by


def test_env_key_names_are_unique_even_with_duplicate_titles():
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node("t1", "TOOL", {"mcp_url": "http://a", "tool_name": "x"}, title="Search"),
        _node("t2", "TOOL", {"mcp_url": "http://b", "tool_name": "y"}, title="Search"),
        _node("end", "END"),
    ]
    edges = [_edge("start", "t1"), _edge("t1", "t2"), _edge("t2", "end")]
    plan = _plan(nodes, edges)

    keys = [k.key for k in plan.env_keys]
    assert len(keys) == len(set(keys))
    assert "MCP_SEARCH_URL" in keys
    assert "MCP_SEARCH_2_URL" in keys


def test_env_bindings_are_attached_to_the_node_config():
    """The renderer reads these instead of re-deriving var names."""
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node("remote", "REMOTE_AGENT", {"endpoint": "http://a:1"}, title="Helper"),
        _node("end", "END"),
    ]
    plan = _plan(nodes, [_edge("start", "remote"), _edge("remote", "end")])

    remote = next(n for n in plan.nodes if n.node_type == "REMOTE_AGENT")
    assert remote.config["_env"]["endpoint"] == "A2A_HELPER_URL"
    assert remote.config["_env"]["auth_token"] == "A2A_HELPER_TOKEN"


# ── Unsupported node types ───────────────────────────────────────────────────


@pytest.mark.parametrize("node_type", ["SUBWORKFLOW"])
def test_unsupported_node_types_are_flagged_not_silently_generated(node_type):
    config = {"workflow_id": "x"} if node_type == "SUBWORKFLOW" else {}
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node("special", node_type, config),
        _node("end", "END"),
    ]
    plan = _plan(nodes, [_edge("start", "special"), _edge("special", "end")], compile_first=False)

    special = next(n for n in plan.nodes if n.node_type == node_type)
    assert special.supported is False
    assert plan.unsupported == [special]
    assert any("not yet generated" in w for w in plan.warnings)
    assert special.to_dict()["supported"] is False


# ── Unreachable nodes ────────────────────────────────────────────────────────


def test_unreachable_node_is_dropped_with_a_warning():
    nodes = [
        _node("start", "A2A_START", _START_CFG),
        _node("island", "TRANSFORM", {"mode": "jmespath", "expression": "a"}),
        _node("end", "END"),
    ]
    plan = _plan(nodes, [_edge("start", "end")], compile_first=False)

    assert "island" not in plan.canvas_ids.values()
    assert any("island" in w and "unreachable" in w for w in plan.warnings)


# ── Workflow name ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("display", "expected"),
    [
        ("Phase 3 Router", "phase_3_router"),
        ("my-workflow", "my_workflow"),
        ("Text Triage!", "text_triage"),
        ("123 go", "wf_123_go"),   # cannot start with a digit
        ("class", "workflow"),     # a python keyword
        ("   ", "workflow"),       # nothing usable
    ],
)
def test_workflow_identifier(display, expected):
    assert workflow_identifier(display) == expected


def test_display_name_with_spaces_still_builds_under_adk():
    """ADK validates Workflow(name=...) as a node name.

    A display name like "Phase 3 Router" is not an identifier, so the plan keeps
    a separate slug. Regression: this was caught by the ADK gate in the
    packaging endpoint, not by the plan's own assertions.
    """
    nodes, edges = _linear()
    canvas = CanvasPayload.model_validate({"nodes": nodes, "edges": edges})
    _e, _w, ir = run_compiler(canvas, "v")
    plan = build_graph_plan(ir, workflow_name="Phase 3 Router")

    assert plan.workflow_name == "Phase 3 Router"   # kept for the agent card
    assert plan.workflow_slug == "phase_3_router"   # used for the ADK graph
    assert verify_plan_builds(plan) == []


def test_agent_name_default_is_the_slug():
    """AGENT_NAME reaches Workflow(name=...) in the generated package."""
    nodes, edges = _linear()
    canvas = CanvasPayload.model_validate({"nodes": nodes, "edges": edges})
    _e, _w, ir = run_compiler(canvas, "v")
    plan = build_graph_plan(ir, workflow_name="My Cool Workflow")

    agent_name = next(k for k in plan.env_keys if k.key == "AGENT_NAME")
    assert agent_name.default == "my_cool_workflow"
    assert agent_name.default.isidentifier()
