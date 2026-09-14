"""WAIT: pausing a run for a fixed time.

ADK has no delay node — searching the package for sleep / delay / schedule /
timer finds only retry backoff, polling loops and fixed internal delays, and
`workflow/_trigger.py` is a data model with no timing in it. So this node is
`asyncio.sleep`, and the tests here cover the two things that make that choice
safe rather than naive: it does not stall sibling branches, and ADK's node
timeout is derived from the wait instead of the canvas policy.

Durations are kept to a couple of seconds so the suite stays quick.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import warnings

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.compiler.semantic_validator import validate_semantics
from app.nodes.registry import NODE_REGISTRY
from app.nodes.tasks.wait import (
    BLOCKING_COMFORT_SECONDS,
    MAX_WAIT_SECONDS,
    node_timeout_for,
    wait_seconds,
)
from app.packaging.render import render_package
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)


def _node(node_id, node_type, config=None, timeout=120):
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": node_id, "description": ""},
        "config": config or {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": timeout, "retry": {"max_attempts": 1},
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
        {"name": "x", "type": "integer", "description": "", "required": False},
    ]},
}


def _canvas(wait_config: dict, *, policy_timeout: int = 120) -> dict:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", _START),
            _node("pause", "WAIT", wait_config, timeout=policy_timeout),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [_edge("start", "pause"), _edge("pause", "end")],
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


# ── Duration arithmetic ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"duration": 5, "unit": "seconds"}, 5.0),
        ({"duration": 3, "unit": "minutes"}, 180.0),
        ({"duration": 0.5, "unit": "minutes"}, 30.0),
        ({}, 5.0),                                   # the declared default
        ({"duration": -4, "unit": "seconds"}, 0.0),  # never negative
        ({"duration": "nonsense"}, 0.0),
    ],
)
def test_wait_seconds(config, expected):
    assert wait_seconds(config) == expected


def test_wait_has_one_way_out():
    definition = NODE_REGISTRY["WAIT"]
    assert definition.output_handles == ["output"]
    assert not definition.uses_named_routes


# ── The timeout derivation, which is what makes it work ──────────────────────


def test_the_node_timeout_covers_the_wait():
    """ADK kills a node that outlives its timeout.

    Verified directly against ADK: a node sleeping 3s under `timeout=1` raises
    `NodeTimeoutError: Node 'slow' timed out after 1.0 seconds`. Nodes inherit
    `policies.timeout_seconds` — 120s in the seeds — so a longer wait would die
    part way through, blaming a timeout rather than the wait.
    """
    assert node_timeout_for({"duration": 3, "unit": "minutes"}) > 180


def test_the_generated_node_overrides_the_canvas_policy(tmp_path):
    """The canvas says 5s; the wait is 8s. The wait must win."""
    destination = _render(
        _canvas({"duration": 8, "unit": "seconds"}, policy_timeout=5),
        "wait-timeout-0001", tmp_path,
    )
    source = next((destination / "nodes").glob("*wait*.py")).read_text()

    assert "WAIT_SECONDS = 8.0" in source
    assert "timeout=38" in source, source


def test_a_wait_longer_than_the_canvas_policy_still_completes(tmp_path):
    """The regression this guards: it used to be killed at the policy timeout.

    Measured before the override: "Node 'n_wait_2' timed out after 5.0 seconds."
    """
    destination = _render(
        _canvas({"duration": 3, "unit": "seconds"}, policy_timeout=1),
        "wait-survive-0001", tmp_path,
    )
    envelope = _run(destination, {"x": 1})

    assert envelope["state"] == "completed", envelope.get("error")
    assert envelope["result"]["waited_seconds"] == 3


# ── Behaviour ────────────────────────────────────────────────────────────────


def test_it_actually_waits_and_passes_the_payload_through(tmp_path):
    destination = _render(_canvas({"duration": 2, "unit": "seconds"}),
                          "wait-runs-0001", tmp_path)

    started = time.monotonic()
    envelope = _run(destination, {"x": 7})
    elapsed = time.monotonic() - started

    assert envelope["state"] == "completed"
    assert elapsed >= 2, f"returned in {elapsed:.1f}s, so it did not wait"
    # The payload is untouched; only the configured wait is added.
    assert envelope["result"]["x"] == 7
    assert envelope["result"]["waited_seconds"] == 2


def test_a_wait_does_not_hold_up_a_parallel_branch(tmp_path):
    """`asyncio.sleep` yields the loop, so siblings keep running.

    Asserted from the trace timestamps rather than wall clock, which would be
    swamped by interpreter start-up: a 1s and a 3s wait on separate branches
    complete ~2s apart and the whole graph finishes ~3s in, not ~4s.
    """
    canvas = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", _START),
            # PARALLEL_FORK names its branches as plain strings; CONDITION
            # uses {name, expression} objects. Different shapes, same idea.
            _node("fork", "PARALLEL_FORK", {"branches": ["left", "right"]}),
            _node("waitA", "WAIT", {"duration": 1, "unit": "seconds"}),
            _node("waitB", "WAIT", {"duration": 3, "unit": "seconds"}),
            _node("join", "MERGE", {}),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [
            _edge("start", "fork"),
            _edge("fork", "waitA", "left"),
            _edge("fork", "waitB", "right"),
            _edge("waitA", "join"),
            _edge("waitB", "join"),
            _edge("join", "end"),
        ],
    }
    destination = _render(canvas, "wait-parallel-0001", tmp_path)

    envelope = _run(destination, {"x": 1})
    assert envelope["state"] == "completed", envelope.get("error")

    trace = envelope["trace"]
    first = min(step["at"] for step in trace)
    waits = sorted(
        (step for step in trace if "wait" in step["node"]), key=lambda s: s["at"]
    )
    assert len(waits) == 2, [s["node"] for s in trace]

    # Run back to back, the second would land ~4s in and 3s after the first.
    gap = waits[1]["at"] - waits[0]["at"]
    total = max(step["at"] for step in trace) - first
    assert gap < 2.5, f"the waits are {gap:.2f}s apart, so they ran in sequence"
    assert total < 3.6, f"the graph took {total:.2f}s, about the sum of the waits"


def test_the_output_is_reproducible(tmp_path):
    """The payload carries the configured wait, not the measured one.

    A measured duration differs by a millisecond or two between runs, and the
    generated suite asserts that a blocking call and a polled call return the
    same result — so a timing value in the payload failed
    `test_both_modes_agree_on_the_result` for every workflow with a WAIT.
    """
    destination = _render(_canvas({"duration": 1, "unit": "seconds"}),
                          "wait-repeat-0001", tmp_path)

    first = _run(destination, {"x": 1})["result"]
    second = _run(destination, {"x": 1})["result"]

    assert first == second
    assert first["waited_seconds"] == 1


# ── Validation ───────────────────────────────────────────────────────────────


def test_a_wait_of_zero_is_rejected():
    errors, _warnings = validate_semantics(
        CanvasPayload.model_validate(_canvas({"duration": 0, "unit": "seconds"}))
    )
    assert any("would not wait at all" in e for e in errors), errors


def test_a_wait_over_the_limit_is_rejected():
    """Past an hour it is a scheduling problem, not a pause in a run."""
    over = (MAX_WAIT_SECONDS // 60) + 1
    errors, _warnings = validate_semantics(
        CanvasPayload.model_validate(_canvas({"duration": over, "unit": "minutes"}))
    )
    assert any("belongs outside the workflow" in e for e in errors), errors


def test_a_long_wait_warns_about_blocking_callers():
    seconds = BLOCKING_COMFORT_SECONDS + 30
    errors, warnings_out = validate_semantics(
        CanvasPayload.model_validate(_canvas({"duration": seconds, "unit": "seconds"}))
    )
    assert errors == []
    assert any("task mode" in w for w in warnings_out), warnings_out


def test_a_short_wait_says_nothing():
    _errors, warnings_out = validate_semantics(
        CanvasPayload.model_validate(_canvas({"duration": 5, "unit": "seconds"}))
    )
    assert not any("WAIT" in w for w in warnings_out), warnings_out


# ── The ADK facts this node is built on ──────────────────────────────────────


def test_adk_has_no_delay_node():
    """Pinned so the day ADK grows one, this fails and we reconsider."""
    import google.adk.workflow as workflow

    exported = {name.lower() for name in dir(workflow) if not name.startswith("_")}
    for word in ("wait", "delay", "sleep", "timer", "schedule"):
        assert not any(word in name for name in exported), (word, sorted(exported))


def test_adk_kills_a_node_that_outlives_its_timeout():
    """The reason the timeout is derived from the wait."""
    import asyncio

    from google.adk.agents.context import Context  # noqa: F401
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import START, Workflow, node
    from google.adk.workflow._errors import NodeTimeoutError
    from google.adk import Runner
    from google.genai import types

    @node(name="slow", timeout=1)
    async def slow(ctx, node_input=None):
        await asyncio.sleep(3)
        return None

    workflow = Workflow(name="pinned", description="sleeps past its timeout",
                        edges=[(START, slow)])

    async def run():
        service = InMemorySessionService()
        runner = Runner(app_name="t", node=workflow, session_service=service)
        await service.create_session(app_name="t", user_id="u", session_id="s")
        async for _ in runner.run_async(
            user_id="u", session_id="s",
            new_message=types.Content(role="user", parts=[types.Part(text="{}")]),
        ):
            pass

    with pytest.raises((NodeTimeoutError, Exception), match="timed out"):
        asyncio.run(run())
