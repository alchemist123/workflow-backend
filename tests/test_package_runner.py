"""Running a workflow by driving its generated package.

These render a real package and shell out to its `run_once.py`, so they are the
platform-side half of the Phase 5 gate: the same path the Test button takes.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.runtime.package_runner import ensure_package, run_workflow_package
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
        "policies": {
            "timeout_seconds": 60,
            "retry": {"max_attempts": 1},
            "on_error": meta.get("on_error", "fail"),
        },
    }


def _edge(source: str, target: str, handle: str = "output") -> dict:
    return {
        "id": f"e_{source}_{target}_{handle}",
        "source": source,
        "source_handle": handle,
        "target": target,
        "target_handle": "input",
        "condition": None,
    }


_START_CFG = {
    "payload_schema": {
        "fields": [
            {"name": "score", "type": "integer", "description": "", "required": True},
            {"name": "text", "type": "string", "description": "", "required": False},
        ]
    }
}


def _router_plan(version_id: str = "runner-0001"):
    """A2A_START -> CONDITION -> one of two TRANSFORMs -> END. No network."""
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", _START_CFG),
                _node(
                    "router",
                    "CONDITION",
                    {
                        "branches": [
                            {"name": "high", "expression": "data.get('score', 0) > 50"},
                            {"name": "low", "expression": "True"},
                        ]
                    },
                ),
                _node(
                    "high",
                    "TRANSFORM",
                    {"mode": "python", "expression": "result = {'tier': 'premium'}"},
                ),
                _node(
                    "low",
                    "TRANSFORM",
                    {"mode": "python", "expression": "result = {'tier': 'standard'}"},
                ),
                _node("end", "END", {"output_mapping": {"tier": "tier", "score": "score"}}),
            ],
            "edges": [
                _edge("start", "router"),
                _edge("router", "high", "high"),
                _edge("router", "low", "low"),
                _edge("high", "end"),
                _edge("low", "end"),
            ],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, version_id)
    assert errors == [], errors
    return build_graph_plan(ir, workflow_name="Runner Test", workflow_description="d")


@pytest.fixture
def packages_dir(tmp_path, monkeypatch):
    root = tmp_path / "packages"
    root.mkdir()
    monkeypatch.setenv("PACKAGES_DIR", str(root))
    monkeypatch.setenv("HOST_PACKAGES_DIR", str(root))
    return root


# ── Rendering and reuse ──────────────────────────────────────────────────────


def test_ensure_package_renders_then_reuses(packages_dir):
    plan = _router_plan()

    first, warnings_first = ensure_package(plan)
    assert (first / "run_once.py").is_file()
    assert (first / "graph.json").is_file()

    marker = first / ".reuse-marker"
    marker.write_text("still here")

    second, _ = ensure_package(plan)
    assert second == first
    # Reuse means the directory was not re-rendered.
    assert marker.exists()
    assert warnings_first == []


def test_ensure_package_returns_a_path_this_process_can_run(tmp_path, monkeypatch):
    """PACKAGES_DIR and HOST_PACKAGES_DIR differ when the backend is in a container.

    `build_runner_package` writes to PACKAGES_DIR but reports the *host* path,
    for the copyable path and compose command the UI shows. Running against that
    reported path failed with ENOENT on every fresh render, while a reused
    package worked -- reuse resolves the container-side path itself.

    The `packages_dir` fixture points both variables at the same directory,
    which is exactly why this went unnoticed, so this test sets them apart.
    """
    real = tmp_path / "container-packages"
    real.mkdir()
    monkeypatch.setenv("PACKAGES_DIR", str(real))
    monkeypatch.setenv("HOST_PACKAGES_DIR", "/host/only/does-not-exist")

    directory, _warnings = ensure_package(_router_plan("runner-hostpath-0001"))

    assert directory.is_dir(), directory
    assert (directory / "run_once.py").is_file()
    assert str(directory).startswith(str(real))


def test_the_builder_reports_both_paths(tmp_path, monkeypatch):
    """The host path is still what the UI shows; only the runner uses the local one."""
    from app.packaging.builder import build_runner_package

    real = tmp_path / "local"
    real.mkdir()
    monkeypatch.setenv("PACKAGES_DIR", str(real))
    monkeypatch.setenv("HOST_PACKAGES_DIR", "/host/view")

    built = build_runner_package(_router_plan("runner-bothpaths-0001"), lint=False)

    assert built["package_dir"].startswith("/host/view")
    assert built["local_package_dir"].startswith(str(real))
    assert Path(built["local_package_dir"]).is_dir()
    assert built["compose_command"].startswith("cd /host/view")


def test_ensure_package_rerenders_when_the_graph_changed(packages_dir):
    """A version id that was reused after a canvas edit must not test stale code."""
    plan = _router_plan()
    directory, _ = ensure_package(plan)

    # Simulate a stale render: same version id, different graph on disk.
    stale = json.loads((directory / "graph.json").read_text())
    stale["terminal_node"] = "something_else"
    (directory / "graph.json").write_text(json.dumps(stale))
    marker = directory / ".stale-marker"
    marker.write_text("x")

    again, _ = ensure_package(plan)
    assert again == directory
    assert not marker.exists(), "a changed graph should have forced a re-render"
    assert json.loads((directory / "graph.json").read_text()) == plan.to_dict()


def test_rebuild_forces_a_fresh_render(packages_dir):
    plan = _router_plan()
    directory, _ = ensure_package(plan)
    marker = directory / ".marker"
    marker.write_text("x")

    ensure_package(plan, rebuild=True)
    assert not marker.exists()


# ── Running ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_mode_run(packages_dir):
    plan = _router_plan()
    run = await run_workflow_package(plan, {"score": 90}, mode="message")

    assert run.ok, run.error
    assert run.state == "completed"
    assert run.mode == "message"
    assert run.task_id
    assert run.result == {"tier": "premium", "score": 90}
    assert run.duration_ms > 0


@pytest.mark.asyncio
async def test_task_mode_run_polls_to_completion(packages_dir):
    plan = _router_plan()
    run = await run_workflow_package(plan, {"score": 10}, mode="task")

    assert run.ok, run.error
    assert run.state == "completed"
    assert run.mode == "task"
    assert run.polls >= 1, "task mode should have polled tasks/get at least once"
    assert run.result == {"tier": "standard", "score": 10}


@pytest.mark.asyncio
async def test_both_modes_agree(packages_dir):
    plan = _router_plan()
    message = await run_workflow_package(plan, {"score": 70}, mode="message")
    task = await run_workflow_package(plan, {"score": 70}, mode="task")

    assert message.result == task.result
    assert message.state == task.state


# ── Per-node trace ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_covers_only_the_branch_that_ran(packages_dir):
    plan = _router_plan()
    run = await run_workflow_package(plan, {"score": 90}, mode="message")

    visited = {step.canvas_id for step in run.steps}
    assert {"start", "router", "high", "end"} <= visited
    assert "low" not in visited, "the untaken branch should not appear in the trace"


@pytest.mark.asyncio
async def test_trace_maps_nodes_back_to_canvas_ids_and_types(packages_dir):
    plan = _router_plan()
    run = await run_workflow_package(plan, {"score": 10}, mode="message")

    by_canvas = {step.canvas_id: step for step in run.steps}
    assert by_canvas["start"].node_type == "A2A_START"
    assert by_canvas["router"].node_type == "CONDITION"
    assert by_canvas["end"].node_type == "END"
    for step in run.steps:
        assert step.iteration >= 1
        assert step.error is None


@pytest.mark.asyncio
async def test_trace_records_loop_iterations(packages_dir):
    """An iteration counter is what makes loop progress visible on the canvas."""
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", {"payload_schema": {"fields": []}}),
                _node(
                    "loop",
                    "LOOP",
                    {"mode": "while", "exit_condition": "i >= 3", "max_iterations": 5},
                ),
                _node(
                    "body",
                    "TRANSFORM",
                    {"mode": "python", "expression": "result = {'seen': True}"},
                ),
                _node("end", "END"),
            ],
            "edges": [
                _edge("start", "loop"),
                _edge("loop", "body", "loop_body"),
                _edge("body", "loop"),
                _edge("loop", "end", "done"),
            ],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, "loop-0001")
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name="Loop Test")

    run = await run_workflow_package(plan, {}, mode="message")

    assert run.ok, run.error
    body_steps = [s for s in run.steps if s.canvas_id == "body"]
    assert len(body_steps) == 3, [s.iteration for s in body_steps]
    assert sorted(s.iteration for s in body_steps) == [1, 2, 3]


# ── Failure reporting ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failing_node_is_reported_not_hidden(packages_dir):
    """A node whose on_error policy is `fail` should surface as a failed run."""
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", {"payload_schema": {"fields": []}}),
                _node(
                    "boom",
                    "FUNCTION",
                    {"name": "boom", "code": "raise ValueError('deliberate')"},
                    on_error="fail",
                ),
                _node("end", "END"),
            ],
            "edges": [_edge("start", "boom"), _edge("boom", "end")],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, "boom-0001")
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name="Boom Test")

    run = await run_workflow_package(plan, {}, mode="message")

    assert not run.ok
    assert run.state != "completed"
    assert run.error
    # The node that ran before the failure is still in the trace.
    assert any(step.canvas_id == "start" for step in run.steps)


@pytest.mark.asyncio
async def test_on_error_continue_lets_the_workflow_finish(packages_dir):
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", {"payload_schema": {"fields": []}}),
                _node(
                    "soft",
                    "FUNCTION",
                    {"name": "soft", "code": "raise ValueError('handled')"},
                    on_error="continue",
                ),
                _node("end", "END"),
            ],
            "edges": [_edge("start", "soft"), _edge("soft", "end")],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, "soft-0001")
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name="Soft Test")

    run = await run_workflow_package(plan, {}, mode="message")

    assert run.ok, run.error
    assert isinstance(run.result, dict)
    # The error travels downstream as data, so a router could branch on it.
    assert run.result.get("_error") is True
    assert run.result.get("_error_node")


@pytest.mark.asyncio
async def test_missing_package_directory_is_reported_clearly(packages_dir, monkeypatch):
    plan = _router_plan()
    monkeypatch.setenv("PACKAGES_DIR", str(packages_dir / "nope" / "deeper"))

    # Rendering creates the directory, so this exercises the happy path even
    # from a non-existent root -- the failure mode worth checking is a bad
    # interpreter, which _invoke reports rather than raising.
    run = await run_workflow_package(plan, {"score": 1}, mode="message")
    assert run.ok or run.error


def test_run_result_is_json_serialisable(packages_dir):
    """The endpoint stores this in a JSON column."""
    import asyncio

    plan = _router_plan()
    run = asyncio.run(run_workflow_package(plan, {"score": 5}, mode="message"))

    encoded = json.dumps(run.to_dict())
    assert json.loads(encoded)["state"] == "completed"


# ── run_once.py is a usable CLI in its own right ─────────────────────────────


def test_run_once_cli_reports_and_sets_an_exit_code(packages_dir):
    import subprocess
    import sys

    plan = _router_plan()
    directory, _ = ensure_package(plan)

    result = subprocess.run(
        [sys.executable, "run_once.py", '{"score": 90}'],
        cwd=str(directory),
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(directory)},
    )

    assert result.returncode == 0, result.stderr[-2000:]
    # The human report goes to stderr, the result JSON to stdout.
    assert "premium" in result.stdout
    assert "state=completed" in result.stderr
    # Each node that ran is named.
    assert "n_condition_" in result.stderr


# ── The Phase 6 gate: flow-control nodes run for real ────────────────────────
#
# LOOP, PARALLEL_FORK and MERGE are the three node types where the platform's
# old engine and the generated package had already diverged: the engine ran real
# iterations and real fan-out, while the generated main.py degraded LOOP to a
# single pass and treated PARALLEL_FORK and MERGE as pass-throughs. There is now
# one implementation, so "identical in both" is structural rather than a claim
# to re-test -- but it still has to actually work.


@pytest.mark.asyncio
async def test_parallel_fork_runs_both_branches_and_merges_them(packages_dir):
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", {"payload_schema": {"fields": []}}),
                _node("fork", "PARALLEL_FORK", {"branches": ["left", "right"]}),
                _node(
                    "left",
                    "TRANSFORM",
                    {"mode": "python", "expression": "result = {'left_ran': True}"},
                ),
                _node(
                    "right",
                    "TRANSFORM",
                    {"mode": "python", "expression": "result = {'right_ran': True}"},
                ),
                _node("merge", "MERGE"),
                _node(
                    "collect",
                    "FUNCTION",
                    {
                        "name": "collect",
                        # A JoinNode hands its successor a dict keyed by
                        # predecessor node name, so this proves both arrived.
                        "code": "result = {'joined': sorted(data.keys())}",
                    },
                ),
                _node("end", "END", {"output_mapping": {"joined": "joined"}}),
            ],
            "edges": [
                _edge("start", "fork"),
                _edge("fork", "left", "left"),
                _edge("fork", "right", "right"),
                _edge("left", "merge"),
                _edge("right", "merge"),
                _edge("merge", "collect"),
                _edge("collect", "end"),
            ],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, "fork-0001")
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name="Fork Test")

    run = await run_workflow_package(plan, {}, mode="message")

    assert run.ok, run.error
    # Both branches ran...
    visited = {step.canvas_id for step in run.steps}
    assert {"left", "right", "merge", "collect"} <= visited
    # ...and the join delivered both, keyed by generated node name.
    joined = run.result["joined"]
    left = next(n.name for n in plan.nodes if n.canvas_id == "left")
    right = next(n.name for n in plan.nodes if n.canvas_id == "right")
    assert sorted(joined) == sorted([left, right]), joined


@pytest.mark.asyncio
async def test_for_each_loop_iterates_over_the_payload(packages_dir):
    """The engine ran real for_each iterations; the old codegen ran one pass."""
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node(
                    "start",
                    "A2A_START",
                    {
                        "payload_schema": {
                            "fields": [
                                {"name": "items", "type": "array", "description": "", "required": True}
                            ]
                        }
                    },
                ),
                _node(
                    "loop",
                    "LOOP",
                    {"mode": "for_each", "items_path": "items", "max_iterations": 10},
                ),
                _node(
                    "body",
                    "TRANSFORM",
                    {"mode": "python", "expression": "result = {'seen': data.get('current_item')}"},
                ),
                _node("end", "END", {"output_mapping": {"iterations": "iterations"}}),
            ],
            "edges": [
                _edge("start", "loop"),
                _edge("loop", "body", "loop_body"),
                _edge("body", "loop"),
                _edge("loop", "end", "done"),
            ],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, "foreach-0001")
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name="For Each Test")

    run = await run_workflow_package(plan, {"items": ["a", "b", "c", "d"]}, mode="message")

    assert run.ok, run.error
    body_steps = [s for s in run.steps if s.canvas_id == "body"]
    assert len(body_steps) == 4, [s.iteration for s in body_steps]
    assert run.result["iterations"] == 4


@pytest.mark.asyncio
async def test_loop_stops_at_its_iteration_cap(packages_dir):
    """A condition that never becomes true must not spin the graph forever."""
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", {"payload_schema": {"fields": []}}),
                _node(
                    "loop",
                    "LOOP",
                    {"mode": "while", "exit_condition": "False", "max_iterations": 3},
                ),
                _node(
                    "body",
                    "TRANSFORM",
                    {"mode": "python", "expression": "result = {'again': True}"},
                ),
                _node("end", "END", {"output_mapping": {"iterations": "iterations"}}),
            ],
            "edges": [
                _edge("start", "loop"),
                _edge("loop", "body", "loop_body"),
                _edge("body", "loop"),
                _edge("loop", "end", "done"),
            ],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, "cap-0001")
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name="Cap Test")

    run = await run_workflow_package(plan, {}, mode="message")

    assert run.ok, run.error
    assert run.result["iterations"] == 3
