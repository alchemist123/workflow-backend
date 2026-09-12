#!/usr/bin/env python
"""Run this workflow once with a payload, and report what each node did.

Useful on its own for debugging a package from the command line, and it is what
the platform's Test button drives — so what you test is the package that ships,
byte for byte, rather than a separate execution path.

    python run_once.py '{"text": "hello"}'
    python run_once.py '{"text": "hello"}' --mode task
    python run_once.py '{"text": "hello"}' --json

The workflow is driven over its own A2A surface (`message/send`, and
`tasks/get` in task mode), so the request path is identical to a real caller's.
Per-node detail comes from a Runner whose `run_async` tees every ADK event —
`to_a2a` accepts a Runner, which is the supported way in.

`--json` prints a single machine-readable object on stdout, which is what the
platform parses; without it the output is formatted for a person.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
import warnings
from typing import Any

# ADK's A2A support is flagged experimental and warns on every call. The warning
# is about ADK, not this workflow, and it would bury the report below.
warnings.filterwarnings("ignore", category=UserWarning)

TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})


def _jsonable(value: Any) -> Any:
    """Coerce a node output into something json can encode."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _jsonable(dump())
        except Exception:  # noqa: BLE001 - fall through to the string form
            pass
    return str(value)


def _build_traced_app(trace: list[dict]):
    """The package's own app, with a Runner that records every node event."""
    from google.adk import Runner
    from google.adk.sessions import InMemorySessionService

    from agent import root_agent
    from core.a2a_app import build_app
    from core.config import settings
    from nodes.registry import canvas_id, iteration_from_path, node_name_from_path

    runner = Runner(
        app_name=settings.AGENT_NAME or "workflow",
        node=root_agent,
        session_service=InMemorySessionService(),
    )

    original = runner.run_async

    async def traced(**kwargs):
        # Runner is a plain class, so replacing the bound method on the instance
        # is enough -- no subclassing and no ADK internals touched.
        async for event in original(**kwargs):
            path = event.node_info.path if event.node_info else ""
            name = node_name_from_path(path)
            if name:
                trace.append(
                    {
                        "node": name,
                        "canvas_id": canvas_id(name),
                        "iteration": iteration_from_path(path),
                        "path": path,
                        "output": _jsonable(event.output),
                        "error": event.error_message,
                        "at": time.time(),
                    }
                )
            yield event

    runner.run_async = traced
    return build_app(root_agent, runner=runner)


def _rpc(client, method: str, params: dict) -> dict:
    response = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"{method} returned HTTP {response.status_code}: {response.text[:400]}")
    return response.json()


def _message(payload: Any) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        "messageId": str(uuid.uuid4()),
        "kind": "message",
    }


def _artifact_text(task: dict) -> str | None:
    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("kind") == "text":
                return part.get("text")
    return None


def _auth_headers() -> dict[str, str]:
    from core.config import settings

    if settings.A2A_AUTH_TOKEN:
        return {"Authorization": f"Bearer {settings.A2A_AUTH_TOKEN}"}
    return {}


def run(payload: Any, mode: str, timeout: float) -> dict:
    """Drive the workflow once. Never raises: failures come back in the envelope."""
    from starlette.testclient import TestClient

    trace: list[dict] = []
    started = time.time()

    try:
        app = _build_traced_app(trace)
    except Exception as exc:  # noqa: BLE001 - report, do not crash the caller
        return {
            "ok": False,
            "state": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "stage": "startup",
            "trace": trace,
            "duration_ms": int((time.time() - started) * 1000),
        }

    try:
        with TestClient(app, headers=_auth_headers()) as client:
            blocking = mode != "task"
            response = _rpc(
                client,
                "message/send",
                {"message": _message(payload), "configuration": {"blocking": blocking}},
            )
            if response.get("error"):
                raise RuntimeError(response["error"].get("message") or str(response["error"]))

            task = response["result"]
            task_id = task.get("id")
            polls = 0

            if not blocking:
                deadline = time.time() + timeout
                while task["status"]["state"] not in TERMINAL_STATES:
                    if time.time() > deadline:
                        break
                    time.sleep(0.05)
                    polls += 1
                    polled = _rpc(client, "tasks/get", {"id": task_id})
                    if polled.get("error"):
                        raise RuntimeError(
                            polled["error"].get("message") or str(polled["error"])
                        )
                    task = polled["result"]

            state = task.get("status", {}).get("state")
            text = _artifact_text(task)
            result: Any = None
            if text is not None:
                try:
                    result = json.loads(text)
                except (ValueError, TypeError):
                    result = text

            status_message = (task.get("status") or {}).get("message") or {}
            failure = None
            if state != "completed":
                failure = _first_text(status_message.get("parts") or []) or (
                    f"task ended in state {state!r}"
                )

            return {
                "ok": state == "completed",
                "state": state,
                "task_id": task_id,
                "context_id": task.get("contextId"),
                "mode": "task" if not blocking else "message",
                "polls": polls,
                "result": result,
                "artifact_text": text,
                "error": failure,
                "trace": trace,
                "duration_ms": int((time.time() - started) * 1000),
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "state": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "stage": "invoke",
            "trace": trace,
            "duration_ms": int((time.time() - started) * 1000),
        }


def _first_text(parts: list[dict]) -> str | None:
    for part in parts:
        if part.get("kind") == "text" and part.get("text"):
            return part["text"]
    return None


def _print_human(envelope: dict) -> None:
    from nodes.registry import NODE_TITLES, NODE_TYPES

    state = envelope.get("state")
    mark = "OK" if envelope.get("ok") else "FAILED"
    print(f"{mark}  state={state}  {envelope.get('duration_ms')}ms", file=sys.stderr)
    if envelope.get("task_id"):
        print(f"task    {envelope['task_id']}", file=sys.stderr)
    if envelope.get("polls"):
        print(f"polls   {envelope['polls']}", file=sys.stderr)

    if envelope.get("trace"):
        print("\nnodes:", file=sys.stderr)
        for step in envelope["trace"]:
            name = step["node"]
            label = NODE_TITLES.get(name) or ""
            suffix = f"  ({label})" if label else ""
            iteration = f" #{step['iteration']}" if step["iteration"] > 1 else ""
            marker = "x" if step.get("error") else "-"
            print(
                f"  {marker} {name}{iteration}  [{NODE_TYPES.get(name, '?')}]{suffix}",
                file=sys.stderr,
            )
            if step.get("error"):
                print(f"      error: {step['error']}", file=sys.stderr)

    if envelope.get("error"):
        print(f"\nerror: {envelope['error']}", file=sys.stderr)

    print("\nresult:", file=sys.stderr)
    print(json.dumps(envelope.get("result"), indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "payload",
        nargs="?",
        default="{}",
        help='JSON payload, e.g. \'{"text": "hello"}\'. Plain prose also works.',
    )
    parser.add_argument(
        "--mode",
        choices=["message", "task"],
        default="message",
        help="message: blocking message/send. task: submit, then poll tasks/get.",
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="seconds to poll in task mode"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one machine-readable object on stdout and nothing else",
    )
    args = parser.parse_args()

    try:
        payload: Any = json.loads(args.payload)
    except (ValueError, TypeError):
        # Not JSON: send it as prose, which the entry node accepts.
        payload = args.payload

    envelope = run(payload, args.mode, args.timeout)

    if args.json:
        print(json.dumps(envelope, default=str))
    else:
        _print_human(envelope)

    return 0 if envelope.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
