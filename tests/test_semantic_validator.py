"""Canvas validation rules, especially the ones standing in for ADK constraints.

Several rules exist to fail at save time rather than at run time. Where that is
the case the test says which ADK failure it prevents; the corresponding ADK
behaviour is proven in tests/test_adk_contract.py.
"""

from __future__ import annotations

import pytest

from app.compiler import run_compiler
from app.compiler.semantic_validator import validate_semantics
from app.schemas.canvas import CanvasPayload


def _node(node_id: str, node_type: str, config: dict | None = None) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": "", "description": ""},
        "config": config if config is not None else {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1}, "on_error": "fail"},
    }


def _edge(source: str, target: str, source_handle: str = "output", target_handle: str = "input") -> dict:
    return {
        "id": f"e_{source}_{target}_{source_handle}",
        "source": source,
        "source_handle": source_handle,
        "target": target,
        "target_handle": target_handle,
        "condition": None,
    }


def _canvas(nodes: list[dict], edges: list[dict]) -> CanvasPayload:
    return CanvasPayload.model_validate({"nodes": nodes, "edges": edges})


_START_FIELDS = {
    "payload_schema": {
        "fields": [{"name": "text", "type": "string", "description": "", "required": True}]
    }
}


def _valid_canvas() -> CanvasPayload:
    """A2A_START -> TRANSFORM -> END: the smallest valid workflow."""
    return _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("mid", "TRANSFORM", {"mode": "jmespath", "expression": "text"}),
            _node("end", "END"),
        ],
        [_edge("start", "mid"), _edge("mid", "end")],
    )


def test_minimal_valid_workflow_has_no_errors_or_warnings():
    errors, warnings = validate_semantics(_valid_canvas())
    assert errors == []
    assert warnings == []


def test_it_compiles():
    errors, warnings, ir = run_compiler(_valid_canvas(), "v1")
    assert errors == []
    assert ir is not None
    assert ir.entrypoints == ["start"]


# ── Entry node ───────────────────────────────────────────────────────────────


def test_missing_entry_node_is_an_error():
    canvas = _canvas(
        [_node("mid", "TRANSFORM"), _node("end", "END")],
        [_edge("mid", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("exactly one A2A_START" in e and "none" in e for e in errors)


def test_two_entry_nodes_is_an_error():
    canvas = _canvas(
        [
            _node("s1", "A2A_START", _START_FIELDS),
            _node("s2", "A2A_START", _START_FIELDS),
            _node("end", "END"),
        ],
        [_edge("s1", "end"), _edge("s2", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("exactly one A2A_START" in e and "found 2" in e for e in errors)


def test_entry_node_may_not_have_inbound_edges():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("mid", "TRANSFORM"),
            _node("end", "END"),
        ],
        [_edge("start", "mid"), _edge("mid", "start"), _edge("mid", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("must not have inbound edges" in e for e in errors)


def test_retired_trigger_type_gets_a_migration_hint():
    """A stale canvas should say what to do, not just 'unknown node type'."""
    canvas = _canvas(
        [_node("t", "HTTP_TRIGGER"), _node("end", "END")],
        [_edge("t", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("no longer exists" in e and "A2A_START" in e for e in errors)


# ── Terminal node — ADK allows at most one ───────────────────────────────────


def test_missing_end_node_is_an_error():
    canvas = _canvas(
        [_node("start", "A2A_START", _START_FIELDS), _node("mid", "TRANSFORM")],
        [_edge("start", "mid")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("exactly one END" in e and "none" in e for e in errors)


def test_two_end_nodes_is_an_error():
    """ADK: 'multiple terminal nodes produced output' — raised at finalisation,
    after the work is already done. Catch it here instead."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("router", "CONDITION", {"branches": [{"name": "a", "expression": "True"}]}),
            _node("end1", "END"),
            _node("end2", "END"),
        ],
        [
            _edge("start", "router"),
            _edge("router", "end1", source_handle="a"),
            _edge("router", "end2", source_handle="default"),
        ],
    )
    errors, _ = validate_semantics(canvas)
    assert any("exactly one END" in e and "found 2" in e for e in errors)
    assert any("MERGE" in e for e in errors)


def test_end_node_may_not_have_outbound_edges():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("end", "END"),
            _node("mid", "TRANSFORM"),
        ],
        [_edge("start", "end"), _edge("end", "mid")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("terminal node and must not have outbound edges" in e for e in errors)


def test_dangling_flow_node_would_be_a_second_terminal():
    """A node with inbound but no outbound edge ends a path of its own."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("fork", "CONDITION", {"branches": [{"name": "a", "expression": "True"}]}),
            _node("orphan_tail", "TRANSFORM"),
            _node("end", "END"),
        ],
        [
            _edge("start", "fork"),
            _edge("fork", "orphan_tail", source_handle="a"),
            _edge("fork", "end", source_handle="default"),
        ],
    )
    errors, _ = validate_semantics(canvas)
    assert any("orphan_tail" in e and "no outbound edge" in e for e in errors)


# ── Parallel branches must reconverge ────────────────────────────────────────


def test_parallel_fork_needs_two_branches():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("fork", "PARALLEL_FORK"),
            _node("end", "END"),
        ],
        [_edge("start", "fork"), _edge("fork", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("at least 2 outbound edges" in e for e in errors)


def test_parallel_fork_that_reconverges_is_valid():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("fork", "PARALLEL_FORK"),
            _node("a", "TRANSFORM"),
            _node("b", "TRANSFORM"),
            _node("merge", "MERGE"),
            _node("end", "END"),
        ],
        [
            _edge("start", "fork"),
            _edge("fork", "a", source_handle="a"),
            _edge("fork", "b", source_handle="b"),
            _edge("a", "merge"),
            _edge("b", "merge"),
            _edge("merge", "end"),
        ],
    )
    errors, _ = validate_semantics(canvas)
    assert errors == []


def test_parallel_fork_whose_branches_never_rejoin_is_an_error():
    """Both branches run, then finalisation fails with two terminal outputs."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("fork", "PARALLEL_FORK"),
            _node("a", "TRANSFORM"),
            _node("b", "TRANSFORM"),
            _node("end", "END"),
        ],
        [
            _edge("start", "fork"),
            _edge("fork", "a", source_handle="a"),
            _edge("fork", "b", source_handle="b"),
            _edge("a", "end"),
            _edge("b", "end"),
        ],
    )
    errors, _ = validate_semantics(canvas)
    assert any("never rejoin" in e for e in errors)


# ── Duplicate edges — merged by the compiler, so a warning ───────────────────


def test_two_branches_to_the_same_target_warn_about_merging():
    """ADK: 'Duplicate edge found: from=X, to=Y' even when handles differ."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
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
        ],
        [
            _edge("start", "router"),
            _edge("router", "end", source_handle="yes"),
            _edge("router", "end", source_handle="no"),
        ],
    )
    errors, warnings = validate_semantics(canvas)
    assert errors == []
    assert any("merged into a" in w and "router" in w for w in warnings)


# ── Reachability and connectivity ────────────────────────────────────────────


def test_unreachable_node_is_an_error():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("island_a", "TRANSFORM"),
            _node("island_b", "TRANSFORM"),
            _node("end", "END"),
        ],
        [_edge("start", "end"), _edge("island_a", "island_b")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("island_a" in e and "not reachable" in e for e in errors)


def test_orphaned_node_is_only_a_warning():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("end", "END"),
            _node("floating", "TRANSFORM"),
        ],
        [_edge("start", "end")],
    )
    errors, warnings = validate_semantics(canvas)
    assert errors == []
    assert any("floating" in w and "not connected" in w for w in warnings)


def test_tool_providers_may_hang_off_an_orchestrator():
    """TOOL/REMOTE_AGENT wire into the "tools" handle, not the flow."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node("tool", "TOOL", {"mcp_url": "http://mcp:9000", "tool_name": "search"}),
            _node("end", "END"),
        ],
        [
            _edge("start", "orch"),
            _edge("tool", "orch", target_handle="tools"),
            _edge("orch", "end"),
        ],
    )
    errors, warnings = validate_semantics(canvas)
    assert errors == []
    assert not any("tool" in w and "no outbound" in w for w in warnings)


def test_missing_edge_endpoint_is_reported_alone():
    """Bail out early rather than cascade every graph rule off a broken edge."""
    canvas = _canvas(
        [_node("start", "A2A_START", _START_FIELDS), _node("end", "END")],
        [_edge("start", "does_not_exist")],
    )
    errors, warnings = validate_semantics(canvas)
    assert len(errors) == 1
    assert "does not exist" in errors[0]


# ── Cycles ───────────────────────────────────────────────────────────────────


def test_cycle_without_a_loop_node_is_an_error():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("a", "TRANSFORM"),
            _node("b", "TRANSFORM"),
            _node("end", "END"),
        ],
        [_edge("start", "a"), _edge("a", "b"), _edge("b", "a"), _edge("a", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("Cycle detected without a LOOP node" in e for e in errors)


def test_cycle_through_a_loop_node_is_allowed():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START_FIELDS),
            _node("loop", "LOOP", {"mode": "while", "exit_condition": "i > 3"}),
            _node("body", "TRANSFORM"),
            _node("end", "END"),
        ],
        [
            _edge("start", "loop"),
            _edge("loop", "body", source_handle="loop_body"),
            _edge("body", "loop"),
            _edge("loop", "end", source_handle="done"),
        ],
    )
    errors, _ = validate_semantics(canvas)
    assert not any("Cycle" in e for e in errors), errors


# ── A2A_START payload schema ─────────────────────────────────────────────────


def test_unnamed_payload_field_is_an_error():
    canvas = _canvas(
        [
            _node("start", "A2A_START", {"payload_schema": {"fields": [{"name": "  ", "type": "string"}]}}),
            _node("end", "END"),
        ],
        [_edge("start", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("payload field 1 has no name" in e for e in errors)


def test_duplicate_payload_field_names_are_an_error():
    canvas = _canvas(
        [
            _node(
                "start",
                "A2A_START",
                {"payload_schema": {"fields": [{"name": "q", "type": "string"}, {"name": "q", "type": "number"}]}},
            ),
            _node("end", "END"),
        ],
        [_edge("start", "end")],
    )
    errors, _ = validate_semantics(canvas)
    assert any("more than one payload field named 'q'" in e for e in errors)


def test_no_payload_fields_is_only_a_warning():
    canvas = _canvas(
        [_node("start", "A2A_START"), _node("end", "END")],
        [_edge("start", "end")],
    )
    errors, warnings = validate_semantics(canvas)
    assert errors == []
    assert any("declares no payload fields" in w for w in warnings)


# ── payload_schema -> JSON Schema conversion ─────────────────────────────────


def test_payload_json_schema_conversion():
    from app.nodes.triggers import payload_json_schema

    schema = payload_json_schema(
        {
            "payload_schema": {
                "fields": [
                    {"name": "text", "type": "text", "description": "Body", "required": True},
                    {"name": "count", "type": "integer", "description": "", "required": False},
                    {"name": "  ", "type": "string"},  # skipped
                ]
            }
        }
    )

    assert schema["type"] == "object"
    assert schema["properties"]["text"] == {
        "type": "string",
        "description": "Body",
        "x-ui-widget": "textarea",
    }
    assert schema["properties"]["count"] == {"type": "integer"}
    assert schema["required"] == ["text"]
    assert "  " not in schema["properties"]


def test_payload_json_schema_defaults():
    from app.nodes.triggers import payload_json_schema

    # No fields, json mode: accepts anything.
    assert payload_json_schema({}) == {"type": "object"}

    # No fields, text mode: the documented shape is {"text": ...}.
    text_mode = payload_json_schema({"input_mode": "text"})
    assert text_mode["required"] == ["text"]


def test_example_payload_matches_the_fields():
    from app.nodes.triggers import example_payload

    example = example_payload(
        {"payload_schema": {"fields": [{"name": "q", "type": "string"}, {"name": "n", "type": "integer"}]}}
    )
    assert example == {"q": "", "n": 0}
    assert example_payload({"input_mode": "text"}) == {"text": "hello"}


# ── A2A_START payload coercion ───────────────────────────────────────────────
#
# The node's own `execute` method is gone with the platform's second engine, and
# with it the `{"body": ..., "headers": ...}` envelope that engine wrapped
# payloads in. Inbound shapes are now normalised once, by
# `core/state.py::coerce_input` in the generated package, and covered by the
# package's own tests (rendered tests/test_nodes.py) plus
# tests/test_package_runner.py.
