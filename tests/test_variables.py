"""Named variables: a node saves its result, later nodes read it by name.

ADK graphs have no separate variable concept — variables *are* session-state
keys, bound by name. Verified against 2.8.0: a node returning
`Event(state={"customer": "ACME"})` makes a later node's `customer` parameter
arrive as `"ACME"`, and the session holds a flat `{'customer': 'ACME'}`. So a
canvas variable here is a flat, top-level state key, which is also where
`LlmAgent.output_key` already writes.

The saving happens in one place — the `flow_node` decorator — rather than at
each node's `return`. The node modules have 27 return sites between them, and a
variable saved on some paths but not others would be worse than none.
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.compiler.semantic_validator import validate_semantics
from app.nodes.registry import NODE_REGISTRY
from app.nodes.variables import name_error
from app.packaging.render import render_package
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)


def _node(node_id, node_type, config=None):
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": node_id, "description": ""},
        "config": config or {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1},
                     "on_error": "fail"},
    }


def _edge(source, target, handle="output"):
    return {
        "id": f"e_{source}_{target}_{handle}",
        "source": source, "source_handle": handle,
        "target": target, "target_handle": "input", "condition": None,
    }


_START = {
    "input_mode": "json", "state_key": "wf",
    "payload_schema": {"fields": [
        {"name": "sku", "type": "string", "description": "", "required": True},
        {"name": "qty", "type": "integer", "description": "", "required": True},
    ]},
}


def _render(canvas: dict, name: str, tmp_path):
    errors, _warnings, ir = run_compiler(CanvasPayload.model_validate(canvas), name)
    assert errors == [], errors
    destination = tmp_path / "pkg"
    render_package(build_graph_plan(ir, workflow_name=name), destination)
    return destination


def _run(destination, payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, "run_once.py", json.dumps(payload), "--json"],
        cwd=str(destination), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode in (0, 1), f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── Names ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["order", "priced_order", "a", "x1", "Total"],
)
def test_usable_names_are_accepted(name):
    assert name_error(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "my var",     # a node parameter cannot be called that
        "1st",
        "with-dash",
        "wf",         # the workflow's own accumulated payload
        "_hidden",
        "_loop_x_i",  # a loop's counters
        "temp:x",     # ADK scope prefixes
        "app:x",
        "user:x",
    ],
)
def test_unusable_names_are_rejected(name):
    assert name_error(name) is not None, name


def test_an_empty_name_is_not_an_error():
    """Naming a variable is optional."""
    assert name_error("") is None
    assert name_error("   ") is None


# ── Declaration ──────────────────────────────────────────────────────────────


def test_every_node_that_produces_a_result_can_name_it():
    """One config field injected at registration, not repeated per node class."""
    without = {
        node_type
        for node_type, definition in NODE_REGISTRY.items()
        if "output_variable" not in definition.config_schema.get("properties", {})
    }
    # A tool group is resolved into its consumer's tool list; MERGE is a
    # JoinNode with no module; a fork hands each branch the payload it was
    # given unchanged. None of them produces a result of its own, and naming a
    # fork's would just save a second copy of the previous node's output.
    assert without == {
        "SEQUENTIAL_AGENT", "PARALLEL_AGENT", "MERGE", "PARALLEL_FORK",
    }


def test_saving_is_wired_once_in_the_decorator(tmp_path):
    """Not at each return: 27 return sites is 27 chances to miss one."""
    canvas = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", {**_START, "output_variable": "order"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [_edge("start", "end")],
    }
    destination = _render(canvas, "vars-wiring-0001", tmp_path)
    source = (destination / "nodes" / "a2a_start.py").read_text()

    assert 'OUTPUT_VARIABLE = "order"' in source
    assert "@flow_node(name=NODE_ID, variable=OUTPUT_VARIABLE" in source


# ── Behaviour ────────────────────────────────────────────────────────────────


def _pipeline(extra_label: str = "") -> dict:
    """start(order) -> price(priced) -> label -> route -> tier -> end."""
    label_expression = extra_label or (
        "result = {'label': vars['order']['sku'] + ' x' "
        "+ str(vars['order']['qty']) + ' = ' + str(vars['priced']['total'])}"
    )
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", {**_START, "output_variable": "order"}),
            _node("price", "TRANSFORM", {
                "mode": "python", "output_variable": "priced",
                "expression": "result = {'total': (data.get('qty') or 0) * 25}",
            }),
            _node("label", "TRANSFORM", {"mode": "python", "expression": label_expression}),
            _node("route", "CONDITION", {"branches": [
                {"name": "big", "expression": "vars['priced']['total'] >= 100"},
                {"name": "small", "expression": "True"},
            ]}),
            _node("big", "TRANSFORM", {"mode": "python",
                                       "expression": "result = {'tier': 'bulk'}"}),
            _node("small", "TRANSFORM", {"mode": "python",
                                         "expression": "result = {'tier': 'standard'}"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [
            _edge("start", "price"), _edge("price", "label"), _edge("label", "route"),
            _edge("route", "big", "big"), _edge("route", "small", "small"),
            _edge("big", "end"), _edge("small", "end"),
        ],
    }


def test_a_transform_reads_a_variable_from_two_nodes_back(tmp_path):
    """The point of variables.

    `label`'s edge payload carries only the price step's output, but the entry
    node's payload is still reachable by name.
    """
    destination = _render(_pipeline(), "vars-read-0001", tmp_path)
    output = _run(destination, {"sku": "WIDGET", "qty": 2})["result"]

    assert output["label"] == "WIDGET x2 = 50"


def test_a_condition_can_branch_on_a_variable(tmp_path):
    destination = _render(_pipeline(), "vars-branch-0001", tmp_path)

    small = _run(destination, {"sku": "WIDGET", "qty": 2})["result"]
    big = _run(destination, {"sku": "WIDGET", "qty": 8})["result"]

    assert small["tier"] == "standard"
    assert big["tier"] == "bulk"


def test_variables_land_as_top_level_state_keys(tmp_path):
    """Flat, the way ADK does it — not nested inside the `wf` payload."""
    destination = _render(
        _pipeline("result = {'seen': sorted(vars)}"), "vars-flat-0001", tmp_path
    )
    output = _run(destination, {"sku": "WIDGET", "qty": 2})["result"]

    assert output["seen"] == ["order", "priced"]
    # The workflow's own accumulated payload is not a variable.
    assert "wf" not in output["seen"]


def test_an_unnamed_node_saves_nothing(tmp_path):
    destination = _render(
        _pipeline("result = {'seen': sorted(vars)}"), "vars-none-0001", tmp_path
    )
    source = (destination / "nodes").glob("*condition*.py")
    assert next(source, None) is not None
    # `label` and the tier nodes name nothing, so only the two named ones show.
    output = _run(destination, {"sku": "W", "qty": 1})["result"]
    assert output["seen"] == ["order", "priced"]


# ── Validation ───────────────────────────────────────────────────────────────


def test_a_bad_variable_name_is_a_compile_error():
    canvas = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", {**_START, "output_variable": "wf"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [_edge("start", "end")],
    }
    errors, _warnings = validate_semantics(CanvasPayload.model_validate(canvas))
    assert any("reserved" in e for e in errors), errors


def test_two_nodes_saving_the_same_name_warns():
    canvas = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", {**_START, "output_variable": "dup"}),
            _node("t", "TRANSFORM", {"mode": "python", "output_variable": "dup",
                                     "expression": "result = {'a': 1}"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [_edge("start", "t"), _edge("t", "end")],
    }
    _errors, warnings_out = validate_semantics(CanvasPayload.model_validate(canvas))
    assert any("also saves to" in w for w in warnings_out), warnings_out


# ── The ADK behaviour this is built on ───────────────────────────────────────


def test_adk_binds_node_parameters_from_state_by_name():
    """Why variables are flat top-level state keys rather than a nested blob.

    `parameter_binding` defaults to `'state'`, so a parameter named `customer`
    is looked up in `ctx.state`. `node_input` is special-cased as the edge
    payload.
    """
    import asyncio

    from google.adk import Event, Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import START, Workflow, node
    from google.genai import types

    @node(name="setter")
    async def setter(ctx, node_input=None):
        return Event(output={"edge": "payload"}, state={"customer": "ACME"})

    seen: dict = {}

    @node(name="reader")
    async def reader(ctx, customer=None, node_input=None):
        seen["customer"] = customer
        seen["node_input"] = node_input
        return Event(output={"ok": True})

    workflow = Workflow(name="binding", description="state binding",
                        edges=[(START, setter), (setter, reader)])

    async def main():
        service = InMemorySessionService()
        runner = Runner(app_name="t", node=workflow, session_service=service)
        await service.create_session(app_name="t", user_id="u", session_id="s")
        async for _ in runner.run_async(
            user_id="u", session_id="s",
            new_message=types.Content(role="user", parts=[types.Part(text="{}")]),
        ):
            pass
        return await service.get_session(app_name="t", user_id="u", session_id="s")

    session = asyncio.run(main())

    assert seen["customer"] == "ACME", "state is bound to the parameter by name"
    assert seen["node_input"] == {"edge": "payload"}, "node_input is the edge payload"
    assert session.state["customer"] == "ACME", "and it is a flat top-level key"
