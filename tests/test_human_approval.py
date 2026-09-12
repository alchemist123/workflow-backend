"""HUMAN_APPROVAL: parking a run on a person, over ADK interrupts and A2A.

The node used to be unpackageable — a placeholder that packaging refused. It
needed no machinery of its own in the end, because ADK graphs interrupt
natively: a node that returns a `RequestInput` becomes an interrupt event
(`BaseNode.run`: "RequestInput -> convert to interrupt Event"), and `to_a2a`
reports the task as A2A `input-required`.

Everything here that runs a graph runs the *generated package*, over its real
A2A surface, because the interrupt and the resume are protocol behaviour rather
than anything the platform implements.
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.compiler.canvas_migrations import migrate_canvas
from app.compiler.semantic_validator import validate_semantics
from app.nodes.registry import BRANCH_NODE_TYPES, NODE_REGISTRY
from app.nodes.tasks.human_approval import (
    required_extra_fields,
    response_json_schema,
)
from app.packaging.render import render_package
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload

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


def _edge(source, target, handle="output") -> dict:
    return {
        "id": f"e_{source}_{target}_{handle}",
        "source": source,
        "source_handle": handle,
        "target": target,
        "target_handle": "input",
        "condition": None,
    }


_GATE = {
    "prompt": "Approve this payment?",
    "assignees": ["finance@example.com"],
    "collect_fields": {"fields": [
        {"name": "cost_centre", "type": "string",
         "description": "Which cost centre", "required": True},
    ]},
}


def _canvas(gate_config: dict | None = None, *, drop_handle: str | None = None) -> dict:
    edges = [
        _edge("start", "gate"),
        _edge("gate", "ok", "approved"),
        _edge("gate", "no", "rejected"),
        _edge("ok", "end"),
        _edge("no", "end"),
    ]
    if drop_handle:
        edges = [e for e in edges if e["source_handle"] != drop_handle]
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", {
                "input_mode": "json",
                "state_key": "wf",
                "payload_schema": {"fields": [
                    {"name": "amount", "type": "number", "description": "", "required": True},
                ]},
            }),
            _node("gate", "HUMAN_APPROVAL", gate_config if gate_config is not None else dict(_GATE)),
            _node("ok", "TRANSFORM", {"mode": "python",
                                      "expression": "result = {'outcome': 'paid'}"}),
            _node("no", "TRANSFORM", {"mode": "python",
                                      "expression": "result = {'outcome': 'declined'}"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": edges,
    }


def _render(canvas: dict, name: str, tmp_path):
    errors, _warnings, ir = run_compiler(CanvasPayload.model_validate(canvas), name)
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name=name)
    destination = tmp_path / "pkg"
    render_package(plan, destination)
    return destination


def _run(destination, payload: dict, answer: str | None = None) -> dict:
    """Drive the generated package over its own A2A surface."""
    command = [sys.executable, "run_once.py", json.dumps(payload), "--json"]
    if answer is not None:
        command += ["--answer", answer]
    result = subprocess.run(
        command, cwd=str(destination), capture_output=True, text=True, timeout=300
    )
    assert result.returncode in (0, 1), f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── The node is packageable at all ───────────────────────────────────────────


def test_human_approval_is_no_longer_a_placeholder(tmp_path):
    """It used to be in `_UNSUPPORTED_TYPES`, so packaging refused outright."""
    destination = _render(_canvas(), "ha-supported-0001", tmp_path)
    source = next((destination / "nodes").glob("*human_approval*.py")).read_text()

    assert "RequestInput" in source
    assert "does nothing" not in source, "still the passthrough placeholder"


def test_the_two_details_that_make_it_work_are_in_the_generated_module(tmp_path):
    """Both are easy to omit and each fails silently in its own way.

    Without `rerun_on_resume` the node is marked complete on resume and never
    reads the answer. With a generated interrupt id the re-run cannot look the
    answer up, so the task sits at `input-required` however often it is
    answered — observed before the id was derived from `ctx.node_path`.
    """
    destination = _render(_canvas(), "ha-details-0001", tmp_path)
    source = next((destination / "nodes").glob("*human_approval*.py")).read_text()

    assert "rerun_on_resume=True" in source
    assert "ctx.node_path" in source
    assert "ctx.resume_inputs" in source


def test_the_decision_is_a_route_so_only_one_branch_runs(tmp_path):
    """HUMAN_APPROVAL was missing from the hardcoded branch-node list, so both
    its edges were plain flow: both branches ran and the loser's output
    overwrote the winner's. The set is derived from the declarations now."""
    assert "HUMAN_APPROVAL" in BRANCH_NODE_TYPES
    assert NODE_REGISTRY["HUMAN_APPROVAL"].uses_named_routes

    errors, _warnings, ir = run_compiler(
        CanvasPayload.model_validate(_canvas()), "ha-routes-0001"
    )
    assert errors == []
    plan = build_graph_plan(ir, workflow_name="ha-routes")
    routes = {
        e.route for e in plan.edges if e.from_node.endswith("human_approval_1")
        or "human_approval" in e.from_node
    }
    assert routes == {"approved", "rejected"}


# ── The interrupt / resume cycle, end to end ─────────────────────────────────


def test_an_unanswered_run_parks_at_input_required(tmp_path):
    destination = _render(_canvas(), "ha-park-0001", tmp_path)
    envelope = _run(destination, {"amount": 5000})

    assert envelope["state"] == "input-required"
    pending = envelope["input_required"]
    assert pending["prompt"] == "Approve this payment?"
    assert pending["interrupt_id"]
    assert pending["payload"]["assignees"] == ["finance@example.com"]
    # It is waiting, not broken.
    assert envelope["error"] is None


def test_the_paused_task_advertises_the_answer_schema(tmp_path):
    """So a client can build a form from the task instead of guessing."""
    destination = _render(_canvas(), "ha-schema-0001", tmp_path)
    schema = _run(destination, {"amount": 5000})["input_required"]["response_schema"]

    assert set(schema["properties"]) == {"approved", "comment", "cost_centre"}
    assert schema["required"] == ["approved"]


def test_approving_takes_only_the_approved_branch(tmp_path):
    destination = _render(_canvas(), "ha-approve-0001", tmp_path)
    envelope = _run(
        destination,
        {"amount": 5000},
        json.dumps({"approved": True, "comment": "ok", "cost_centre": "ENG-1"}),
    )

    assert envelope["state"] == "completed"
    assert envelope["result"]["outcome"] == "paid"
    assert envelope["result"]["cost_centre"] == "ENG-1"
    assert envelope["result"]["approved"] is True


def test_rejecting_takes_only_the_rejected_branch(tmp_path):
    destination = _render(_canvas(), "ha-reject-0001", tmp_path)
    envelope = _run(
        destination,
        {"amount": 5000},
        json.dumps({"approved": False, "comment": "no"}),
    )

    assert envelope["state"] == "completed"
    assert envelope["result"]["outcome"] == "declined"
    assert envelope["result"]["approved"] is False


# ── Required collected fields ────────────────────────────────────────────────


def test_a_required_field_is_not_demanded_by_the_advertised_schema():
    """ADK validates the answer against the schema before the node sees it.

    Marking a collected field required there would make *rejecting* impossible
    without inventing a value for a field that does not apply to a rejection —
    it failed with "cost_centre Field required" until this was split.
    """
    schema = response_json_schema(_GATE)
    assert schema["required"] == ["approved"]
    assert "Required when approving" in schema["properties"]["cost_centre"]["description"]
    assert required_extra_fields(_GATE) == ["cost_centre"]


def test_approving_without_a_required_field_asks_again(tmp_path):
    destination = _render(_canvas(), "ha-missing-0001", tmp_path)
    envelope = _run(destination, {"amount": 5000}, json.dumps({"approved": True}))

    assert envelope["state"] == "input-required"
    assert "cost_centre" in envelope["input_required"]["prompt"]


def test_rejecting_without_that_field_is_fine(tmp_path):
    """The other half of the same rule."""
    destination = _render(_canvas(), "ha-reject2-0001", tmp_path)
    envelope = _run(destination, {"amount": 5000}, json.dumps({"approved": False}))

    assert envelope["state"] == "completed"
    assert envelope["result"]["outcome"] == "declined"


# ── Resuming a parked run, rather than replaying it ──────────────────────────


def _resume(destination, task_id, context_id, interrupt_id, response: dict) -> dict:
    """A *separate* invocation answering a task an earlier one parked."""
    command = [
        sys.executable, "run_once.py",
        "--resume", task_id,
        "--interrupt", interrupt_id,
        "--answer", json.dumps(response),
        "--json",
    ]
    if context_id:
        command += ["--context", context_id]
    result = subprocess.run(
        command, cwd=str(destination), capture_output=True, text=True, timeout=300
    )
    assert result.returncode in (0, 1), f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_a_later_process_can_answer_a_parked_task(tmp_path):
    """The whole point: the task has to outlive the process that made it.

    `run_once.py` keeps the A2A task *and* the ADK session in one SQLite file,
    because persisting only the task resumes into an empty workflow.
    """
    destination = _render(_canvas(), "ha-resume-0001", tmp_path)
    parked = _run(destination, {"amount": 5000})
    assert parked["state"] == "input-required"

    resumed = _resume(
        destination,
        parked["task_id"],
        parked.get("context_id"),
        parked["input_required"]["interrupt_id"],
        {"approved": True, "comment": "ok", "cost_centre": "ENG-1"},
    )

    assert resumed["state"] == "completed"
    assert resumed["resumed"] is True
    assert resumed["result"]["outcome"] == "paid"


def test_resuming_does_not_replay_the_steps_before_the_gate(tmp_path):
    """A resume, not a re-run: an earlier side effect must not happen twice."""
    destination = _render(_canvas(), "ha-noreplay-0001", tmp_path)
    parked = _run(destination, {"amount": 5000})

    resumed = _resume(
        destination,
        parked["task_id"],
        parked.get("context_id"),
        parked["input_required"]["interrupt_id"],
        {"approved": True, "cost_centre": "ENG-1"},
    )

    nodes = [step["node"] for step in resumed["trace"]]
    assert not any(n.startswith("a2a_start") for n in nodes), nodes
    # It picks up at the approval node and carries on from there.
    assert any("human_approval" in n for n in nodes), nodes


def test_the_parked_state_is_kept_beside_the_package(tmp_path):
    destination = _render(_canvas(), "ha-state-0001", tmp_path)
    _run(destination, {"amount": 5000})

    assert (destination / ".runs" / "tasks.db").is_file()
    # Scratch, not source.
    assert ".runs/" in (destination / ".gitignore").read_text()


def test_resuming_an_unknown_task_is_reported_not_raised(tmp_path):
    destination = _render(_canvas(), "ha-unknown-0001", tmp_path)
    envelope = _resume(destination, "no-such-task", None, "no-such-interrupt",
                       {"approved": True})

    assert envelope["ok"] is False
    assert envelope["error"]


# ── Validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["approved", "rejected"])
def test_both_decisions_must_be_wired(missing):
    canvas = CanvasPayload.model_validate(_canvas(drop_handle=missing))
    errors, _warnings = validate_semantics(canvas)
    assert any(f"'{missing}' output" in e for e in errors), errors


def test_a_gate_with_no_prompt_warns():
    canvas = CanvasPayload.model_validate(_canvas({"assignees": []}))
    _errors, warnings_out = validate_semantics(canvas)
    assert any("no prompt" in w for w in warnings_out), warnings_out


# ── Saved canvases ───────────────────────────────────────────────────────────


def test_the_timeout_settings_are_migrated_away():
    """They promised an approval could expire. Nothing implemented them, and
    the package cannot: it parks at `input-required` with nothing waiting on a
    clock, so expiry needs a scheduler outside the package."""
    canvas = {
        "schema_version": 5,
        "nodes": [_node("gate", "HUMAN_APPROVAL", {
            "prompt": "ok?", "assignees": ["a@b.c"],
            "timeout_seconds": 86400, "on_timeout": "reject",
        })],
        "edges": [],
    }
    migrated, notes = migrate_canvas(canvas)
    config = migrated["nodes"][0]["config"]

    assert "timeout_seconds" not in config
    assert "on_timeout" not in config
    assert config["prompt"] == "ok?"
    assert config["assignees"] == ["a@b.c"]
    assert any("cannot expire" in n for n in notes)


def test_that_migration_is_idempotent():
    canvas = {
        "schema_version": 5,
        "nodes": [_node("gate", "HUMAN_APPROVAL", {"on_timeout": "approve"})],
        "edges": [],
    }
    once, _ = migrate_canvas(canvas)
    twice, notes = migrate_canvas(once)
    assert twice == once
    assert notes == []


# ── HUMAN_INPUT: the other thing a person is asked for ───────────────────────


def _input_canvas(collect: list[dict] | None = None, prompt: str = "Shipping details?") -> dict:
    fields = collect if collect is not None else [
        {"name": "address", "type": "text", "description": "Where to", "required": True},
        {"name": "courier", "type": "string", "description": "Who ships", "required": False},
    ]
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", {
                "input_mode": "json", "state_key": "wf",
                "payload_schema": {"fields": [
                    {"name": "order", "type": "string", "description": "", "required": True},
                ]},
            }),
            _node("ask", "HUMAN_INPUT", {
                "prompt": prompt,
                "assignees": ["ops@example.com"],
                "collect_fields": {"fields": fields},
            }),
            _node("ship", "TRANSFORM", {"mode": "python",
                                        "expression": "result = {'shipped': True}"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [_edge("start", "ask"), _edge("ask", "ship"), _edge("ship", "end")],
    }


def test_human_input_has_one_way_out():
    """It collects values rather than making a decision, so it does not branch."""
    definition = NODE_REGISTRY["HUMAN_INPUT"]
    assert definition.output_handles == ["output"]
    assert not definition.uses_named_routes
    assert "HUMAN_INPUT" not in BRANCH_NODE_TYPES


def test_human_input_parks_and_advertises_what_it_wants(tmp_path):
    destination = _render(_input_canvas(), "hi-park-0001", tmp_path)
    envelope = _run(destination, {"order": "A-1"})

    assert envelope["state"] == "input-required"
    pending = envelope["input_required"]
    assert pending["prompt"] == "Shipping details?"
    assert set(pending["response_schema"]["properties"]) == {"address", "courier"}
    # Required names travel on the payload, not in the schema -- see below.
    assert pending["payload"]["required_fields"] == ["address"]


def test_the_answer_is_merged_into_the_payload(tmp_path):
    destination = _render(_input_canvas(), "hi-answer-0001", tmp_path)
    envelope = _run(
        destination, {"order": "A-1"},
        json.dumps({"address": "12 Mill Lane", "courier": "DHL"}),
    )

    assert envelope["state"] == "completed"
    assert envelope["result"]["address"] == "12 Mill Lane"
    assert envelope["result"]["shipped"] is True


def test_an_incomplete_answer_asks_again_instead_of_failing_the_run(tmp_path):
    """The reason required fields are not in the advertised schema.

    ADK validates that schema before the node runs again, and a validation
    failure fails the whole run — losing a workflow that had been waiting on a
    person. Marking `address` required there produced exactly that: a failed
    run reading "address Field required".
    """
    destination = _render(_input_canvas(), "hi-missing-0001", tmp_path)
    envelope = _run(destination, {"order": "A-1"}, json.dumps({"courier": "DHL"}))

    assert envelope["state"] == "input-required"
    assert envelope["error"] is None
    assert "address" in envelope["input_required"]["prompt"]


def test_the_advertised_schema_marks_nothing_required():
    from app.nodes.tasks.human_input import request_json_schema, required_fields

    config = (_input_canvas()["nodes"][1])["config"]
    schema = request_json_schema(config)

    assert "required" not in schema
    assert "Required." in schema["properties"]["address"]["description"]
    assert required_fields(config) == ["address"]


def test_an_input_node_that_collects_nothing_is_rejected():
    canvas = CanvasPayload.model_validate(_input_canvas(collect=[]))
    errors, _warnings = validate_semantics(canvas)
    assert any("collects no fields" in e for e in errors), errors


def test_both_pause_types_share_one_template():
    """The fragile parts are identical, so they are written once."""
    from app.packaging.render import NODE_TEMPLATES

    assert NODE_TEMPLATES["HUMAN_APPROVAL"] == NODE_TEMPLATES["HUMAN_INPUT"]


def test_a_workflow_can_pause_twice(tmp_path):
    """Approve, then supply values — two interrupts in one run."""
    canvas = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", {
                "input_mode": "json", "state_key": "wf",
                "payload_schema": {"fields": [
                    {"name": "amount", "type": "number", "description": "", "required": True},
                ]},
            }),
            _node("gate", "HUMAN_APPROVAL", {"prompt": "Approve?", "assignees": []}),
            _node("ask", "HUMAN_INPUT", {
                "prompt": "Account?",
                "collect_fields": {"fields": [
                    {"name": "account", "type": "string", "description": "", "required": True},
                ]},
            }),
            _node("ok", "TRANSFORM", {"mode": "python",
                                      "expression": "result = {'outcome': 'paid'}"}),
            _node("no", "TRANSFORM", {"mode": "python",
                                      "expression": "result = {'outcome': 'declined'}"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [
            _edge("start", "gate"),
            _edge("gate", "ask", "approved"),
            _edge("ask", "ok"),
            _edge("gate", "no", "rejected"),
            _edge("ok", "end"),
            _edge("no", "end"),
        ],
    }
    destination = _render(canvas, "hi-twice-0001", tmp_path)

    first = _run(destination, {"amount": 5000})
    assert first["state"] == "input-required"

    second = _run(destination, {"amount": 5000}, json.dumps({"approved": True}))
    assert second["state"] == "input-required", "should stop again for the input"
    assert "Account?" in second["input_required"]["prompt"]

    third = _resume(
        destination,
        second["task_id"],
        second.get("context_id"),
        second["input_required"]["interrupt_id"],
        {"account": "GBP-1"},
    )
    assert third["state"] == "completed"
    assert third["result"]["outcome"] == "paid"
    assert third["result"]["account"] == "GBP-1"


# ── The ADK contract this rests on ───────────────────────────────────────────


def test_adk_turns_a_returned_request_input_into_an_interrupt():
    """Pinned because the whole node is one line of ADK behaviour."""
    from google.adk.agents.context import Context  # noqa: F401
    from google.adk.events.request_input import RequestInput
    from google.adk.workflow import START, Workflow, node

    @node(name="asker", rerun_on_resume=True)
    async def asker(ctx, node_input=None):
        return RequestInput(interrupt_id="fixed", message="ok?")

    workflow = Workflow(
        name="pinned", description="one interrupting node", edges=[(START, asker)]
    )
    assert workflow is not None

    from google.adk.workflow.utils._workflow_hitl_utils import (
        REQUEST_INPUT_FUNCTION_CALL_NAME,
        create_request_input_event,
    )

    event = create_request_input_event(RequestInput(interrupt_id="fixed", message="ok?"))
    assert set(event.long_running_tool_ids) == {"fixed"}
    call = event.content.parts[0].function_call
    assert call.name == REQUEST_INPUT_FUNCTION_CALL_NAME
    assert call.args["message"] == "ok?"
