"""TRANSFORM in `fields` mode: declare the output shape, pick each input.

The other three modes are expressions — a text box, checked only when the run
happens. This mode is data: a list of output fields, each naming where its
value comes from. That buys two things a text box cannot give a no-code user:

* the sources can be *offered* rather than typed, because the compiler can work
  out what a node can read from the canvas (``app.compiler.inputs``);
* a source that nothing upstream produces, or a value that cannot become the
  declared type, is caught at compile time instead of at 3am.

The tests are in three groups: what is available at a node, what the validator
makes of a mapping, and what the generated code actually does with it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings

from app.compiler import build_graph_plan, run_compiler
from app.compiler.inputs import available_inputs, input_is_opaque
from app.compiler.semantic_validator import validate_semantics
from app.packaging.render import render_package
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)


def _node(node_id, node_type, config=None, title=""):
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": title or node_id, "description": ""},
        "config": config or {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1},
                     "on_error": "fail"},
    }


def _edge(source, target, handle="output", target_handle="input"):
    return {
        "id": f"e_{source}_{target}_{handle}",
        "source": source, "source_handle": handle,
        "target": target, "target_handle": target_handle, "condition": None,
    }


_START = {
    "input_mode": "json", "state_key": "wf", "output_variable": "order",
    "payload_schema": {"fields": [
        {"name": "sku", "type": "string", "description": "", "required": True},
        {"name": "qty", "type": "integer", "description": "", "required": True},
        {"name": "note", "type": "string", "description": "", "required": False},
    ]},
}


def _canvas(fields: list[dict], *, extra_nodes=None, extra_edges=None) -> dict:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", _START, title="Order"),
            _node("map", "TRANSFORM", {"mode": "fields", "output_fields": fields}),
            _node("end", "END", {"output_mapping": {}}),
            *(extra_nodes or []),
        ],
        "edges": [_edge("start", "map"), _edge("map", "end"), *(extra_edges or [])],
    }


def _payload(canvas: dict) -> CanvasPayload:
    return CanvasPayload.model_validate(canvas)


def _render(canvas: dict, name: str, tmp_path):
    errors, _warnings, ir = run_compiler(_payload(canvas), name)
    assert errors == [], errors
    destination = tmp_path / "pkg"
    render_package(build_graph_plan(ir, workflow_name=name), destination)
    return destination


def _run(destination, payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, "run_once.py", json.dumps(payload), "--json"],
        cwd=str(destination), capture_output=True, text=True, timeout=300,
        check=False,  # a failed run is a result here, not an error
    )
    assert result.returncode in (0, 1), f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


_MAPPING = [
    {"name": "code", "type": "string", "source": "data.sku", "required": True},
    {"name": "count", "type": "integer", "source": "data.qty", "required": True},
    {"name": "qty_as_text", "type": "string", "source": "data.qty"},
    {"name": "from_variable", "type": "string", "source": "vars.order.sku"},
    {"name": "channel", "type": "string", "default": "web"},
    {"name": "optional_note", "type": "string", "source": "data.note"},
]


# ── What a node can read ─────────────────────────────────────────────────────


def test_the_entry_payload_is_offered_field_by_field():
    """The picker's list, so a user chooses instead of typing a path."""
    inputs = available_inputs(_payload(_canvas(_MAPPING)), "map")
    payload = {i.path: i for i in inputs if i.source == "payload"}

    assert set(payload) == {"data.sku", "data.qty", "data.note"}
    assert payload["data.qty"].type == "integer"
    # The label names the node the value came from, not its canvas id.
    assert payload["data.sku"].label == "sku — from Order"


def test_a_named_result_is_offered_whole_and_field_by_field():
    inputs = available_inputs(_payload(_canvas(_MAPPING)), "map")
    variables = {i.path: i for i in inputs if i.source == "variable"}

    assert "vars.order" in variables, "the whole result"
    assert variables["vars.order"].type == "object"
    assert {"vars.order.sku", "vars.order.qty"} <= set(variables)
    assert variables["vars.order.qty"].type == "integer"


def test_only_ancestors_are_offered():
    """A sibling branch's output is not guaranteed to have happened.

    Offering it would offer something that is absent whenever the other branch
    is the one that ran.
    """
    canvas = _canvas(
        _MAPPING,
        extra_nodes=[
            _node("route", "CONDITION", {"branches": [
                {"name": "a", "expression": "True"},
                {"name": "b", "expression": "True"},
            ]}),
            _node("other", "TRANSFORM", {
                "mode": "fields", "output_variable": "sibling",
                "output_fields": [{"name": "never_seen", "type": "string",
                                   "default": "x"}],
            }),
        ],
    )
    canvas["edges"] = [
        _edge("start", "route"),
        _edge("route", "map", "a"), _edge("route", "other", "b"),
        _edge("map", "end"), _edge("other", "end"),
    ]

    paths = {i.path for i in available_inputs(_payload(canvas), "map")}
    assert "data.sku" in paths, "the entry payload is upstream of both branches"
    assert "data.never_seen" not in paths
    assert "vars.sibling" not in paths


def test_the_nearest_producer_of_a_name_wins():
    """`data.sku` re-made one node back is the one that will be on the payload."""
    canvas = _canvas(
        _MAPPING,
        extra_nodes=[_node("relabel", "TRANSFORM", {
            "mode": "fields",
            "output_fields": [{"name": "sku", "type": "integer", "default": 1}],
        }, title="Relabel")],
    )
    canvas["edges"] = [_edge("start", "relabel"), _edge("relabel", "map"),
                       _edge("map", "end")]

    sku = next(i for i in available_inputs(_payload(canvas), "map")
               if i.path == "data.sku")
    assert sku.from_node == "relabel"
    assert sku.type == "integer"


def test_an_expression_transform_is_opaque():
    """It declares nothing, so nothing is claimed on its behalf."""
    canvas = _canvas(
        _MAPPING,
        extra_nodes=[_node("guess", "TRANSFORM", {
            "mode": "jmespath", "expression": "{a: sku}",
        })],
    )
    canvas["edges"] = [_edge("start", "guess"), _edge("guess", "map"),
                       _edge("map", "end")]
    payload = _payload(canvas)

    assert input_is_opaque(payload, "map")
    # But what came before it still passes through and is still offered.
    assert "data.sku" in {i.path for i in available_inputs(payload, "map")}


def test_a_tool_edge_is_not_a_flow_edge():
    """An MCP tool is wired into a consumer; it is not upstream of it."""
    canvas = _canvas(
        _MAPPING,
        extra_nodes=[_node("tool", "MCP_TOOL", {"server_url": "http://x",
                                                "tool_name": "t"})],
        extra_edges=[_edge("tool", "map", "output", target_handle="tools")],
    )
    assert not input_is_opaque(_payload(canvas), "map")


# ── What the validator makes of a mapping ────────────────────────────────────


def test_a_good_mapping_passes_clean():
    errors, warnings_out = validate_semantics(_payload(_canvas(_MAPPING)))
    assert errors == []
    assert not any("map" in w and "source" in w for w in warnings_out), warnings_out


def test_no_fields_at_all_is_an_error():
    errors, _warnings = validate_semantics(_payload(_canvas([])))
    assert any("builds no fields" in e for e in errors), errors


def test_a_field_with_no_name_is_an_error():
    errors, _warnings = validate_semantics(
        _payload(_canvas([{"name": "  ", "source": "data.sku"}]))
    )
    assert any("no name" in e for e in errors), errors


def test_building_the_same_field_twice_is_an_error():
    errors, _warnings = validate_semantics(_payload(_canvas([
        {"name": "code", "source": "data.sku"},
        {"name": "code", "source": "data.qty"},
    ])))
    assert any("twice" in e for e in errors), errors


def test_a_field_with_neither_source_nor_default_is_an_error():
    errors, _warnings = validate_semantics(
        _payload(_canvas([{"name": "code", "type": "string"}]))
    )
    assert any("has no source" in e for e in errors), errors


def test_a_constant_needs_no_source():
    errors, _warnings = validate_semantics(
        _payload(_canvas([{"name": "channel", "type": "string", "default": "web"}]))
    )
    assert errors == []


def test_a_source_nothing_produces_is_an_error_that_lists_the_real_ones():
    """The whole point of the picker: this cannot happen by accident.

    It can still happen by editing a saved workflow, so the message says what
    *is* available rather than only what is not.
    """
    errors, _warnings = validate_semantics(
        _payload(_canvas([{"name": "code", "source": "data.skew"}]))
    )
    assert any("data.skew" in e and "data.sku" in e for e in errors), errors


def test_an_unknown_source_downstream_of_something_opaque_only_warns():
    """It might exist at runtime, and a false error blocks a valid workflow."""
    canvas = _canvas(
        [{"name": "code", "source": "data.whatever"}],
        extra_nodes=[_node("guess", "TRANSFORM",
                           {"mode": "jmespath", "expression": "@"})],
    )
    canvas["edges"] = [_edge("start", "guess"), _edge("guess", "map"),
                       _edge("map", "end")]

    errors, warnings_out = validate_semantics(_payload(canvas))
    assert errors == []
    assert any("data.whatever" in w for w in warnings_out), warnings_out


def test_a_type_that_does_not_match_the_source_warns():
    """A warning, not an error: the conversion usually works — `4` -> `"4"`."""
    errors, warnings_out = validate_semantics(_payload(_canvas([
        {"name": "code", "type": "boolean", "source": "data.sku"},
    ])))
    assert errors == []
    assert any("string" in w and "boolean" in w for w in warnings_out), warnings_out


def test_fields_mode_does_not_need_an_expression():
    """`expression` was required for every mode; declaring a shape replaced it."""
    errors, _warnings = validate_semantics(_payload(_canvas(_MAPPING)))
    assert not any("expression" in e for e in errors), errors


# ── What the generated code does ─────────────────────────────────────────────


def test_the_declared_shape_is_what_comes_out(tmp_path):
    destination = _render(_canvas(_MAPPING), "fields-build-0001", tmp_path)
    envelope = _run(destination, {"sku": "WIDGET", "qty": 4, "note": "rush"})

    assert envelope["state"] == "completed", envelope.get("error")
    result = envelope["result"]
    assert result["code"] == "WIDGET"
    assert result["count"] == 4
    assert result["qty_as_text"] == "4", "converted to the declared type"
    assert result["from_variable"] == "WIDGET", "read out of a named variable"
    assert result["channel"] == "web", "a constant"
    assert result["optional_note"] == "rush"


def test_an_absent_optional_field_is_skipped_not_nulled(tmp_path):
    """A key that is present and null is a different contract from an absent key.

    Downstream `if "optional_note" in data` is the ordinary way to ask, and a
    null would answer it wrongly.
    """
    destination = _render(_canvas(_MAPPING), "fields-absent-0001", tmp_path)
    result = _run(destination, {"sku": "W", "qty": 1})["result"]

    assert "optional_note" not in result
    assert result["code"] == "W"


def test_a_required_field_with_no_value_fails_the_run(tmp_path):
    """Loudly, naming the field and the source — not silently omitted."""
    canvas = _canvas([
        {"name": "code", "type": "string", "source": "data.missing", "required": True},
    ])
    # The source is unknown, so compiling it needs something opaque upstream.
    canvas["nodes"].append(_node("guess", "TRANSFORM",
                                 {"mode": "jmespath", "expression": "@"}))
    canvas["edges"] = [_edge("start", "guess"), _edge("guess", "map"),
                       _edge("map", "end")]

    destination = _render(canvas, "fields-required-0001", tmp_path)
    envelope = _run(destination, {"sku": "W", "qty": 1})

    assert envelope["state"] == "failed"
    assert "code" in envelope["error"] and "data.missing" in envelope["error"]


def test_a_zero_is_a_value_not_a_missing_one(tmp_path):
    """`found` is tracked separately from truthiness for exactly this."""
    destination = _render(_canvas([
        {"name": "count", "type": "integer", "source": "data.qty", "default": 99},
    ]), "fields-zero-0001", tmp_path)

    assert _run(destination, {"sku": "W", "qty": 0})["result"]["count"] == 0


def test_a_value_that_cannot_be_converted_passes_through_with_a_warning(tmp_path):
    """A declared type is a statement of intent, not a reason to kill a run."""
    destination = _render(_canvas([
        {"name": "count", "type": "integer", "source": "data.sku", "required": True},
    ]), "fields-badcast-0001", tmp_path)
    envelope = _run(destination, {"sku": "NOT-A-NUMBER", "qty": 1})

    assert envelope["state"] == "completed", envelope.get("error")
    assert envelope["result"]["count"] == "NOT-A-NUMBER"


def test_the_mapping_is_emitted_as_data_not_as_code(tmp_path):
    """No generated expression to escape, so a field name cannot inject Python."""
    destination = _render(_canvas(_MAPPING), "fields-source-0001", tmp_path)
    source = next((destination / "nodes").glob("*transform*.py")).read_text()

    assert "OUTPUT_FIELDS" in source
    assert "jmespath" not in source


def test_a_fields_transform_declares_its_shape_to_the_next_node(tmp_path):
    """Which is what makes mappers chainable: the second one has a picker too."""
    canvas = _canvas(_MAPPING)
    canvas["nodes"].append(_node("second", "TRANSFORM", {
        "mode": "fields",
        "output_fields": [{"name": "final", "type": "string", "source": "data.code"}],
    }))
    canvas["edges"] = [_edge("start", "map"), _edge("map", "second"),
                       _edge("second", "end")]

    paths = {i.path for i in available_inputs(_payload(canvas), "second")}
    assert {"data.code", "data.count", "data.channel"} <= paths

    errors, _warnings = validate_semantics(_payload(canvas))
    assert errors == []

    destination = _render(canvas, "fields-chain-0001", tmp_path)
    result = _run(destination, {"sku": "W", "qty": 2})["result"]
    assert result["final"] == "W"


# ── The API the picker calls ─────────────────────────────────────────────────


def _ask(canvas: dict, node_id: str) -> dict:
    """The endpoint itself. It takes no database, so it is called directly."""
    import asyncio

    from app.api.workflows import NodeInputsRequest, node_inputs

    return asyncio.run(
        node_inputs(NodeInputsRequest(canvas=canvas, node_id=node_id))
    )


def test_the_picker_endpoint_answers_from_an_unsaved_canvas():
    """The picker runs while editing, so it posts the canvas rather than an id."""
    body = _ask(_canvas(_MAPPING), "map")

    assert body["opaque"] is False
    assert {i["path"] for i in body["inputs"]} >= {"data.sku", "vars.order.qty"}
    assert all({"path", "type", "source", "label"} <= set(i) for i in body["inputs"])


def test_the_picker_endpoint_on_an_unknown_node_is_empty_not_an_error():
    """A node the user just dropped is not yet on the canvas being posted."""
    assert _ask(_canvas(_MAPPING), "nope")["inputs"] == []


def test_a_half_drawn_canvas_is_a_422_not_a_500():
    """Editing means unfinished: the picker must not blow up on the way there."""
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        _ask({"schema_version": CURRENT_SCHEMA_VERSION, "nodes": "not a list"}, "map")
    assert caught.value.status_code == 422
