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
from pathlib import Path
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


# Where a parked run is kept between invocations. A workflow that stops on a
# HUMAN_APPROVAL node has to be answerable by a *later* `run_once.py`, and an
# in-memory task store dies with the process that made it -- so both the A2A
# task and the ADK session live in one SQLite file beside the package.
#
# An async driver is required: SQLAlchemy's asyncio extension rejects plain
# `sqlite:///`, and both stores are async.
STATE_DIR = Path(__file__).resolve().parent / ".runs"
STATE_DSN = f"sqlite+aiosqlite:///{STATE_DIR / 'tasks.db'}"


def _build_traced_app(trace: list[dict]):
    """The package's own app, with a Runner that records every node event."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from a2a.server.tasks import DatabaseTaskStore
    from google.adk import Runner
    from google.adk.sessions import DatabaseSessionService

    from agent import root_agent
    from core.a2a_app import build_app
    from core.config import settings
    from core.progress import emit as emit_progress
    from nodes.registry import canvas_id, iteration_from_path, node_name_from_path

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Both stores share one file: the A2A task records *that* the run is
    # parked, the ADK session records everything needed to carry on from there.
    # Persisting one without the other resumes into an empty workflow.
    runner = Runner(
        app_name=settings.AGENT_NAME or "workflow",
        node=root_agent,
        session_service=DatabaseSessionService(db_url=STATE_DSN),
    )
    task_store = DatabaseTaskStore(engine=create_async_engine(STATE_DSN))

    original = runner.run_async

    async def traced(**kwargs):
        # Runner is a plain class, so replacing the bound method on the instance
        # is enough -- no subclassing and no ADK internals touched.
        async for event in original(**kwargs):
            path = event.node_info.path if event.node_info else ""
            name = node_name_from_path(path)
            if name:
                step = {
                    "node": name,
                    "canvas_id": canvas_id(name),
                    "iteration": iteration_from_path(path),
                    "path": path,
                    "output": _jsonable(event.output),
                    "error": event.error_message,
                    "at": time.time(),
                }
                trace.append(step)
                # The same step, reported now rather than at the end, for a
                # caller painting the run as it happens.
                emit_progress(
                    "end",
                    name,
                    canvas_id=step["canvas_id"],
                    iteration=step["iteration"],
                    error=step["error"],
                )
            yield event

    runner.run_async = traced
    return build_app(root_agent, runner=runner, task_store=task_store)


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


def run(payload: Any, mode: str, timeout: float, answer_with: Any = None) -> dict:
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

            # `input-required` is a workflow parked on a HUMAN_APPROVAL node,
            # not a failure. Report what it is waiting for so the caller can
            # answer it -- see `--answer`.
            pending = _pending_input(task) if state == "input-required" else None

            # Answering has to happen on this same client: the task lives in
            # this app's task store, so a second app would not find it.
            asked = None
            if pending and answer_with is not None:
                asked = pending
                resumed = _rpc(
                    client,
                    "message/send",
                    {
                        "message": _answer_message(
                            task_id,
                            task.get("contextId"),
                            pending["interrupt_id"],
                            answer_with,
                        ),
                        "configuration": {"blocking": True},
                    },
                )
                if resumed.get("error"):
                    raise RuntimeError(
                        resumed["error"].get("message") or str(resumed["error"])
                    )
                task = resumed["result"]
                state = (task.get("status") or {}).get("state")
                text = _artifact_text(task)
                if text is not None:
                    try:
                        result = json.loads(text)
                    except (ValueError, TypeError):
                        result = text
                status_message = (task.get("status") or {}).get("message") or {}
                pending = _pending_input(task) if state == "input-required" else None

            failure = None
            if state != "completed" and not pending:
                failure = _first_text(status_message.get("parts") or []) or (
                    f"task ended in state {state!r}"
                )

            envelope = {
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
            if pending:
                envelope["input_required"] = pending
            if asked:
                envelope["asked"] = asked
            return envelope
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "state": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "stage": "invoke",
            "trace": trace,
            "duration_ms": int((time.time() - started) * 1000),
        }


def _pending_input(task: dict) -> dict | None:
    """What a parked task is waiting for.

    ADK emits the interrupt as a data part marked `adk_type: function_call`
    naming `adk_request_input`; its args carry the message shown to the human
    and the schema their answer must match.
    """
    turns = list(task.get("history") or [])
    status_message = (task.get("status") or {}).get("message")
    if status_message:
        turns.append(status_message)

    # Newest first: a node that asks again (an approval missing a required
    # field) leaves the original question earlier in the history, and showing
    # that one would hide what is actually still needed.
    for turn in reversed(turns):
        for part in (turn or {}).get("parts") or []:
            if (part.get("metadata") or {}).get("adk_type") != "function_call":
                continue
            data = part.get("data") or {}
            if data.get("name") != "adk_request_input":
                continue
            args = data.get("args") or {}
            return {
                "interrupt_id": data.get("id"),
                "prompt": args.get("message"),
                "response_schema": args.get("response_schema")
                or args.get("responseSchema"),
                "payload": args.get("payload"),
            }
    return None


def resume(
    task_id: str, context_id: str | None, interrupt_id: str, response: Any
) -> dict:
    """Answer a task an *earlier* invocation parked, without re-running.

    Possible only because the task and its session are on disk (`STATE_DSN`).
    The workflow carries on from the approval node; nothing before it runs a
    second time.
    """
    from starlette.testclient import TestClient

    trace: list[dict] = []
    started = time.time()

    try:
        app = _build_traced_app(trace)
    except Exception as exc:  # noqa: BLE001
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
            reply = _rpc(
                client,
                "message/send",
                {
                    "message": _answer_message(task_id, context_id, interrupt_id, response),
                    "configuration": {"blocking": True},
                },
            )
            if reply.get("error"):
                raise RuntimeError(
                    reply["error"].get("message") or str(reply["error"])
                )

            task = reply["result"]
            state = (task.get("status") or {}).get("state")
            text = _artifact_text(task)
            result: Any = None
            if text is not None:
                try:
                    result = json.loads(text)
                except (ValueError, TypeError):
                    result = text

            pending = _pending_input(task) if state == "input-required" else None
            envelope = {
                "ok": state == "completed",
                "state": state,
                "task_id": task.get("id"),
                "context_id": task.get("contextId"),
                "mode": "message",
                "polls": 0,
                "result": result,
                "artifact_text": text,
                "error": None
                if state == "completed" or pending
                else f"task ended in state {state!r}",
                "trace": trace,
                "resumed": True,
                "duration_ms": int((time.time() - started) * 1000),
            }
            if pending:
                envelope["input_required"] = pending
            return envelope
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "state": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "stage": "resume",
            "trace": trace,
            "duration_ms": int((time.time() - started) * 1000),
        }


def _answer_message(
    task_id: str, context_id: str | None, interrupt_id: str, response: Any
) -> dict:
    """The A2A message that answers a request for input.

    The shape is fixed by ADK: a data part tagged `function_response`, carrying
    the interrupt id so the node finds it on `ctx.resume_inputs`.
    """
    message: dict = {
        "role": "user",
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "taskId": task_id,
        "parts": [
            {
                "kind": "data",
                "data": {
                    "id": interrupt_id,
                    "name": "adk_request_input",
                    "response": response,
                },
                "metadata": {"adk_type": "function_response"},
            }
        ],
    }
    if context_id:
        message["contextId"] = context_id
    return message


def _first_text(parts: list[dict]) -> str | None:
    for part in parts:
        if part.get("kind") == "text" and part.get("text"):
            return part["text"]
    return None


def _print_human(envelope: dict) -> None:
    from nodes.registry import NODE_TITLES, NODE_TYPES

    state = envelope.get("state")
    pending = envelope.get("input_required")
    mark = "OK" if envelope.get("ok") else ("WAITING" if pending else "FAILED")
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

    if pending:
        print("\nthis workflow is waiting for a person:", file=sys.stderr)
        print(f"  {pending.get('prompt')}", file=sys.stderr)
        schema = pending.get("response_schema") or {}
        fields = list((schema.get("properties") or {}).keys())
        if fields:
            required = schema.get("required") or []
            shown = ", ".join(f"{f}*" if f in required else f for f in fields)
            print(f"  answer with: {shown}", file=sys.stderr)
        payload = (pending.get("payload") or {}).get("assignees")
        if payload:
            print(f"  assigned to: {', '.join(payload)}", file=sys.stderr)
        print(
            "\n  answer it with:  python run_once.py '<payload>' "
            "--answer '{\"approved\": true}'",
            file=sys.stderr,
        )

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
    # Answering a workflow parked on a HUMAN_APPROVAL node. The task lives in
    # this process, so answering it means running the workflow again in the
    # same invocation -- see --replay.
    parser.add_argument(
        "--answer",
        metavar="JSON",
        help=(
            'answer a request for input, e.g. \'{"approved": true}\'. '
            "Runs the workflow, then answers the first thing it asks for."
        ),
    )
    # Answering a task an earlier invocation parked. The task and its session
    # are on disk, so this resumes rather than replaying the workflow.
    parser.add_argument("--resume", metavar="TASK_ID", help="task id to answer")
    parser.add_argument("--context", metavar="CONTEXT_ID", help="its context id")
    parser.add_argument(
        "--interrupt", metavar="INTERRUPT_ID", help="the interrupt being answered"
    )
    args = parser.parse_args()

    if args.resume:
        if not args.answer or not args.interrupt:
            parser.error("--resume needs --interrupt and --answer")
        try:
            response: Any = json.loads(args.answer)
        except (ValueError, TypeError):
            response = {"approved": args.answer.strip().lower() in ("true", "yes", "y")}
        envelope = resume(args.resume, args.context, args.interrupt, response)
        if args.json:
            print(json.dumps(envelope, default=str))
        else:
            _print_human(envelope)
        return 0 if envelope.get("ok") else 1

    try:
        payload: Any = json.loads(args.payload)
    except (ValueError, TypeError):
        # Not JSON: send it as prose, which the entry node accepts.
        payload = args.payload

    response: Any = None
    if args.answer:
        try:
            response = json.loads(args.answer)
        except (ValueError, TypeError):
            response = {"approved": args.answer.strip().lower() in ("true", "yes", "y")}

    envelope = run(payload, args.mode, args.timeout, answer_with=response)

    if args.answer and not envelope.get("asked"):
        envelope["error"] = (
            "--answer was given but the workflow did not ask for input "
            f"(state {envelope.get('state')!r})."
        )
        envelope["ok"] = False

    if args.json:
        print(json.dumps(envelope, default=str))
    else:
        _print_human(envelope)

    return 0 if envelope.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
