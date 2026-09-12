"""LOOP: the back-edge guard, and the ways it used to be quietly wrong.

ADK graph workflows have no loop construct. A loop is a cycle in the graph, and
ADK's only structural rule is that the cycle must contain at least one *routed*
edge — `_detect_unconditional_cycles` in
`google/adk/workflow/utils/_graph_validation.py`: "Cycles must include at least
one conditional (routed) edge to avoid infinite loops". The LOOP node supplies
that edge and owns the iteration counter, because on re-entry `node_input` is
whatever the body produced rather than what the loop last saw.

Every test here that runs a graph runs the *generated package*, because the
iteration logic lives in the rendered module rather than in the platform.
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.compiler.semantic_validator import validate_semantics
from app.packaging.render import RenderError, render_package
from app.schemas.canvas import CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)


def _node(node_id: str, node_type: str, config: dict | None = None) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": node_id, "description": ""},
        "config": config or {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1}, "on_error": "fail"},
    }


def _edge(source, target, handle="output", target_handle="input") -> dict:
    return {
        "id": f"e_{source}_{target}_{handle}",
        "source": source,
        "source_handle": handle,
        "target": target,
        "target_handle": target_handle,
        "condition": None,
    }


_FIELDS = [
    {"name": "items", "type": "array", "description": "", "required": False},
    {"name": "seed", "type": "integer", "description": "", "required": False},
]


def _loop_canvas(loop_config: dict, body_expression: str) -> dict:
    """start -> loop -(loop_body)-> body -> loop ; loop -(done)-> end."""
    return {
        "schema_version": 4,
        "nodes": [
            _node("start", "A2A_START", {
                "input_mode": "json",
                "state_key": "wf",
                "payload_schema": {"fields": _FIELDS},
            }),
            _node("loop", "LOOP", loop_config),
            _node("body", "TRANSFORM", {"mode": "python", "expression": body_expression}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [
            _edge("start", "loop"),
            _edge("loop", "body", "loop_body"),
            _edge("body", "loop"),
            _edge("loop", "end", "done"),
        ],
    }


def _render(canvas: dict, name: str, tmp_path):
    payload = CanvasPayload.model_validate(canvas)
    errors, _warnings, ir = run_compiler(payload, name)
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name=name)
    destination = tmp_path / "pkg"
    render_package(plan, destination)
    return destination


def _run(destination, payload: dict) -> dict:
    """Run the generated graph once and return the terminal node's output."""
    script = (
        "import asyncio, json, sys\n"
        f"sys.path.insert(0, {str(destination)!r})\n"
        "from google.adk import Runner\n"
        "from google.adk.sessions import InMemorySessionService\n"
        "from google.genai import types\n"
        "from agent import root_agent\n"
        "async def main():\n"
        "    service = InMemorySessionService()\n"
        "    runner = Runner(app_name='t', node=root_agent, session_service=service)\n"
        "    await service.create_session(app_name='t', user_id='u', session_id='s')\n"
        f"    message = types.Content(role='user', parts=[types.Part(text=json.dumps({payload!r}))])\n"
        "    outputs = []\n"
        "    async for event in runner.run_async(user_id='u', session_id='s', new_message=message):\n"
        "        if event.output is not None:\n"
        "            outputs.append(event.output)\n"
        "    print(json.dumps(outputs[-1] if outputs else None, default=str))\n"
        "asyncio.run(main())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(destination),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── The list is pinned on entry ──────────────────────────────────────────────


def test_a_body_that_mutates_the_list_cannot_change_what_the_loop_walks(tmp_path):
    """The bug this replaced: the list was re-read from the *body's* output.

    Iterating [1, 2, 3] with a body that drops the head visited 1 then 3 and
    stopped after two passes, because each pass re-resolved `items_path` against
    a payload the body had rewritten.
    """
    canvas = _loop_canvas(
        {"mode": "for_each", "items_path": "items", "max_iterations": 50},
        "result = {'items': (data.get('items') or [])[1:], 'seen': data.get('current_item')}",
    )
    output = _run(_render(canvas, "loop-pin-0001", tmp_path), {"items": [1, 2, 3]})

    assert output["iterations"] == 3
    assert [r["seen"] for r in output["results"]] == [1, 2, 3]


def test_nested_loops_do_not_share_iteration_state(tmp_path):
    """Pinning only works if the pinned list is per node.

    With one shared state key an inner loop overwrites the outer loop's list, and
    the outer loop resumes against the inner one's — verified to report a single
    iteration over nothing.
    """
    canvas = {
        "schema_version": 4,
        "nodes": [
            _node("start", "A2A_START", {
                "input_mode": "json",
                "state_key": "wf",
                "payload_schema": {"fields": [
                    {"name": "outer", "type": "array", "description": "", "required": True},
                    {"name": "inner", "type": "array", "description": "", "required": True},
                ]},
            }),
            _node("oloop", "LOOP", {"mode": "for_each", "items_path": "outer", "max_iterations": 20}),
            _node("iloop", "LOOP", {"mode": "for_each", "items_path": "inner", "max_iterations": 20}),
            _node("ibody", "TRANSFORM", {"mode": "python", "expression": "result = {'n': 1}"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [
            _edge("start", "oloop"),
            _edge("oloop", "iloop", "loop_body"),
            _edge("iloop", "ibody", "loop_body"),
            _edge("ibody", "iloop"),
            _edge("iloop", "oloop", "done"),
            _edge("oloop", "end", "done"),
        ],
    }
    output = _run(_render(canvas, "loop-nested-0001", tmp_path),
                  {"outer": ["a", "b"], "inner": [1, 2, 3]})

    # The outer loop is the terminal one, so its counts are what surface.
    assert output["iterations"] == 2
    assert output["total_items"] == 2


def test_state_is_namespaced_per_node(tmp_path):
    """Cheap structural guard for the above, so a regression is obvious."""
    canvas = _loop_canvas(
        {"mode": "for_each", "items_path": "items", "max_iterations": 5},
        "result = {'n': 1}",
    )
    destination = _render(canvas, "loop-ns-0001", tmp_path)
    source = next((destination / "nodes").glob("*loop*.py")).read_text()

    assert '_STATE_PREFIX = f"_loop_{NODE_ID}"' in source
    for key in ("_COUNTER_KEY", "_RESULTS_KEY", "_ITEMS_KEY"):
        assert f'{key} = f"{{_STATE_PREFIX}}' in source, key
    # The old global key would collide between two loops.
    assert '"_loop_items"' not in source


# ── Truncation is reported, not silent ───────────────────────────────────────


def test_for_each_reports_truncation_when_it_hits_the_cap(tmp_path):
    """A partial result that looks complete is worse than a slow loop."""
    canvas = _loop_canvas(
        {"mode": "for_each", "items_path": "items", "max_iterations": 2},
        "result = {'seen': data.get('current_item')}",
    )
    output = _run(_render(canvas, "loop-cap-0001", tmp_path), {"items": [1, 2, 3, 4, 5]})

    assert output["iterations"] == 2
    assert output["total_items"] == 5
    assert output["truncated"] is True


def test_for_each_does_not_claim_truncation_when_it_finished(tmp_path):
    canvas = _loop_canvas(
        {"mode": "for_each", "items_path": "items", "max_iterations": 50},
        "result = {'seen': data.get('current_item')}",
    )
    output = _run(_render(canvas, "loop-full-0001", tmp_path), {"items": [1, 2, 3]})

    assert output["iterations"] == 3
    assert output["total_items"] == 3
    assert "truncated" not in output


def test_an_empty_list_is_zero_iterations_not_an_error(tmp_path):
    canvas = _loop_canvas(
        {"mode": "for_each", "items_path": "items", "max_iterations": 10},
        "result = {'seen': data.get('current_item')}",
    )
    output = _run(_render(canvas, "loop-empty-0001", tmp_path), {"items": []})

    assert output["iterations"] == 0
    assert output["total_items"] == 0
    assert "truncated" not in output


# ── while mode ───────────────────────────────────────────────────────────────


def test_while_exits_when_the_condition_becomes_true(tmp_path):
    """`exit_condition` means *exit* when true.

    The node used to describe this as "repeats until condition is false", which
    is the opposite; the text now matches the behaviour.
    """
    canvas = _loop_canvas(
        {"mode": "while", "exit_condition": "i >= 2", "max_iterations": 10},
        "result = {'n': (data.get('current_index') or 0)}",
    )
    output = _run(_render(canvas, "loop-while-0001", tmp_path), {"seed": 0})

    assert output["iterations"] == 2
    assert "truncated" not in output


def test_while_flags_truncation_when_the_condition_never_holds(tmp_path):
    canvas = _loop_canvas(
        {"mode": "while", "exit_condition": "False", "max_iterations": 3},
        "result = {'n': 1}",
    )
    output = _run(_render(canvas, "loop-whilecap-0001", tmp_path), {"seed": 0})

    assert output["iterations"] == 3
    assert output["truncated"] is True


def test_a_condition_that_is_already_true_runs_the_body_zero_times(tmp_path):
    canvas = _loop_canvas(
        {"mode": "while", "exit_condition": "True", "max_iterations": 10},
        "result = {'n': 1}",
    )
    output = _run(_render(canvas, "loop-while0-0001", tmp_path), {"seed": 0})

    assert output["iterations"] == 0
    assert output["results"] == []


# ── Misconfiguration is caught on the canvas ─────────────────────────────────


def test_for_each_without_an_items_path_is_rejected():
    """It used to compile, package, run, and iterate zero times."""
    canvas = CanvasPayload.model_validate(
        _loop_canvas({"mode": "for_each", "max_iterations": 10}, "result = {}")
    )
    errors, _warnings = validate_semantics(canvas)
    assert any("no items path" in e for e in errors), errors


def test_while_without_an_exit_condition_is_rejected():
    canvas = CanvasPayload.model_validate(
        _loop_canvas({"mode": "while", "max_iterations": 10}, "result = {}")
    )
    errors, _warnings = validate_semantics(canvas)
    assert any("no exit condition" in e for e in errors), errors


@pytest.mark.parametrize("missing", ["loop_body", "done"])
def test_a_loop_missing_either_output_is_rejected(missing):
    """Without both handles wired it is not a loop, whatever the canvas says."""
    canvas = _loop_canvas(
        {"mode": "for_each", "items_path": "items", "max_iterations": 10},
        "result = {}",
    )
    canvas["edges"] = [e for e in canvas["edges"] if e["source_handle"] != missing]
    errors, _warnings = validate_semantics(CanvasPayload.model_validate(canvas))
    assert any(f"'{missing}' output" in e for e in errors), errors


def test_render_refuses_a_for_each_with_no_items_path(tmp_path):
    """Backstop for a canvas that reached packaging some other way."""
    payload = CanvasPayload.model_validate(
        _loop_canvas({"mode": "for_each", "items_path": "items", "max_iterations": 10},
                     "result = {}")
    )
    _errors, _warnings, ir = run_compiler(payload, "loop-backstop-0001")
    plan = build_graph_plan(ir, workflow_name="loop-backstop")

    loop = next(n for n in plan.nodes if n.node_type == "LOOP")
    loop.config = {**(loop.config or {}), "items_path": ""}

    with pytest.raises(RenderError, match="items path"):
        render_package(plan, tmp_path / "pkg")


# ── Saved canvases ───────────────────────────────────────────────────────────


def test_a_loop_saved_by_the_old_panel_migrates_to_the_real_keys():
    """The config panel wrote keys the node never read.

    `items_expression` held exactly what `items_path` wants, so it carries over;
    without the migration every existing loop would now fail validation.
    """
    from app.compiler.canvas_migrations import migrate_canvas
    from app.schemas.canvas import CURRENT_SCHEMA_VERSION

    canvas = {
        "schema_version": 4,
        "nodes": [_node("l", "LOOP", {
            "items_expression": "data.items",
            "item_variable": "row",
            "max_iterations": 50,
        })],
        "edges": [],
    }
    migrated, notes = migrate_canvas(canvas)
    config = migrated["nodes"][0]["config"]

    assert config["items_path"] == "data.items"
    assert config["mode"] == "for_each"
    assert config["max_iterations"] == 50
    assert "items_expression" not in config
    assert "item_variable" not in config
    assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
    assert any("items_path" in n for n in notes)
    assert any("item_variable" in n for n in notes)


def test_the_loop_migration_is_idempotent():
    from app.compiler.canvas_migrations import migrate_canvas

    canvas = {
        "schema_version": 4,
        "nodes": [_node("l", "LOOP", {"items_expression": "items"})],
        "edges": [],
    }
    once, _ = migrate_canvas(canvas)
    twice, notes = migrate_canvas(once)
    assert twice == once
    assert notes == []


def test_a_migrated_loop_passes_validation():
    """End of the chain: an old canvas opens, validates and can be packaged."""
    from app.compiler.canvas_migrations import migrate_canvas

    canvas = _loop_canvas({"items_expression": "items", "max_iterations": 10},
                          "result = {'n': 1}")
    canvas["schema_version"] = 4
    migrated, _notes = migrate_canvas(canvas)

    errors, _warnings = validate_semantics(CanvasPayload.model_validate(migrated))
    assert errors == [], errors


# ── The ADK contract the loop relies on ──────────────────────────────────────


def test_adk_requires_a_routed_edge_in_every_cycle():
    """Why LOOP owns the routing rather than being a plain pass-through node.

    Pinned because it is the reason the node has two output handles at all.
    """
    from google.adk.workflow import Workflow, node

    @node(name="a")
    async def a(ctx, node_input=None):
        return None

    @node(name="b")
    async def b(ctx, node_input=None):
        return None

    from google.adk.workflow import START

    with pytest.raises(ValueError, match="[Uu]nconditional cycle"):
        Workflow(
            name="cyclic",
            description="two nodes in an unrouted cycle",
            edges=[(START, a), (a, b), (b, a)],
        )
