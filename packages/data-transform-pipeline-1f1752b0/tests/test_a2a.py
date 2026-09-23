"""The served A2A surface: agent card, both invocation modes, and polling.

Generated for Data Transform Pipeline. Runs against the real application built
by `main.py` — same routes, same agent card, same executor a deployed container
serves.

These assertions are deliberately about the A2A protocol rather than this
workflow's output, so they hold whatever the canvas computes. Add assertions on
the result payload below if you want to pin the workflow's behaviour too.
"""

from __future__ import annotations

import json
import time

import pytest

_TERMINAL = {"completed", "failed", "canceled", "rejected"}

# The payload the entry node documents, from its PAYLOAD_SCHEMA.
EXAMPLE_PAYLOAD: dict = {'items': []}


def _artifact_text(task: dict) -> str:
    artifacts = task.get("artifacts") or []
    assert artifacts, (
        f"task carried no artifacts (state: {task.get('status', {}).get('state')}). "
        "The terminal node must emit content, not just output."
    )
    parts = artifacts[0].get("parts") or []
    assert parts, "artifact carried no parts"
    return parts[0]["text"]


def _poll(client, task_id: str, attempts: int = 60) -> dict:
    for _ in range(attempts):
        task = client.rpc("tasks/get", {"id": task_id})["result"]
        if task["status"]["state"] in _TERMINAL:
            return task
        time.sleep(0.05)
    pytest.fail(f"task {task_id} never reached a terminal state")


# ── Discovery ────────────────────────────────────────────────────────────────


def test_agent_card_is_served(a2a_client):
    response = a2a_client.get("/.well-known/agent-card.json")
    assert response.status_code == 200

    card = response.json()
    assert card["name"]
    assert card["version"]
    assert card["skills"], "an agent with no skills is not discoverable"
    # These three fields exist only on a2a-sdk 0.3.x; see the pin in
    # requirements.txt.
    assert card["url"]
    assert card["preferredTransport"] == "JSONRPC"
    assert card["protocolVersion"] == "0.3.0"


def test_agent_name_is_a_valid_identifier(a2a_client):
    """ADK validates a Workflow's name as a node name.

    agent.py passes AGENT_NAME straight to `Workflow(name=...)`, so a name with
    a space or a hyphen in .env would break the graph at import.
    """
    card = a2a_client.get("/.well-known/agent-card.json").json()
    assert card["name"].isidentifier(), card["name"]


def test_legacy_agent_card_path_also_works(a2a_client):
    """Older A2A clients look for /.well-known/agent.json."""
    assert a2a_client.get("/.well-known/agent.json").status_code == 200


def test_health_endpoint(a2a_client):
    """to_a2a does not provide one; core/a2a_app.py appends it."""
    response = a2a_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_graph_endpoint_serves_the_compiled_plan(a2a_client):
    response = a2a_client.get("/graph")
    assert response.status_code == 200

    graph = response.json()
    assert graph["entry_node"] == "a2a_start"
    assert graph["terminal_node"] == "n_end_1"
    assert len(graph["nodes"]) == 4


# ── Message-based invocation ─────────────────────────────────────────────────


def test_blocking_message_send_completes(a2a_client):
    response = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message(EXAMPLE_PAYLOAD),
            "configuration": {"blocking": True},
        },
    )

    task = response["result"]
    assert task["status"]["state"] == "completed", task["status"]

    # The workflow's result is JSON text in the first artifact.
    result = json.loads(_artifact_text(task))
    assert isinstance(result, dict)


def test_plain_text_message_is_accepted(a2a_client):
    """A caller may send prose instead of a JSON payload.

    core/state.py::coerce_input wraps it as {"text": ...}.
    """
    response = a2a_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello there"}],
                    "messageId": "m-prose",
                    "kind": "message",
                },
                "configuration": {"blocking": True},
            },
        },
    )

    task = response.json()["result"]
    # The workflow may legitimately fail on an unexpected payload; what must not
    # happen is the task hanging at `working` forever.
    assert task["status"]["state"] in _TERMINAL, task["status"]


# ── Task-based invocation and polling ────────────────────────────────────────


def test_non_blocking_send_returns_a_task_id(a2a_client):
    response = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message(EXAMPLE_PAYLOAD),
            "configuration": {"blocking": False},
        },
    )

    task = response["result"]
    assert task["kind"] == "task"
    assert task["id"]
    assert task["status"]["state"] in {"submitted", "working", "completed"}


def test_task_can_be_polled_to_completion(a2a_client):
    submitted = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message(EXAMPLE_PAYLOAD),
            "configuration": {"blocking": False},
        },
    )["result"]

    task = _poll(a2a_client, submitted["id"])

    assert task["id"] == submitted["id"]
    assert task["status"]["state"] == "completed", task["status"]
    assert _artifact_text(task)


def test_polling_an_unknown_task_id_errors(a2a_client):
    response = a2a_client.rpc(
        "tasks/get", {"id": "00000000-0000-0000-0000-000000000000"}
    )

    assert "error" in response
    assert response["error"]["message"]


def test_both_modes_agree_on_the_result(a2a_client):
    """Message-based and task-based invocation must not diverge."""
    blocking = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message(EXAMPLE_PAYLOAD),
            "configuration": {"blocking": True},
        },
    )["result"]

    submitted = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message(EXAMPLE_PAYLOAD),
            "configuration": {"blocking": False},
        },
    )["result"]
    polled = _poll(a2a_client, submitted["id"])

    assert json.loads(_artifact_text(blocking)) == json.loads(_artifact_text(polled))
