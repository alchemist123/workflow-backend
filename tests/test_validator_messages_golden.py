"""What the validator says, pinned word for word.

The validator's messages are about to become `Finding` records so the canvas can
mark the node each one concerns. That is a change to ~60 call sites, and the
thing it must not do is quietly reword anything: eleven test files assert on
substrings of these strings, and the UI shows them verbatim.

So this captures the exact output first, against the code as it stands, and the
refactor is done until this passes again. Anything reworded shows up as a
reviewed diff rather than a silent drift. Same pattern as
`tests/test_graph_plan_golden.py`:

    UPDATE_GOLDEN=1 pytest tests/test_validator_messages_golden.py

The canvases below are deliberately broken, each in a different way, and
between them they reach most of the rules. They are not meant to be realistic.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import pytest

from app.compiler.semantic_validator import validate_semantics
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)

GOLDEN = Path(__file__).resolve().parent / "golden" / "validator_messages.json"


def _node(node_id, node_type, config=None):
    return {
        "id": node_id, "type": node_type, "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": node_id, "description": ""},
        "config": config or {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1},
                     "on_error": "fail"},
    }


def _edge(source, target, source_handle="output", target_handle="input"):
    return {
        "id": f"e_{source}_{target}_{target_handle}_{source_handle}",
        "source": source, "source_handle": source_handle,
        "target": target, "target_handle": target_handle, "condition": None,
    }


def _canvas(nodes, edges):
    return {"schema_version": CURRENT_SCHEMA_VERSION, "nodes": nodes, "edges": edges}


_START = {
    "input_mode": "json", "state_key": "wf",
    "payload_schema": {"fields": [
        {"name": "sku", "type": "string", "description": "", "required": True},
    ]},
}

# Each entry is one broken canvas. The name is what appears in the golden file.
CASES: dict[str, dict] = {
    # Nothing wrong: the golden must record silence as carefully as noise.
    "healthy": _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("shape", "TRANSFORM", {"mode": "fields", "output_fields": [
                {"name": "code", "type": "string", "source": "data.sku"},
            ]}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        [_edge("start", "shape"), _edge("shape", "end")],
    ),
    "no_entry_no_terminal": _canvas(
        [_node("lonely", "TRANSFORM", {"mode": "jmespath", "expression": "@"})], []
    ),
    "two_entries_two_terminals": _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("start2", "A2A_START", _START),
            _node("end", "END", {"output_mapping": {}}),
            _node("end2", "END", {"output_mapping": {}}),
        ],
        [_edge("start", "end"), _edge("start2", "end2")],
    ),
    "edge_to_nowhere": _canvas(
        [_node("start", "A2A_START", _START), _node("end", "END")],
        [_edge("start", "ghost"), _edge("phantom", "end")],
    ),
    "tool_wiring": _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("agent", "ORCHESTRATOR_AGENT", {"model": "gemini-2.0-flash"}),
            _node("shape", "TRANSFORM", {"mode": "jmespath", "expression": "@"}),
            _node("group", "SEQUENTIAL_AGENT", {}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        [
            _edge("start", "agent"),
            # A transform is not a tool; a transform has no tools socket.
            _edge("shape", "agent", "output", "tools"),
            _edge("agent", "shape", "output", "tools"),
            _edge("group", "end"),
            _edge("agent", "end"),
        ],
    ),
    "fork_without_merge": _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("fork", "PARALLEL_FORK", {"branches": ["a", "b"]}),
            _node("left", "TRANSFORM", {"mode": "jmespath", "expression": "@"}),
            _node("right", "TRANSFORM", {"mode": "jmespath", "expression": "@"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        [
            _edge("start", "fork"),
            _edge("fork", "left", "a"), _edge("fork", "right", "b"),
            _edge("left", "end"),
        ],
    ),
    "unreachable_and_orphaned": _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("island", "TRANSFORM", {"mode": "jmespath", "expression": "@"}),
            _node("floating", "WAIT", {"duration": 2, "unit": "seconds"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        [_edge("start", "end"), _edge("island", "island")],
    ),
    "human_and_loop_handles": _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("ask", "HUMAN_APPROVAL", {"prompt": "ok?"}),
            _node("repeat", "LOOP", {"mode": "for_each", "items_path": "items"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        [
            _edge("start", "ask"),
            _edge("ask", "repeat", "approved"),
            _edge("repeat", "end", "done"),
        ],
    ),
    "bad_configs": _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("shape", "TRANSFORM", {"mode": "fields", "output_fields": []}),
            _node("pick", "CONDITION", {"branches": []}),
            _node("pause", "WAIT", {"duration": 0, "unit": "seconds"}),
            _node("call", "MCP_TOOL", {}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        [
            _edge("start", "shape"), _edge("shape", "pick"),
            _edge("pick", "pause", "default"), _edge("pause", "call"),
            _edge("call", "end"),
        ],
    ),
    "variables_and_mapping": _canvas(
        [
            _node("start", "A2A_START", {**_START, "output_variable": "wf"}),
            _node("shape", "TRANSFORM", {"mode": "fields", "output_variable": "dup",
                                         "output_fields": [
                {"name": "a", "type": "string", "source": "data.nope"},
                {"name": "a", "type": "integer", "source": "data.sku"},
                {"name": "", "type": "string"},
            ]}),
            _node("again", "TRANSFORM", {"mode": "jmespath", "expression": "@",
                                         "output_variable": "dup"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        [_edge("start", "shape"), _edge("shape", "again"), _edge("again", "end")],
    ),
    "duplicate_edges": _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("shape", "TRANSFORM", {"mode": "jmespath", "expression": "@"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        [_edge("start", "shape"), _edge("start", "shape"), _edge("shape", "end")],
    ),
    "retired_trigger": _canvas(
        [
            _node("old", "HTTP_TRIGGER", {}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        [_edge("old", "end")],
    ),
}


def _capture() -> dict[str, dict[str, list[str]]]:
    captured: dict[str, dict[str, list[str]]] = {}
    for name, canvas in CASES.items():
        errors, warnings_out = validate_semantics(CanvasPayload.model_validate(canvas))
        captured[name] = {"errors": errors, "warnings": warnings_out}
    return captured


def test_the_messages_have_not_changed():
    actual = _capture()

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, indent=2) + "\n")
        pytest.skip(f"rewrote {GOLDEN.name}")

    assert GOLDEN.exists(), (
        f"no golden file; run UPDATE_GOLDEN=1 pytest {Path(__file__).name}"
    )
    expected = json.loads(GOLDEN.read_text())

    for name in sorted(set(expected) | set(actual)):
        assert actual.get(name) == expected.get(name), (
            f"the validator's output changed for '{name}'. If that was "
            f"deliberate: UPDATE_GOLDEN=1 pytest {Path(__file__).name} and "
            "review the diff."
        )


def test_the_cases_actually_reach_the_rules():
    """A golden over silence would pass forever and prove nothing."""
    captured = _capture()
    total = sum(
        len(c["errors"]) + len(c["warnings"]) for c in captured.values()
    )
    assert total > 25, f"only {total} messages captured"
    assert captured["healthy"] == {"errors": [], "warnings": []}


# ── The anchors, which are the point of the refactor ─────────────────────────


def test_every_finding_points_at_something_that_exists():
    """An anchor naming a node that is not on the canvas would mark nothing.

    The one deliberate exception is the missing-endpoint rule: `Edge 'e1':
    source node 'x' does not exist` anchors the *edge*, because `x` is
    precisely the id that is not there.
    """
    from app.compiler.semantic_validator import collect_findings

    for name, canvas in CASES.items():
        payload = CanvasPayload.model_validate(canvas)
        node_ids = {n.id for n in payload.nodes}
        edge_ids = {e.id for e in payload.edges}

        for finding in collect_findings(payload).items:
            if finding.node_id is not None:
                assert finding.node_id in node_ids, (name, finding.message)
            if finding.edge_id is not None:
                assert finding.edge_id in edge_ids, (name, finding.message)


def test_most_findings_are_anchored():
    """Rules about the workflow as a whole have nowhere to point, and that is
    fine — but if the bulk were unanchored the canvas would have nothing to
    mark and the feature would be pointless."""
    from app.compiler.semantic_validator import collect_findings

    anchored = unanchored = 0
    for canvas in CASES.values():
        for finding in collect_findings(CanvasPayload.model_validate(canvas)).items:
            if finding.subject == "workflow":
                unanchored += 1
            else:
                anchored += 1

    assert anchored > unanchored * 3, f"{anchored} anchored, {unanchored} not"


def test_the_canvas_gets_the_sentence_without_the_node_id():
    """`Node 'x' (TOOL) cannot be used as a tool` reads badly on a canvas.

    The prefix is removed by reconstructing it from the anchor, not by matching
    a pattern — so a message that does not start with it is returned whole
    rather than mangled.
    """
    from app.compiler.semantic_validator import collect_findings

    payload = CanvasPayload.model_validate(CASES["tool_wiring"])
    types = {n.id: n.type for n in payload.nodes}
    findings = collect_findings(payload).items

    tool_error = next(f for f in findings if "cannot be used as a tool" in f.message)
    assert tool_error.node_id == "shape"
    assert tool_error.message.startswith("Node 'shape' (TRANSFORM)")
    # What the canvas shows, headed by the node's own title instead.
    assert tool_error.detail(types).startswith("cannot be used as a tool")
    assert "shape" not in tool_error.detail(types)


def test_a_message_with_no_such_prefix_survives_whole():
    from app.compiler.findings import Finding

    finding = Finding("Workflow must have exactly one END node", node_id="a")
    assert finding.detail({"a": "END"}) == "Workflow must have exactly one END node"


def test_the_wire_shape_carries_both_forms():
    from app.compiler.semantic_validator import collect_findings

    payload = CanvasPayload.model_validate(CASES["tool_wiring"])
    types = {n.id: n.type for n in payload.nodes}
    wire = [f.to_dict(types) for f in collect_findings(payload).items]

    assert wire, "nothing to send"
    for item in wire:
        assert item["subject"] in ("node", "edge", "workflow")
        assert item["text"], "the compile-log form must always be present"
        assert item["message"], "the canvas form must always be present"


# ── The shape stored in the database ─────────────────────────────────────────


def test_a_stored_finding_keeps_the_message_a_reader_expects():
    """`validation_errors` now holds finding dicts where it held strings.

    The column is JSON and already typed `list[Any]`, so this needed no
    migration -- which mattered, because the project has none: `create_all`
    builds the schema and will not ALTER an existing table. Rows written before
    this hold strings, so both shapes share the column and every reader has to
    cope. `text` is the string that used to be stored.
    """
    from app.compiler import collect_all_findings

    payload = CanvasPayload.model_validate(CASES["tool_wiring"])
    types = {n.id: n.type for n in payload.nodes}
    stored = [f.to_dict(types) for f in collect_all_findings(payload).items]

    errors, _warnings = validate_semantics(payload)
    assert [f["text"] for f in stored if f["severity"] == "error"] == errors


def test_the_cycle_message_does_not_depend_on_the_weather():
    """`nx.simple_cycles` may start the same cycle anywhere in it, and does.

    The identical canvas produced "agent -> shape -> agent" on one run and
    "shape -> agent -> shape" on the next, depending only on what else had been
    imported first. Rotating to the lowest id makes it reproducible -- and
    testable at all.
    """
    from app.compiler.semantic_validator import _rotate_cycle

    assert _rotate_cycle(["shape", "agent"]) == ["agent", "shape"]
    assert _rotate_cycle(["agent", "shape"]) == ["agent", "shape"]
    assert _rotate_cycle(["c", "a", "b"]) == ["a", "b", "c"]
    assert _rotate_cycle([]) == []


def test_a_cycle_marks_every_node_in_it():
    """One sentence, several places to put a mark."""
    from app.compiler.semantic_validator import collect_findings

    payload = CanvasPayload.model_validate(CASES["tool_wiring"])
    cycle = next(
        f for f in collect_findings(payload).items if "wired in a loop" in f.message
    )
    assert set(cycle.related_node_ids) == {"agent", "shape"}
