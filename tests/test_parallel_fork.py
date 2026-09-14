"""PARALLEL_FORK: fanning one payload out to several nodes at once.

The fork is not a routing node even though it has several outgoing handles.
ADK fans out along every plain outgoing edge on its own, so the generated
module returns a plain `Event(output=...)` and never a route — the branch names
exist so the canvas can tell one handle from another, and so an edge knows
which handle it leaves from.

Two consequences the tests here pin down: the number of branches is whatever
the canvas draws rather than a fixed two, and a fork produces nothing of its
own to name.
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings

from app.compiler import build_graph_plan, run_compiler
from app.compiler.semantic_validator import validate_semantics
from app.nodes.registry import NODE_REGISTRY
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
        {"name": "x", "type": "integer", "description": "", "required": True},
    ]},
}


def _fan(count: int) -> dict:
    """A fork with `count` branches, each doing its own arithmetic, then MERGE."""
    names = [f"branch_{i + 1}" for i in range(count)]
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", _START),
            _node("fork", "PARALLEL_FORK", {"branches": names}),
            *[
                _node(f"work_{i + 1}", "TRANSFORM", {
                    "mode": "python",
                    "expression": f"result = {{'r{i + 1}': (data.get('x') or 0) * {i + 1}}}",
                })
                for i in range(count)
            ],
            _node("join", "MERGE", {"strategy": "wait_all"}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [
            _edge("start", "fork"),
            *[_edge("fork", f"work_{i + 1}", names[i]) for i in range(count)],
            *[_edge(f"work_{i + 1}", "join") for i in range(count)],
            _edge("join", "end"),
        ],
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
        check=False,  # a failed run is a result here, not an error
    )
    assert result.returncode in (0, 1), f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── Fanning out to more than two ─────────────────────────────────────────────


def test_a_fork_takes_as_many_branches_as_the_canvas_draws():
    """Nothing caps it at two; the branch list is whatever was wired."""
    canvas = CanvasPayload.model_validate(_fan(5))
    errors, _warnings = validate_semantics(canvas)
    assert errors == []

    _errors, _warnings, ir = run_compiler(canvas, "fork-five")
    assert sorted(ir.nodes["fork"].branches) == [
        "branch_1", "branch_2", "branch_3", "branch_4", "branch_5",
    ]


def test_four_branches_all_run_and_all_reach_the_merge(tmp_path):
    destination = _render(_fan(4), "fork-four-0001", tmp_path)
    envelope = _run(destination, {"x": 3})

    assert envelope["state"] == "completed", envelope.get("error")
    # Generated modules are renamed by type and index, so the trace is counted
    # rather than matched against the canvas ids.
    ran = {step["node"] for step in envelope["trace"]}
    assert len([n for n in ran if "transform" in n]) == 4, sorted(ran)

    # Every branch's own key survives the merge.
    result = json.dumps(envelope["result"])
    for i in range(1, 5):
        assert f'"r{i}"' in result, (i, result)


def test_the_branches_run_concurrently_not_one_after_another(tmp_path):
    """Four 1s waits finish in about a second, not four.

    Asserted from the trace timestamps rather than wall clock, which would be
    swamped by interpreter start-up.
    """
    canvas = _fan(4)
    for node in canvas["nodes"]:
        if node["id"].startswith("work_"):
            node["type"] = "WAIT"
            node["config"] = {"duration": 1, "unit": "seconds"}

    destination = _render(canvas, "fork-concurrent-0001", tmp_path)
    envelope = _run(destination, {"x": 1})
    assert envelope["state"] == "completed", envelope.get("error")

    trace = envelope["trace"]
    span = max(s["at"] for s in trace) - min(s["at"] for s in trace)
    assert span < 2.5, f"the graph took {span:.2f}s, about the sum of the waits"


def test_one_branch_is_rejected():
    """A fork with a single branch has nothing to run in parallel."""
    canvas = _fan(2)
    canvas["edges"] = [e for e in canvas["edges"] if e["source"] != "fork"] + [
        _edge("fork", "work_1", "branch_1")
    ]
    errors, _warnings = validate_semantics(CanvasPayload.model_validate(canvas))
    assert any("at least 2 outbound edges" in e for e in errors), errors


def test_branches_that_never_rejoin_are_rejected():
    """ADK fails the run with several terminal outputs, which reads as nonsense."""
    canvas = _fan(3)
    canvas["edges"] = [e for e in canvas["edges"] if e["target"] != "join"]
    canvas["nodes"].append(_node("end2", "END", {"output_mapping": {}}))
    canvas["edges"] += [_edge("work_1", "end"), _edge("work_2", "end2")]

    errors, _warnings = validate_semantics(CanvasPayload.model_validate(canvas))
    assert any("never rejoin" in e for e in errors), errors


# ── A fork has no result of its own ──────────────────────────────────────────


def test_a_fork_cannot_be_given_a_variable():
    """It hands each branch the payload it was given, unchanged.

    Naming that would save a second copy of whatever the node before it already
    produced, under a name that suggests the fork did something.
    """
    assert "output_variable" not in (
        NODE_REGISTRY["PARALLEL_FORK"].config_schema["properties"]
    )


def test_the_generated_fork_saves_nothing(tmp_path):
    destination = _render(_fan(3), "fork-novar-0001", tmp_path)
    source = next((destination / "nodes").glob("*fork*.py")).read_text()

    assert "OUTPUT_VARIABLE" not in source
    decorator = next(line for line in source.splitlines()
                     if line.startswith("@flow_node("))
    assert "variable=" not in decorator, decorator
    # And it still routes nothing: ADK fans out along the plain edges itself.
    assert "route_event" not in source


def test_each_branch_receives_the_same_payload(tmp_path):
    destination = _render(_fan(3), "fork-same-0001", tmp_path)
    result = _run(destination, {"x": 7})["result"]

    # r1 = x*1, r2 = x*2, r3 = x*3 — so every branch saw x = 7.
    assert (result["r1"], result["r2"], result["r3"]) == (7, 14, 21)


# ── What the branches look like to the node after the merge ──────────────────


def _merged(mode: str, tmp_path, *, payload=None) -> dict:
    """What the node *after* a 3-branch MERGE actually receives.

    Asserted on the successor's input rather than the workflow's final result:
    every node merges its output into workflow state, and the END node reports
    that accumulated state — so a branch the merge dropped would still show up
    there. The successor's `node_input` is the thing this is about.
    """
    canvas = _fan(3)
    for node in canvas["nodes"]:
        if node["id"] == "join":
            node["config"] = {"merge_mode": mode}
    canvas["nodes"].append(_node("probe", "TRANSFORM", {
        "mode": "python",
        "expression": "result = {'seen': dict(data)}",
    }))
    canvas["edges"] = [e for e in canvas["edges"] if e["source"] != "join"] + [
        _edge("join", "probe"), _edge("probe", "end"),
    ]
    destination = _render(canvas, f"merge-{mode}-0001", tmp_path)
    return _run(destination, payload or {"x": 2})["result"]["seen"]


def test_merge_hands_the_next_node_one_flat_payload(tmp_path):
    """Not ADK's raw join shape, which is keyed by predecessor node name.

    A JoinNode passes the aggregated inputs straight through, so the next node
    saw `{"n_transform_7": {"r1": 2}, "n_transform_2": {"r2": 4}, ...}` and a
    transform asking for `data["r1"]` got nothing. Measured before the fix: a
    totalling step after a four-branch fork returned the untouched input.
    """
    result = _merged("merge", tmp_path)

    assert result["r1"] == 2 and result["r2"] == 4 and result["r3"] == 6
    assert not any(key.startswith("n_") for key in result), result


def test_merge_can_hand_over_a_list_instead(tmp_path):
    result = _merged("array", tmp_path)

    assert [r["r1"] for r in result["results"] if "r1" in r] == [2]
    assert len(result["results"]) == 3


def test_merge_can_keep_only_the_first_branch(tmp_path):
    result = _merged("first", tmp_path)

    # branch_1 is the first edge drawn from the fork, so it is r1 that survives.
    assert "r1" in result
    assert "r2" not in result and "r3" not in result


def test_the_combined_result_is_the_same_every_run(tmp_path):
    """Branch order comes from the canvas, not from which branch finished first.

    ADK keys the aggregated dict by arrival, which is a race — so `array` and
    `first` would otherwise give a different answer on different runs, and the
    package's own suite checks that a blocking call and a polled call agree.
    """
    canvas = _fan(3)
    for node in canvas["nodes"]:
        if node["id"] == "join":
            node["config"] = {"merge_mode": "array"}
    destination = _render(canvas, "merge-stable-0001", tmp_path)

    runs = [_run(destination, {"x": 2})["result"] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2], runs


def test_a_merge_always_waits_for_every_branch():
    """There is no 'continue on the first' setting, because there cannot be.

    `JoinNode._requires_all_predecessors` is True on the class, so the config
    schema offers nothing that would claim otherwise.
    """
    from google.adk.workflow import JoinNode

    assert JoinNode(name="j")._requires_all_predecessors is True
    assert "strategy" not in NODE_REGISTRY["MERGE"].config_schema["properties"]
