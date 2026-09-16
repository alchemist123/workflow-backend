"""Live node-by-node progress, from the running package to the canvas.

The obvious channel would be A2A, and it is the wrong one. Measured against
a2a-sdk 0.3.26 and google-adk 2.8.0:

* the package's agent card declares `streaming=False`, and the SDK refuses
  `message/stream` outright when it does ("Streaming is not supported by the
  agent");
* turning it on works, but a three-node graph produced three frames —
  `submitted`, `working`, `working` — because ADK's converter only emits an
  A2A event for an ADK event carrying `content`, and only the terminal node
  emits content here;
* and those frames carry `adk_app_name` / `adk_user_id` / `adk_session_id` /
  `adk_invocation_id` and no node path, so nothing on the wire says which node
  is running.

So the package reports its own steps on stderr instead, one JSON line per node,
and the backend fans them out over SSE. The tests here cover that chain end to
end, plus the A2A facts above so the day ADK changes them, this notices.
"""

from __future__ import annotations

import asyncio
import json
import warnings

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.packaging.render import render_package
from app.runtime import progress
from app.runtime.package_runner import PROGRESS_PREFIX, run_workflow_package
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


_CANVAS = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "nodes": [
        _node("start", "A2A_START", {
            "input_mode": "json", "state_key": "wf",
            "payload_schema": {"fields": [
                {"name": "x", "type": "integer", "description": "", "required": True},
            ]},
        }),
        _node("one", "TRANSFORM", {"mode": "python",
                                   "expression": "result = {'a': 1}"}),
        _node("two", "TRANSFORM", {"mode": "python",
                                   "expression": "result = {'b': 2}"}),
        _node("end", "END", {"output_mapping": {}}),
    ],
    "edges": [_edge("start", "one"), _edge("one", "two"), _edge("two", "end")],
}


def _plan(name: str):
    errors, _warnings, ir = run_compiler(CanvasPayload.model_validate(_CANVAS), name)
    assert errors == [], errors
    return build_graph_plan(ir, workflow_name=name)


# ── The package reports its own steps ────────────────────────────────────────


def test_the_package_ships_the_progress_channel(tmp_path):
    plan = _plan("live-ships-0001")
    render_package(plan, tmp_path / "pkg")

    assert (tmp_path / "pkg" / "core" / "progress.py").exists()
    base = (tmp_path / "pkg" / "nodes" / "base.py").read_text()
    assert "emit_progress(\"start\", name)" in base


def test_the_prefix_is_the_same_on_both_sides(tmp_path):
    """The backend splits lines on it, so a drift here loses every frame."""
    plan = _plan("live-prefix-0001")
    render_package(plan, tmp_path / "pkg")
    source = (tmp_path / "pkg" / "core" / "progress.py").read_text()

    assert f'PREFIX = "{PROGRESS_PREFIX}"' in source


def test_progress_is_off_unless_asked_for(tmp_path):
    """A plain `python run_once.py` stays readable; it is not a debug dump."""
    plan = _plan("live-optin-0001")
    render_package(plan, tmp_path / "pkg")
    source = (tmp_path / "pkg" / "core" / "progress.py").read_text()

    assert 'os.environ.get("WORKFLOW_PROGRESS") == "1"' in source


@pytest.mark.asyncio
async def test_every_node_is_reported_while_the_run_is_still_going(tmp_path,
                                                                   monkeypatch):
    monkeypatch.setenv("WORKFLOW_PACKAGES_DIR", str(tmp_path))
    monkeypatch.setenv("WORKFLOW_LOCAL_PACKAGES_DIR", str(tmp_path))

    steps: list[dict] = []
    run = await run_workflow_package(
        _plan("live-run-0001"), {"x": 1}, on_progress=steps.append
    )

    assert run.ok, run.error
    assert steps, "nothing was reported"

    # Every node reports both halves: a `start` from the node wrapper and an
    # `end` from the runner's event.
    started = [s["node"] for s in steps if s["e"] == "start"]
    ended = [s["node"] for s in steps if s["e"] == "end"]
    assert len(started) >= 4, started
    assert sorted(set(started)) == sorted(set(ended)), (started, ended)

    # In order, and each one named by its canvas node so the UI can find it.
    assert steps[0]["e"] == "start"
    assert all(s.get("canvas_id") for s in steps), steps
    assert [s["canvas_id"] for s in steps if s["e"] == "end"] == [
        "start", "one", "two", "end",
    ]


@pytest.mark.asyncio
async def test_a_start_and_its_end_name_the_same_canvas_node(tmp_path, monkeypatch):
    """Otherwise a node lights up twice: 'running' under one id, 'done' under another.

    A `start` comes from the node wrapper, which knows only the generated name;
    the runner fills in the canvas id so a subscriber sees one identity.
    """
    monkeypatch.setenv("WORKFLOW_PACKAGES_DIR", str(tmp_path))
    monkeypatch.setenv("WORKFLOW_LOCAL_PACKAGES_DIR", str(tmp_path))

    steps: list[dict] = []
    await run_workflow_package(_plan("live-ids-0001"), {"x": 1},
                               on_progress=steps.append)

    by_node: dict[str, set] = {}
    for step in steps:
        by_node.setdefault(step["node"], set()).add(step["canvas_id"])
    assert all(len(ids) == 1 for ids in by_node.values()), by_node


@pytest.mark.asyncio
async def test_a_failing_node_is_reported_as_it_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOW_PACKAGES_DIR", str(tmp_path))
    monkeypatch.setenv("WORKFLOW_LOCAL_PACKAGES_DIR", str(tmp_path))

    canvas = json.loads(json.dumps(_CANVAS))
    for node in canvas["nodes"]:
        if node["id"] == "two":
            node["config"]["expression"] = "raise ValueError('boom')"
    errors, _warnings, ir = run_compiler(CanvasPayload.model_validate(canvas),
                                         "live-fail-0001")
    assert errors == [], errors

    steps: list[dict] = []
    await run_workflow_package(
        build_graph_plan(ir, workflow_name="live-fail-0001"), {"x": 1},
        on_progress=steps.append,
    )

    failed = [s for s in steps if s["e"] == "error"]
    assert failed, steps
    assert failed[0]["canvas_id"] == "two"
    assert "boom" in failed[0]["error"]


# ── The broker fans one run out to several watchers ──────────────────────────


@pytest.mark.asyncio
async def test_two_subscribers_both_see_every_step():
    """One queue each: a shared one would let the first reader eat a step."""
    run_id = "exec-fanout"
    seen_a, seen_b = [], []

    async def watch(sink):
        async for step in progress.subscribe(run_id):
            sink.append(step)

    tasks = [asyncio.create_task(watch(seen_a)), asyncio.create_task(watch(seen_b))]
    await asyncio.sleep(0)  # let both subscribe before anything is published

    progress.publish(run_id, {"e": "start", "node": "a"})
    progress.publish(run_id, {"e": "end", "node": "a"})
    progress.finish(run_id)
    await asyncio.gather(*tasks)

    assert [s["e"] for s in seen_a] == ["start", "end", "finished"]
    assert seen_a == seen_b


@pytest.mark.asyncio
async def test_a_late_subscriber_catches_up_on_what_it_missed():
    """A canvas that subscribes a moment in must not show a blank run."""
    run_id = "exec-late"
    progress.publish(run_id, {"e": "start", "node": "a"})
    progress.publish(run_id, {"e": "end", "node": "a"})

    seen = []

    async def watch():
        async for step in progress.subscribe(run_id):
            seen.append(step)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    progress.finish(run_id)
    await task

    assert [s["e"] for s in seen] == ["start", "end", "finished"]


@pytest.mark.asyncio
async def test_a_subscriber_that_stopped_reading_cannot_stall_the_run():
    """Dropped-oldest, not backpressure: telemetry must never hold up a run."""
    run_id = "exec-slow"

    # Subscribe without consuming: one `asend(None)` registers the queue and
    # then parks on the first `await`.
    agen = progress.subscribe(run_id)
    task = asyncio.create_task(agen.asend(None))
    await asyncio.sleep(0)

    for i in range(progress.QUEUE_SIZE * 2):
        progress.publish(run_id, {"e": "end", "node": f"n{i}"})

    progress.finish(run_id)
    await task
    await agen.aclose()


@pytest.mark.asyncio
async def test_the_stream_ends_when_the_run_does():
    """Or a watching canvas sits on a spinner for a run that already ended."""
    run_id = "exec-ends"
    progress.finish(run_id)

    seen = [step async for step in progress.subscribe(run_id)]
    assert seen[-1]["e"] == "finished"


# ── Why this is not done over A2A ────────────────────────────────────────────


def test_the_packages_agent_card_does_not_promise_streaming():
    """A2A streaming is a task-level channel; it cannot report nodes.

    Left off deliberately rather than by omission: turning it on would make
    `message/stream` work and still say nothing about which node is running.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent
        / "app/packaging/template/core/agent_card.py"
    ).read_text()
    assert "streaming=False" in source


def test_adk_drops_the_node_path_when_it_converts_an_event_for_a2a():
    """The reason a node cannot be identified from the A2A stream.

    `_get_context_metadata` is the whole of what an A2A event carries back
    about its ADK origin, and `node_info` is not in it.
    """
    import inspect

    from google.adk.a2a.converters import event_converter

    source = inspect.getsource(event_converter._get_context_metadata)
    for key in ("app_name", "user_id", "session_id", "invocation_id"):
        assert key in source, key
    assert "node_info" not in source
    assert "node_path" not in source


# ── The stale-package trap ───────────────────────────────────────────────────


def test_a_package_records_which_template_built_it(tmp_path):
    from app.packaging.render import FINGERPRINT_FILE, template_fingerprint

    plan = _plan("live-stamp-0001")
    render_package(plan, tmp_path / "pkg")

    assert (tmp_path / "pkg" / FINGERPRINT_FILE).read_text() == template_fingerprint()


def test_a_package_from_an_older_template_is_re_rendered(tmp_path, monkeypatch):
    """Otherwise a change to the generated code reaches only new workflows.

    Packages are reused when the rendered graph still matches the plan, and a
    template edit leaves every plan identical — so this feature's first run
    against an existing package produced no frames at all and no error to
    explain it. Measured before the fingerprint: a completed run whose only
    event was `finished`.
    """
    from app.packaging.render import FINGERPRINT_FILE
    from app.runtime.package_runner import _template_matches

    plan = _plan("live-stale-0001")
    package = tmp_path / "pkg"
    render_package(plan, package)
    assert _template_matches(package)

    (package / FINGERPRINT_FILE).write_text("built by an older platform")
    assert not _template_matches(package)

    # A package from before packages were stamped has no file at all.
    (package / FINGERPRINT_FILE).unlink()
    assert not _template_matches(package)


@pytest.mark.asyncio
async def test_a_stream_for_a_run_nobody_is_tracking_ends_at_once():
    """A connection that will never produce a frame looks like a slow workflow.

    An EventSource reconnecting after the run is over, or a tab asking about a
    run from before a restart, would otherwise hold the canvas on a spinner
    forever.
    """
    assert not progress.is_live("never-started")


# ── Looking a task up after the fact ─────────────────────────────────────────


def test_a_re_render_keeps_the_packages_tasks(tmp_path):
    """`.runs/` is data; everything else in the package is generated code.

    Rendering used to delete the destination wholesale, which took the A2A task
    store and the ADK sessions with it — so a re-render silently made every
    outstanding human approval unanswerable and every task id a dead link. The
    fingerprint check made re-renders routine, which made this matter.
    """
    from app.packaging.render import STATE_DIR_NAME

    plan = _plan("live-state-0001")
    package = tmp_path / "pkg"
    render_package(plan, package)

    runs = package / STATE_DIR_NAME
    runs.mkdir(exist_ok=True)
    (runs / "tasks.db").write_bytes(b"pretend this is a task store")

    render_package(plan, package)

    assert (runs / "tasks.db").read_bytes() == b"pretend this is a task store"


def test_the_package_and_the_platform_agree_where_state_lives(tmp_path):
    from app.packaging.render import STATE_DIR_NAME

    plan = _plan("live-statedir-0001")
    render_package(plan, tmp_path / "pkg")
    source = (tmp_path / "pkg" / "run_once.py").read_text()

    assert f'STATE_DIR = Path(__file__).resolve().parent / "{STATE_DIR_NAME}"' in source


@pytest.mark.asyncio
async def test_a_task_can_be_looked_up_after_the_run_that_made_it(tmp_path, monkeypatch):
    """The point of showing a task id: it outlives the process that made it.

    Task mode hands a caller an id and returns. Looking it up later is a plain
    `tasks/get`, which answers because the store is on disk — a different
    process, a later session, or a caller outside the UI all reach the same
    task.
    """
    monkeypatch.setenv("WORKFLOW_PACKAGES_DIR", str(tmp_path))
    monkeypatch.setenv("WORKFLOW_LOCAL_PACKAGES_DIR", str(tmp_path))

    from app.runtime.package_runner import lookup_task

    plan = _plan("live-lookup-0001")
    run = await run_workflow_package(plan, {"x": 1}, mode="task")
    assert run.ok, run.error
    assert run.task_id, "task mode must report an id"

    found = await lookup_task(plan, task_id=run.task_id)

    assert found.state == "completed", found.error
    assert found.task_id == run.task_id
    assert found.result == run.result, "the same answer, read back rather than re-run"


@pytest.mark.asyncio
async def test_looking_up_an_unknown_task_is_an_answer_not_an_error(tmp_path,
                                                                    monkeypatch):
    """A task id from another workflow is the ordinary case, not a crash."""
    monkeypatch.setenv("WORKFLOW_PACKAGES_DIR", str(tmp_path))
    monkeypatch.setenv("WORKFLOW_LOCAL_PACKAGES_DIR", str(tmp_path))

    from app.runtime.package_runner import lookup_task

    plan = _plan("live-unknown-0001")
    await run_workflow_package(plan, {"x": 1})  # render the package first

    found = await lookup_task(plan, task_id="00000000-0000-0000-0000-000000000000")

    assert found.state == "not-found"
    assert found.ok is False


@pytest.mark.asyncio
async def test_a_lookup_runs_nothing(tmp_path, monkeypatch):
    """Read-only: it reads the task back, it does not run the workflow again.

    Asserted two ways, because "the answer looked right" would also be true of
    a re-run: the same task id comes back rather than a new one, and the
    package's lookup path only ever sends `tasks/get`.
    """
    monkeypatch.setenv("WORKFLOW_PACKAGES_DIR", str(tmp_path))
    monkeypatch.setenv("WORKFLOW_LOCAL_PACKAGES_DIR", str(tmp_path))

    from app.runtime.package_runner import ensure_package, lookup_task

    plan = _plan("live-readonly-0001")
    run = await run_workflow_package(plan, {"x": 1}, mode="task")
    assert run.task_id

    first = await lookup_task(plan, task_id=run.task_id)
    second = await lookup_task(plan, task_id=run.task_id)

    assert first.task_id == second.task_id == run.task_id, "a re-run would make a new task"
    assert first.result == second.result

    package_dir, _warnings = ensure_package(plan)
    source = (package_dir / "run_once.py").read_text()
    body = source[source.index("def get_task("):source.index("def resume(")]
    assert '_rpc(client, "tasks/get"' in body
    assert "message/send" not in body
