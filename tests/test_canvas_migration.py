"""The v1 -> v2 canvas migration: triggers become a single A2A_START node."""

from __future__ import annotations

import pytest

from app.compiler.canvas_migrations import (
    canvas_schema_version,
    migrate_canvas,
    needs_migration,
)
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload


def _node(node_id: str, node_type: str, config: dict | None = None, **extra) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": "", "description": ""},
        "config": config or {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1}, "on_error": "fail"},
        **extra,
    }


def _edge(edge_id: str, source: str, target: str, **extra) -> dict:
    return {
        "id": edge_id,
        "source": source,
        "source_handle": "output",
        "target": target,
        "target_handle": "input",
        "condition": None,
        **extra,
    }


# ── Versioning ───────────────────────────────────────────────────────────────


def test_missing_schema_version_reads_as_1():
    assert canvas_schema_version({"nodes": [], "edges": []}) == 1
    assert canvas_schema_version({"schema_version": None}) == 1
    assert canvas_schema_version({"schema_version": "not a number"}) == 1


def test_needs_migration_only_below_current():
    assert needs_migration({"nodes": [], "edges": []})
    assert not needs_migration({"schema_version": CURRENT_SCHEMA_VERSION})


def test_migration_is_idempotent():
    canvas = {
        "nodes": [_node("t", "HTTP_TRIGGER"), _node("e", "END")],
        "edges": [_edge("e1", "t", "e")],
    }

    once, notes = migrate_canvas(canvas)
    assert notes
    twice, notes_again = migrate_canvas(once)

    assert once == twice
    assert notes_again == []


def test_input_is_not_mutated():
    canvas = {"nodes": [_node("t", "HTTP_TRIGGER")], "edges": []}
    before = canvas["nodes"][0]["type"]

    migrate_canvas(canvas)

    assert canvas["nodes"][0]["type"] == before
    assert "schema_version" not in canvas


# ── Trigger rewriting ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "old_type",
    ["HTTP_TRIGGER", "SCHEDULE_TRIGGER", "WEBHOOK_TRIGGER", "QUEUE_TRIGGER"],
)
def test_every_retired_trigger_becomes_a2a_start(old_type):
    canvas = {
        "nodes": [_node("trigger_1", old_type), _node("end_1", "END")],
        "edges": [_edge("edge_1", "trigger_1", "end_1")],
    }

    migrated, notes = migrate_canvas(canvas)

    start = migrated["nodes"][0]
    assert start["type"] == "A2A_START"
    assert start["id"] == "trigger_1"           # id preserved
    assert start["position"] == {"x": 0, "y": 0}  # position preserved
    assert migrated["edges"] == canvas["edges"]   # edges untouched
    assert any("A2A_START" in note for note in notes)


def test_http_trigger_body_schema_becomes_payload_schema():
    """The Run modal's field list carries over, so the test form still works."""
    fields = [
        {"name": "text", "type": "text", "description": "The input", "required": True},
        {"name": "count", "type": "integer", "description": "", "required": False},
    ]
    canvas = {
        "nodes": [
            _node("t", "HTTP_TRIGGER", {"path": "/run", "method": "POST", "body_schema": {"fields": fields}}),
            _node("e", "END"),
        ],
        "edges": [_edge("e1", "t", "e")],
    }

    migrated, notes = migrate_canvas(canvas)

    config = migrated["nodes"][0]["config"]
    assert config["payload_schema"] == {"fields": fields}
    assert config["input_mode"] == "json"
    assert config["state_key"] == "wf"
    # The retired HTTP settings are gone from config...
    assert "path" not in config
    assert "method" not in config
    assert any("body_schema" in note for note in notes)


def test_retired_settings_are_recorded_not_silently_dropped():
    """A cron expression is real information; losing it without a trace is worse
    than keeping it in the node's description."""
    canvas = {
        "nodes": [
            _node("t", "SCHEDULE_TRIGGER", {"cron": "0 9 * * 1-5", "timezone": "UTC"}),
            _node("e", "END"),
        ],
        "edges": [_edge("e1", "t", "e")],
    }

    migrated, _ = migrate_canvas(canvas)

    description = migrated["nodes"][0]["metadata"]["description"]
    assert "0 9 * * 1-5" in description
    assert "SCHEDULE_TRIGGER" in description


def test_existing_description_is_kept_when_recording_old_settings():
    canvas = {
        "nodes": [
            {
                **_node("t", "SCHEDULE_TRIGGER", {"cron": "* * * * *"}),
                "metadata": {"title": "Nightly", "description": "Runs the report."},
            },
            _node("e", "END"),
        ],
        "edges": [_edge("e1", "t", "e")],
    }

    migrated, _ = migrate_canvas(canvas)

    metadata = migrated["nodes"][0]["metadata"]
    assert metadata["title"] == "Nightly"          # user's title wins
    assert metadata["description"].startswith("Runs the report.")
    assert "* * * * *" in metadata["description"]


def test_untitled_trigger_gets_a_default_title():
    canvas = {"nodes": [_node("t", "HTTP_TRIGGER")], "edges": []}

    migrated, _ = migrate_canvas(canvas)

    assert migrated["nodes"][0]["metadata"]["title"] == "A2A Start"


# ── Several triggers collapse to one entry ───────────────────────────────────


def test_extra_triggers_and_their_edges_are_removed():
    """A workflow may now have only one entry node."""
    canvas = {
        "nodes": [
            _node("http", "HTTP_TRIGGER"),
            _node("cron", "SCHEDULE_TRIGGER", {"cron": "* * * * *"}),
            _node("mid", "TRANSFORM"),
            _node("end", "END"),
        ],
        "edges": [
            _edge("e1", "http", "mid"),
            _edge("e2", "cron", "mid"),
            _edge("e3", "mid", "end"),
        ],
    }

    migrated, notes = migrate_canvas(canvas)

    ids = [n["id"] for n in migrated["nodes"]]
    assert ids == ["http", "mid", "end"]
    assert [n["type"] for n in migrated["nodes"]][0] == "A2A_START"

    edge_ids = [e["id"] for e in migrated["edges"]]
    assert edge_ids == ["e1", "e3"]  # the dropped trigger's edge went with it
    assert any("removed" in note for note in notes)
    assert any("1 edge(s) removed" in note for note in notes)


def test_canvas_with_no_trigger_is_only_stamped():
    canvas = {
        "nodes": [_node("mid", "TRANSFORM"), _node("end", "END")],
        "edges": [_edge("e1", "mid", "end")],
    }

    migrated, notes = migrate_canvas(canvas)

    assert notes == []
    assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
    assert [n["type"] for n in migrated["nodes"]] == ["TRANSFORM", "END"]


def test_non_dict_input_is_returned_unchanged():
    assert migrate_canvas(None) == (None, [])
    assert migrate_canvas([]) == ([], [])


# ── The migrated canvas is a valid CanvasPayload ─────────────────────────────


def test_migrated_canvas_parses_as_a_canvas_payload():
    canvas = {
        "nodes": [
            _node("t", "HTTP_TRIGGER", {"path": "/x", "body_schema": {"fields": [{"name": "q", "type": "string", "description": "", "required": True}]}}),
            _node("mid", "TRANSFORM", {"mode": "jmespath", "expression": "q"}),
            _node("end", "END"),
        ],
        "edges": [_edge("e1", "t", "mid"), _edge("e2", "mid", "end")],
    }

    migrated, _ = migrate_canvas(canvas)
    payload = CanvasPayload.model_validate(migrated)

    assert payload.schema_version == CURRENT_SCHEMA_VERSION
    assert payload.nodes[0].type == "A2A_START"


def test_migrated_canvas_compiles():
    from app.compiler import run_compiler

    canvas = {
        "nodes": [
            _node("t", "HTTP_TRIGGER", {"body_schema": {"fields": [{"name": "q", "type": "string", "description": "", "required": True}]}}),
            _node("mid", "TRANSFORM", {"mode": "jmespath", "expression": "q"}),
            _node("end", "END"),
        ],
        "edges": [_edge("e1", "t", "mid"), _edge("e2", "mid", "end")],
    }

    migrated, _ = migrate_canvas(canvas)
    errors, _warnings, ir = run_compiler(CanvasPayload.model_validate(migrated), "v1")

    assert errors == []
    assert ir is not None
    assert ir.entrypoints == ["t"]
    assert ir.nodes["t"].kind == "trigger.a2a_start"


# ── v6 -> v7: the fork/merge pair the UI could not draw ──────────────────────


def _fork_canvas(fork_config: dict, merge_config: dict, *, wired: bool) -> dict:
    edges = [_edge("e0", "start", "fork"), _edge("e3", "join", "end")]
    if wired:
        edges += [
            _edge("e1", "fork", "a", source_handle="left"),
            _edge("e2", "fork", "b", source_handle="right"),
            _edge("e4", "a", "join"),
            _edge("e5", "b", "join"),
        ]
    return {
        "schema_version": 6,
        "nodes": [
            _node("start", "A2A_START", {"input_mode": "json", "state_key": "wf"}),
            _node("fork", "PARALLEL_FORK", fork_config),
            _node("a", "TRANSFORM", {"mode": "jmespath", "expression": "@"}),
            _node("b", "TRANSFORM", {"mode": "jmespath", "expression": "@"}),
            _node("join", "MERGE", merge_config),
            _node("end", "END"),
        ],
        "edges": edges,
    }


def _config(canvas: dict, node_id: str) -> dict:
    return next(n for n in canvas["nodes"] if n["id"] == node_id)["config"]


@pytest.mark.parametrize("strategy", ["all", "first", "any", "wait_all", "wait_first"])
def test_a_merges_strategy_is_dropped(strategy):
    """A MERGE compiles to an ADK JoinNode, which always waits for every branch.

    `_requires_all_predecessors` is True on the class and not configurable, so
    "continue on the first" was never implementable. The config panel made it
    worse by offering `all` / `first` / `any`, none of which were in the
    schema's enum — so every Merge configured from the UI failed validation.
    """
    migrated, notes = migrate_canvas(
        _fork_canvas({"branches": ["left", "right"]}, {"strategy": strategy},
                     wired=True)
    )

    assert "strategy" not in _config(migrated, "join")
    assert any("strategy" in note for note in notes), notes


def test_how_a_merge_combines_its_branches_is_kept():
    """`merge_mode` is the one setting that does something."""
    migrated, notes = migrate_canvas(
        _fork_canvas({"branches": ["left", "right"]},
                     {"strategy": "all", "merge_mode": "array"}, wired=True)
    )
    assert _config(migrated, "join")["merge_mode"] == "array"
    assert not any("merge_mode" in note for note in notes), notes


def test_a_forks_branches_are_recovered_from_the_edges_that_left_it():
    """A fork dropped from the palette had no `branches`, so it had no handles.

    Edges could still be drawn programmatically (or by a seed), and their
    handles say what the branches were meant to be.
    """
    migrated, notes = migrate_canvas(
        _fork_canvas({}, {"strategy": "wait_all"}, wired=True)
    )

    assert _config(migrated, "fork")["branches"] == ["left", "right"]
    assert any("branches" in note for note in notes), notes


def test_an_unwired_fork_gets_a_usable_default_pair():
    """Two, because one branch is not a fork and the schema requires two."""
    migrated, _notes = migrate_canvas(
        _fork_canvas({}, {"strategy": "wait_all"}, wired=False)
    )
    assert _config(migrated, "fork")["branches"] == ["branch_1", "branch_2"]


def test_a_forks_variable_is_dropped():
    """It hands each branch the payload it was given; there is nothing to name."""
    migrated, notes = migrate_canvas(
        _fork_canvas({"branches": ["left", "right"], "output_variable": "fanned"},
                     {"strategy": "wait_all"}, wired=True)
    )

    assert "output_variable" not in _config(migrated, "fork")
    assert any("output_variable" in note for note in notes), notes


def test_a_canvas_the_ui_used_to_produce_now_compiles():
    """The whole point: a fork and merge drawn in the panel were dead on arrival.

    An empty `branches` failed the schema (minItems 2) and `strategy: "all"`
    failed the enum, so neither node could ever reach the compiler.
    """
    from app.compiler import run_compiler

    broken = _fork_canvas({}, {"strategy": "all"}, wired=True)
    migrated, _notes = migrate_canvas(broken)
    errors, _warnings, ir = run_compiler(
        CanvasPayload.model_validate(migrated), "v7"
    )

    assert errors == [], errors
    assert sorted(ir.nodes["fork"].branches) == ["left", "right"]
