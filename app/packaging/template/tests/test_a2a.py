"""The served A2A surface: agent card, both invocation modes, and polling.

Runs against the real application built by `main.py` -- same routes, same agent
card, same executor a deployed container serves. No model and no network: the
example workflow is model-free on purpose so this suite is a usable gate in CI.
"""

from __future__ import annotations

import json
import time

import pytest

_TERMINAL = {"completed", "failed", "canceled", "rejected"}


def _artifact_text(task: dict) -> str:
    artifacts = task.get("artifacts") or []
    assert artifacts, f"task carried no artifacts: {task.get('status')}"
    parts = artifacts[0].get("parts") or []
    assert parts, "artifact carried no parts"
    return parts[0]["text"]


def _poll(client, task_id: str, attempts: int = 40) -> dict:
    """Poll `tasks/get` until the task is terminal."""
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
    # requirements.txt and tests/test_adk_contract.py in the platform repo.
    assert card["url"]
    assert card["preferredTransport"] == "JSONRPC"
    assert card["protocolVersion"] == "0.3.0"


def test_legacy_agent_card_path_also_works(a2a_client):
    """Older A2A clients look for /.well-known/agent.json."""
    assert a2a_client.get("/.well-known/agent.json").status_code == 200


def test_health_endpoint(a2a_client):
    """to_a2a does not provide one; core/a2a_app.py appends it."""
    response = a2a_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ── Message-based invocation ─────────────────────────────────────────────────


def test_blocking_message_send_returns_the_result(a2a_client):
    payload = {"text": "Just a few words here."}
    response = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message(payload),
            "configuration": {"blocking": True},
        },
    )

    task = response["result"]
    assert task["status"]["state"] == "completed"

    result = json.loads(_artifact_text(task))
    assert result["branch"] == "short"
    assert result["word_count"] == 5
    assert result["summary"] == "Just a few words here."


def test_blocking_send_routes_the_long_branch(a2a_client):
    text = " ".join(f"word{i}" for i in range(30)) + ". Second sentence here."
    response = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message({"text": text}),
            "configuration": {"blocking": True},
        },
    )

    result = json.loads(_artifact_text(response["result"]))
    assert result["branch"] == "long"
    assert result["word_count"] > 20


def test_plain_text_message_is_accepted(a2a_client):
    """A caller may send prose instead of a JSON payload."""
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
    assert task["status"]["state"] == "completed"
    result = json.loads(_artifact_text(task))
    assert result["summary"] == "hello there"


# ── Task-based invocation and polling ────────────────────────────────────────


def test_non_blocking_send_returns_a_task_id(a2a_client):
    response = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message({"text": "poll me"}),
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
            "message": a2a_client.message({"text": "Poll until done."}),
            "configuration": {"blocking": False},
        },
    )["result"]

    task = _poll(a2a_client, submitted["id"])

    assert task["status"]["state"] == "completed"
    assert task["id"] == submitted["id"]
    result = json.loads(_artifact_text(task))
    assert result["summary"] == "Poll until done."


def test_polling_an_unknown_task_id_errors(a2a_client):
    response = a2a_client.rpc("tasks/get", {"id": "00000000-0000-0000-0000-000000000000"})

    assert "error" in response
    assert response["error"]["message"]


def test_both_modes_agree_on_the_result(a2a_client):
    """Message-based and task-based invocation must not diverge."""
    payload = {"text": "One consistent answer."}

    blocking = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message(payload),
            "configuration": {"blocking": True},
        },
    )["result"]

    submitted = a2a_client.rpc(
        "message/send",
        {
            "message": a2a_client.message(payload),
            "configuration": {"blocking": False},
        },
    )["result"]
    polled = _poll(a2a_client, submitted["id"])

    assert json.loads(_artifact_text(blocking)) == json.loads(_artifact_text(polled))


# ── Debugging surface ────────────────────────────────────────────────────────


def test_graph_endpoint_reports_missing_graph_json(a2a_client):
    """The template ships no graph.json; the generator writes one per package."""
    response = a2a_client.get("/graph")
    assert response.status_code in {200, 404}
    if response.status_code == 200:
        assert "nodes" in response.json() or "edges" in response.json()
