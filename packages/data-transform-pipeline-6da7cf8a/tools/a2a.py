"""Calling other A2A agents -- as an agent-node tool or as a graph node.

Speaks the same JSON-RPC surface this package serves, so a generated workflow can
delegate to another generated workflow. Both call styles are supported:

  * `send_message`  -- blocking `message/send`, returns the final result
  * `submit_task` / `get_task` -- non-blocking `message/send` plus `tasks/get`
    polling, for a remote agent that takes longer than one request should

Result extraction handles both shapes a remote can answer with: a Task carrying
`artifacts`, or a Message returned directly.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from tools.signature import safe_tool_name

logger = logging.getLogger("workflow.tools.a2a")

_DEFAULT_TIMEOUT = 60
_TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})


def _headers(auth_token: str = "") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return headers


def _rpc_body(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }


def _message(text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        "messageId": str(uuid.uuid4()),
        "kind": "message",
    }


def extract_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the answer out of an A2A response.

    Order matters: a completed task's `artifacts` hold the authoritative result,
    then the status message, then the last agent turn in `history`.
    """
    if "error" in payload and payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        return {"error": message or "Remote agent returned an error."}

    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return {"result": result}

    # A Message reply rather than a Task.
    if result.get("kind") == "message":
        text = _first_text(result.get("parts", []))
        if text is not None:
            return {"result": text}

    for artifact in result.get("artifacts") or []:
        text = _first_text(artifact.get("parts", []))
        if text is not None:
            return {"result": text}

    status = result.get("status") or {}
    status_message = status.get("message") or {}
    text = _first_text(status_message.get("parts", []))
    if text is not None:
        return {"result": text}

    for turn in reversed(result.get("history") or []):
        if turn.get("role") == "agent":
            text = _first_text(turn.get("parts", []))
            if text is not None:
                return {"result": text}

    return result


def _first_text(parts: list[dict[str, Any]]) -> str | None:
    for part in parts or []:
        if part.get("kind") == "text" and part.get("text") is not None:
            return part["text"]
    return None


async def send_message(
    endpoint: str,
    message: str,
    auth_token: str = "",
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Blocking `message/send` -- the remote runs to completion before replying."""
    if not endpoint:
        return {"error": "No endpoint configured for this remote agent."}

    url = endpoint.rstrip("/") + "/"
    body = _rpc_body(
        "message/send",
        {"message": _message(message), "configuration": {"blocking": True}},
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=body, headers=_headers(auth_token))
    return extract_result(response.json())


async def submit_task(
    endpoint: str,
    message: str,
    auth_token: str = "",
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Non-blocking `message/send` -- returns `{task_id, state}` to poll."""
    if not endpoint:
        return {"error": "No endpoint configured for this remote agent."}

    url = endpoint.rstrip("/") + "/"
    body = _rpc_body(
        "message/send",
        {"message": _message(message), "configuration": {"blocking": False}},
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=body, headers=_headers(auth_token))

    payload = response.json()
    task = payload.get("result") or {}
    return {
        "task_id": task.get("id"),
        "state": (task.get("status") or {}).get("state"),
        "context_id": task.get("contextId"),
    }


async def get_task(
    endpoint: str,
    task_id: str,
    auth_token: str = "",
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """`tasks/get` -- one poll. Returns state plus the result once terminal."""
    url = endpoint.rstrip("/") + "/"
    body = _rpc_body("tasks/get", {"id": task_id})
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=body, headers=_headers(auth_token))

    payload = response.json()
    task = payload.get("result") or {}
    state = (task.get("status") or {}).get("state")
    out: dict[str, Any] = {"task_id": task_id, "state": state}
    if state in _TERMINAL_STATES:
        out.update(extract_result(payload))
    return out


def build_tools(agents: list[dict[str, Any]]) -> list:
    """Wrap each configured remote agent as a delegation tool for an agent node."""
    from google.adk.tools import FunctionTool

    tools: list = []
    for agent in agents or []:
        name = agent.get("name") or "remote_agent"
        endpoint = agent.get("endpoint") or agent.get("url") or ""
        token = agent.get("auth_token", "")
        description = agent.get("description") or f"Delegate a request to '{name}'."

        def _make(endpoint=endpoint, token=token, name=name, description=description):
            async def call_remote_agent(message: str) -> dict:
                """Send a natural-language request to a remote A2A agent."""
                try:
                    return await send_message(endpoint, message, token)
                except Exception as exc:  # noqa: BLE001 - reported to the model
                    logger.warning("A2A call to %s failed: %s", name, exc)
                    return {"error": str(exc)}

            call_remote_agent.__name__ = safe_tool_name("a2a", name)
            call_remote_agent.__doc__ = description
            return FunctionTool(call_remote_agent)

        tools.append(_make())
    return tools
