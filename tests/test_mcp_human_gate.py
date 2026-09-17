"""An MCP tool an agent may call, but only once a person approves the call.

Two kinds of human-in-the-loop, and this is the second:

* a HUMAN_APPROVAL node is a **step you place** — the flow always reaches it,
  at a point you chose;
* `require_confirmation` on a TOOL is a **guard on a capability** — the model
  picks the moment, which is the only way to gate a tool an agent calls on its
  own initiative.

Both park the A2A task at `input-required`, so both are answered the same way.

The tests drive the real generated package against the real demo MCP server,
with only the model scripted: deciding to call a tool is the one part that
needs a paid API key, and it is not the part under test.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import warnings
from pathlib import Path

import httpx
import pytest

from app.compiler import build_graph_plan, run_compiler
from app.packaging.render import render_package
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def mcp_server():
    port = _free_port()
    script = Path(__file__).parent / "support" / "demo_mcp_server.py"
    process = subprocess.Popen(
        [sys.executable, str(script), str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.skip("the demo MCP server exited before it was ready")
        try:
            httpx.get(f"{base}/mcp", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:  # pragma: no cover
        process.kill()
        pytest.skip("the demo MCP server did not start in time")
    try:
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def _node(node_id, node_type, config=None, title=""):
    return {
        "id": node_id, "type": node_type, "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": title or node_id, "description": ""},
        "config": config or {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 120, "retry": {"max_attempts": 1},
                     "on_error": "fail"},
    }


def _edge(source, target, source_handle="output", target_handle="input"):
    return {
        "id": f"e_{source}_{target}_{target_handle}",
        "source": source, "source_handle": source_handle,
        "target": target, "target_handle": target_handle, "condition": None,
    }


def _canvas(mcp_url: str, *, gated: bool = True) -> dict:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", {
                "input_mode": "json", "state_key": "wf",
                "payload_schema": {"fields": [
                    {"name": "request", "type": "string", "description": "",
                     "required": True},
                ]},
            }, "Request"),
            _node("agent", "ORCHESTRATOR_AGENT", {
                "model": "gemini-2.0-flash",
                "instruction": "Quote prices with your tool.",
            }, "Buyer Agent"),
            _node("quote_tool", "TOOL", {
                "mcp_url": f"{mcp_url}/mcp",
                "tool_name": "price_quote",
                "require_confirmation": gated,
            }, "Price Quote"),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [
            _edge("start", "agent"),
            _edge("quote_tool", "agent", "output", "tools"),
            _edge("agent", "end"),
        ],
    }


def _render(canvas: dict, name: str, tmp_path):
    errors, _warnings, ir = run_compiler(CanvasPayload.model_validate(canvas), name)
    assert errors == [], errors
    destination = tmp_path / "pkg"
    render_package(build_graph_plan(ir, workflow_name=name), destination)
    return destination


# The probe that drives a rendered package with a scripted model. Written to
# the package and run there, because a package's modules are named `agent`,
# `core`, `nodes` — the platform never imports one in-process.
_PROBE = '''
import asyncio, json, sys, uuid
from typing import AsyncGenerator

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

APPROVE = json.loads(sys.argv[1])


class Scripted(BaseLlm):
    model: str = "scripted"

    async def generate_content_async(self, llm_request, stream=False):
        answers = [
            p.function_response for c in (llm_request.contents or [])
            for p in (c.parts or []) if p.function_response
            and not (p.function_response.name or "").startswith("adk_request_confirmation")
        ]
        if answers:
            yield LlmResponse(content=types.Content(role="model", parts=[
                types.Part(text=json.dumps(answers[-1].response))]))
            return
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(
                name=TOOL_FN, args={"sku": "WIDGET", "quantity": 2}))]))


async def main():
    from agent import root_agent
    from core.a2a_app import build_app
    import nodes.__NODE_MODULE__ as agent_node

    built = await agent_node._build_agent()
    tools = await built.canonical_tools()
    global TOOL_FN
    gate = next(t for t in tools if "price_quote" in t.name)
    TOOL_FN = gate.name

    out = {"tool": TOOL_FN,
           "requires_confirmation": await gate.check_require_confirmation({}, None)}

    built.model = Scripted()
    agent_node._agent = built
    app = build_app(root_agent)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://pkg",
                                     timeout=180) as c:
            async def rpc(params):
                r = await c.post("/", json={"jsonrpc": "2.0", "id": uuid.uuid4().hex,
                                            "method": "message/send", "params": params})
                return r.json()

            first = await rpc({
                "message": {"role": "user", "messageId": uuid.uuid4().hex,
                            "parts": [{"kind": "text",
                                       "text": json.dumps({"request": "quote 2 widgets"})}]},
                "configuration": {"blocking": True}})
            if first.get("error"):
                print(json.dumps({**out, "rpc_error": first["error"]})); return
            task = first["result"]
            out["first_state"] = task["status"]["state"]

            ask = None
            for part in (task["status"].get("message") or {}).get("parts") or []:
                if part.get("kind") == "data" and \\
                        part["data"].get("name", "").startswith("adk_request_confirmation"):
                    ask = part["data"]
            if not ask:
                out["asked"] = False
                for art in task.get("artifacts") or []:
                    for part in art.get("parts") or []:
                        if part.get("kind") == "text":
                            out["result_text"] = part["text"]
                print(json.dumps(out)); return

            out["asked"] = True
            out["pending_call"] = ask["args"]["originalFunctionCall"]

            answer = {
                "role": "user", "messageId": uuid.uuid4().hex,
                "taskId": task["id"], "contextId": task["contextId"],
                "parts": [{"kind": "data",
                           "data": {"id": ask["id"], "name": ask["name"],
                                    "response": {**ask["args"]["toolConfirmation"],
                                                 "confirmed": APPROVE}},
                           "metadata": {"adk_type": "function_response"}}],
            }
            second = await rpc({"message": answer, "configuration": {"blocking": True}})
            if second.get("error"):
                print(json.dumps({**out, "rpc_error": second["error"]})); return
            done = second["result"]
            out["second_state"] = done["status"]["state"]
            for art in done.get("artifacts") or []:
                for part in art.get("parts") or []:
                    if part.get("kind") == "text":
                        out["result_text"] = part["text"]
    print(json.dumps(out))

asyncio.run(main())
'''


def _drive(destination: Path, *, approve: bool) -> dict:
    """Run the package's A2A app with a scripted model; return what happened."""
    node = next(
        p.stem for p in (destination / "nodes").glob("*orchestrator_agent*.py")
    )
    (destination / "probe.py").write_text(_PROBE.replace("__NODE_MODULE__", node))
    result = subprocess.run(
        [sys.executable, "probe.py", json.dumps(approve)],
        cwd=str(destination), capture_output=True, text=True, timeout=300,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── The gate ─────────────────────────────────────────────────────────────────


def test_the_canvas_flag_reaches_the_generated_tool(tmp_path, mcp_server):
    destination = _render(_canvas(mcp_server), "gate-wire-0001", tmp_path)
    node = next((destination / "nodes").glob("*orchestrator_agent*.py")).read_text()

    assert "'require_confirmation': True" in node
    assert "require_confirmation=bool(server.get(\"require_confirmation\"))" in (
        destination / "tools" / "mcp.py"
    ).read_text()


def test_an_ungated_tool_carries_no_flag(tmp_path, mcp_server):
    """Off by default, and absent rather than False, so nothing else shifts."""
    destination = _render(_canvas(mcp_server, gated=False), "gate-off-0001", tmp_path)
    node = next((destination / "nodes").glob("*orchestrator_agent*.py")).read_text()
    servers = next(l for l in node.splitlines() if l.startswith("MCP_SERVERS"))

    assert "require_confirmation" not in servers, servers


def test_the_agent_node_can_be_resumed_into(tmp_path, mcp_server):
    """ADK refuses `ctx.run_node` without it, and says exactly why.

    "A node must have rerun_on_resume=True. Reason is that dynamically
    scheduled nodes might be interrupted, and the workflow wakes-up/re-runs
    the parent node, so it can get the child node response." Measured: without
    it the run dies with DynamicNodeFailError the first time a tool asks.
    """
    destination = _render(_canvas(mcp_server), "gate-rerun-0001", tmp_path)
    node = next((destination / "nodes").glob("*orchestrator_agent*.py")).read_text()

    assert "rerun_on_resume=True" in node
    assert "ctx.run_node(_agent" in node


def test_the_agent_runs_inside_the_workflow_not_beside_it(tmp_path, mcp_server):
    """The regression this guards.

    The agent node used to run its LlmAgent in its own `Runner` with a fresh
    `InMemorySessionService`, keeping only `is_final_response()` text. A
    confirmation request was produced and then dropped, so the task completed
    as though the human had approved — and even had it propagated, the
    throwaway session left nothing to resume into.
    """
    destination = _render(_canvas(mcp_server), "gate-ctx-0001", tmp_path)
    node = next((destination / "nodes").glob("*orchestrator_agent*.py")).read_text()

    # Asserted on the code, not the file: the module's own comments explain
    # what it stopped doing, and say these words.
    code = [l for l in node.splitlines() if not l.strip().startswith(("#", "*"))]
    assert not any("import InMemorySessionService" in l for l in code)
    assert not any("Runner(app_name=" in l for l in code)


def test_the_run_stops_and_names_the_call_it_wants_to_make(tmp_path, mcp_server):
    destination = _render(_canvas(mcp_server), "gate-park-0001", tmp_path)
    out = _drive(destination, approve=True)

    assert out["requires_confirmation"] is True
    assert out["first_state"] == "input-required", out
    assert out["asked"] is True, out
    # Not just "something needs approval" — which call, with which arguments.
    assert out["pending_call"]["args"] == {"sku": "WIDGET", "quantity": 2}
    assert "price_quote" in out["pending_call"]["name"]


def test_approving_lets_the_mcp_tool_run(tmp_path, mcp_server):
    destination = _render(_canvas(mcp_server), "gate-approve-0001", tmp_path)
    out = _drive(destination, approve=True)

    assert out["second_state"] == "completed", out
    # 2 widgets at 25.0 — the real demo server answered.
    assert '"total": 50.0' in out["result_text"] or "50.0" in out["result_text"], out


def test_rejecting_stops_it_and_the_server_is_never_called(tmp_path, mcp_server):
    destination = _render(_canvas(mcp_server), "gate-reject-0001", tmp_path)
    out = _drive(destination, approve=False)

    assert out["first_state"] == "input-required"
    assert out["second_state"] == "completed", out
    assert "rejected" in out["result_text"].lower(), out
    assert "50.0" not in out["result_text"], "the tool ran despite being rejected"


def test_without_the_gate_the_tool_runs_unasked(tmp_path, mcp_server):
    """The counterfactual: the same flow, the flag off, no interruption."""
    destination = _render(_canvas(mcp_server, gated=False), "gate-none-0001", tmp_path)
    out = _drive(destination, approve=True)

    assert out["requires_confirmation"] is False
    assert out["first_state"] == "completed", out
    assert out["asked"] is False
    assert "50.0" in out["result_text"], out
