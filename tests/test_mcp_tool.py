"""MCP_TOOL: calling one named tool on an MCP server, inline in the flow.

The existing TOOL node is a tool *provider* — wired into an agent's tools
handle, called if and when a model decides to. This node is the other half: an
ordinary step, called every time the flow reaches it, with arguments the canvas
maps rather than a model invents.

Run against a real MCP server (`tests/support/demo_mcp_server.py`) rather than
a mock, because the two bugs that actually mattered here were both about what a
real server does: it answers at a path the client did not guess, and it returns
its result as JSON *text* with no `structuredContent` at all.
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
from app.compiler.semantic_validator import validate_semantics
from app.nodes.registry import NODE_REGISTRY, TOOL_PROVIDER_TYPES
from app.packaging.render import render_package
from app.runtime.mcp_discovery import arguments_from_schema, candidates, list_tools
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)


# ── A real server for the duration of the module ─────────────────────────────


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def mcp_server():
    """The demo MCP server, on its own port. Yields its base URL."""
    port = _free_port()
    script = Path(__file__).parent / "support" / "demo_mcp_server.py"
    process = subprocess.Popen(
        [sys.executable, str(script), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
    else:  # pragma: no cover - only on a very slow machine
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


# ── Canvas helpers ───────────────────────────────────────────────────────────


def _node(node_id, node_type, config=None, title=""):
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": title or node_id, "description": ""},
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
    "input_mode": "json", "state_key": "wf", "output_variable": "order",
    "payload_schema": {"fields": [
        {"name": "item", "type": "string", "description": "", "required": True},
        {"name": "count", "type": "integer", "description": "", "required": True},
    ]},
}

# What the demo server reports for price_quote, as the config panel caches it.
_QUOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "sku": {"type": "string"},
        "quantity": {"type": "integer"},
        "currency": {"type": "string"},
    },
    "required": ["sku", "quantity"],
}

_ARGS = [
    {"name": "sku", "type": "string", "source": "data.item", "required": True},
    {"name": "quantity", "type": "integer", "source": "data.count", "required": True},
    {"name": "currency", "type": "string", "default": "GBP"},
]


def _canvas(url: str, config: dict | None = None) -> dict:
    node_config = {
        "mcp_url": f"{url}/mcp",
        "tool_name": "price_quote",
        "tool_schema": _QUOTE_SCHEMA,
        "arg_mode": "fields",
        "arg_fields": _ARGS,
        **(config or {}),
    }
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "nodes": [
            _node("start", "A2A_START", _START, "Order"),
            _node("quote", "MCP_TOOL", node_config, "Price Quote"),
            _node("end", "END", {"output_mapping": {}}),
        ],
        "edges": [_edge("start", "quote"), _edge("quote", "end")],
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


# ── It is a step, not something an agent may call ────────────────────────────


def test_it_is_not_offered_to_agents_as_a_tool():
    """That is what TOOL is for. Mixing the two is what confused people.

    A node called every time the flow reaches it, and a node a model may choose
    to call, want different configuration and different mental models.
    """
    assert "MCP_TOOL" not in TOOL_PROVIDER_TYPES
    assert NODE_REGISTRY["MCP_TOOL"].provides_tool is False
    assert NODE_REGISTRY["TOOL"].provides_tool is True


def test_the_url_and_token_never_reach_the_generated_code(tmp_path, mcp_server):
    """An MCP URL can carry basic-auth credentials, and nodes/ is committed."""
    destination = _render(_canvas(mcp_server), "mcp-secret-0001", tmp_path)
    source = next((destination / "nodes").glob("*mcp_tool*.py")).read_text()

    assert mcp_server not in source
    assert "MCP_PRICE_QUOTE_URL" in source
    assert f"MCP_PRICE_QUOTE_URL={mcp_server}/mcp" in (destination / ".env").read_text()


# ── Reaching a server someone typed the URL of ───────────────────────────────


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        # An explicit endpoint is tried as given, first.
        ("http://h/mcp", [("http", "http://h/mcp"), ("sse", "http://h/sse")]),
        ("http://h/sse", [("sse", "http://h/sse")]),
        # A bare host: the two conventional paths, then the root.
        ("http://h", [("http", "http://h/mcp"), ("sse", "http://h/sse"),
                      ("http", "http://h")]),
        ("http://h/", [("http", "http://h/mcp"), ("sse", "http://h/sse"),
                       ("http", "http://h")]),
        ("", []),
    ],
)
def test_a_url_someone_typed_is_tried_several_ways(typed, expected):
    """It used to append `/mcp` unconditionally.

    That turned a working `https://host/sse` into a 404 and reported it as the
    server being unreachable — the single most likely thing to go wrong when a
    person pastes a URL off a README.
    """
    assert candidates(typed) == expected


@pytest.mark.asyncio
async def test_the_bare_host_a_user_is_most_likely_to_paste_works(mcp_server):
    found, error = await list_tools(mcp_server)
    assert error is None, error
    assert {t.name for t in found} == {"price_quote", "stock_level", "always_fails"}


@pytest.mark.asyncio
async def test_discovery_reports_each_tools_arguments(mcp_server):
    """What makes the picker able to pre-fill the argument rows."""
    found, error = await list_tools(f"{mcp_server}/mcp")
    assert error is None, error

    quote = next(t for t in found if t.name == "price_quote")
    assert quote.description == "Quote a price for a SKU and quantity."
    by_name = {a.name: a for a in quote.arguments}
    assert by_name["sku"].type == "string" and by_name["sku"].required
    assert by_name["quantity"].type == "integer" and by_name["quantity"].required
    assert not by_name["currency"].required


def test_a_nullable_argument_keeps_its_real_type():
    """JSON Schema writes an optional string as ["string", "null"]."""
    args = arguments_from_schema(
        {"properties": {"note": {"type": ["string", "null"]}}, "required": []}
    )
    assert args[0].type == "string"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", "Enter the MCP server URL first."),
        ("http://no-such-host.invalid/mcp", "does not resolve"),
    ],
)
async def test_discovery_failures_say_what_to_do(url, expected):
    """The Fetch tools button shows this verbatim.

    It used to surface anyio's wrapper — "unhandled errors in a TaskGroup
    (1 sub-exception)" — to someone who had simply mistyped a port.
    """
    found, error = await list_tools(url, timeout=8)
    assert found == []
    assert expected in error


@pytest.mark.asyncio
async def test_a_url_that_is_not_an_mcp_server_says_so(mcp_server):
    found, error = await list_tools(f"{mcp_server}/definitely-not-mcp", timeout=8)
    assert found == []
    assert "not as an MCP server" in error


# ── Calling it ───────────────────────────────────────────────────────────────


def test_the_mapped_arguments_reach_the_tool(tmp_path, mcp_server):
    destination = _render(_canvas(mcp_server), "mcp-call-0001", tmp_path)
    envelope = _run(destination, {"item": "widget", "count": 3})

    assert envelope["state"] == "completed", envelope.get("error")
    result = envelope["result"]
    # `item` -> `sku` renamed and upper-cased by the tool; `count` -> `quantity`
    # converted to the integer the tool declared; `currency` a constant.
    assert result["sku"] == "WIDGET"
    assert result["quantity"] == 3
    assert result["total"] == 75.0
    assert result["currency"] == "GBP", "the constant argument was sent"


def test_the_tools_own_keys_land_on_the_payload(tmp_path, mcp_server):
    """So the next node reads `data.total`, not a JSON string it cannot index.

    The demo server — like the stock `mcp.server.fastmcp` — returns its result
    as JSON *text* and no `structuredContent`. Before the client parsed that,
    this arrived as `{"result": "{\\n  \\"total\\": 75.0 ...}"}` and the whole
    point of passing a tool's output to the next node was lost.
    """
    canvas = _canvas(mcp_server)
    canvas["nodes"].append(_node("after", "TRANSFORM", {
        "mode": "fields",
        "output_fields": [
            {"name": "line_total", "type": "number", "source": "data.total",
             "required": True},
        ],
    }))
    canvas["edges"] = [_edge("start", "quote"), _edge("quote", "after"),
                       _edge("after", "end")]

    destination = _render(canvas, "mcp-chain-0001", tmp_path)
    envelope = _run(destination, {"item": "gizmo", "count": 4})

    assert envelope["state"] == "completed", envelope.get("error")
    assert envelope["result"]["line_total"] == 38.0


def test_the_result_can_be_kept_under_a_key(tmp_path, mcp_server):
    """For a tool whose keys would collide with the payload's."""
    destination = _render(
        _canvas(mcp_server, {"result_key": "quote"}), "mcp-key-0001", tmp_path
    )
    result = _run(destination, {"item": "widget", "count": 1})["result"]

    assert result["quote"]["total"] == 25.0
    assert "total" not in result


def test_a_variable_can_supply_an_argument(tmp_path, mcp_server):
    """The entry payload is still reachable once the edge no longer carries it."""
    args = [
        {"name": "sku", "type": "string", "source": "vars.order.item",
         "required": True},
        {"name": "quantity", "type": "integer", "source": "vars.order.count",
         "required": True},
    ]
    canvas = _canvas(mcp_server, {"arg_fields": args})
    canvas["nodes"].insert(2, _node("between", "TRANSFORM", {
        "mode": "fields",
        "output_fields": [{"name": "unrelated", "type": "string", "default": "x"}],
    }))
    canvas["edges"] = [_edge("start", "between"), _edge("between", "quote"),
                       _edge("quote", "end")]

    destination = _render(canvas, "mcp-vars-0001", tmp_path)
    result = _run(destination, {"item": "widget", "count": 2})["result"]

    assert result["total"] == 50.0


def test_a_tool_that_raises_fails_the_run(tmp_path, mcp_server):
    destination = _render(
        _canvas(mcp_server, {
            "tool_name": "always_fails",
            "tool_schema": {"type": "object",
                            "properties": {"reason": {"type": "string"}},
                            "required": ["reason"]},
            "arg_fields": [{"name": "reason", "type": "string",
                            "default": "on purpose", "required": True}],
        }),
        "mcp-fails-0001", tmp_path,
    )
    envelope = _run(destination, {"item": "w", "count": 1})

    assert envelope["state"] == "failed"
    assert "on purpose" in envelope["error"]


def test_a_failing_tool_can_be_allowed_to_continue(tmp_path, mcp_server):
    """`on_error: continue` turns it into an `_error` payload to branch on."""
    canvas = _canvas(mcp_server, {
        "tool_name": "always_fails",
        "tool_schema": {"type": "object",
                        "properties": {"reason": {"type": "string"}},
                        "required": ["reason"]},
        "arg_fields": [{"name": "reason", "type": "string", "default": "oops",
                        "required": True}],
    })
    for node in canvas["nodes"]:
        if node["id"] == "quote":
            node["policies"]["on_error"] = "continue"

    destination = _render(canvas, "mcp-continue-0001", tmp_path)
    envelope = _run(destination, {"item": "w", "count": 1})

    assert envelope["state"] == "completed", envelope.get("error")
    assert envelope["result"]["_error"] is True


def test_only_the_mapped_arguments_are_sent(tmp_path, mcp_server):
    """A strict server rejects the extra keys every payload accumulates.

    `price_quote` declares three parameters; the payload arriving at the node
    also carries `item` and `count` from the entry node. Passthrough would send
    all five.
    """
    destination = _render(_canvas(mcp_server), "mcp-onlyargs-0001", tmp_path)
    source = next((destination / "nodes").glob("*mcp_tool*.py")).read_text()

    assert "build_mapping(ARG_FIELDS" in source
    assert "arguments = dict(data)" not in source


# ── Validation, so a broken call is caught before it runs ────────────────────


def _errors(canvas: dict) -> list[str]:
    return validate_semantics(CanvasPayload.model_validate(canvas))[0]


def _warnings(canvas: dict) -> list[str]:
    return validate_semantics(CanvasPayload.model_validate(canvas))[1]


def test_a_good_call_passes_clean(mcp_server):
    errors, warnings_out = validate_semantics(
        CanvasPayload.model_validate(_canvas(mcp_server))
    )
    assert errors == []
    assert not any("MCP_TOOL" in w for w in warnings_out), warnings_out


def test_no_server_url_is_an_error(mcp_server):
    assert any(
        "no server URL" in e for e in _errors(_canvas(mcp_server, {"mcp_url": ""}))
    )


def test_no_tool_name_is_an_error(mcp_server):
    assert any(
        "names no tool" in e for e in _errors(_canvas(mcp_server, {"tool_name": ""}))
    )


def test_a_required_argument_that_is_not_mapped_is_an_error(mcp_server):
    """The tool's own schema says it is required, so this is knowable early."""
    only_one = [a for a in _ARGS if a["name"] != "quantity"]
    errors = _errors(_canvas(mcp_server, {"arg_fields": only_one}))

    assert any("requires the argument 'quantity'" in e for e in errors), errors


def test_an_argument_reading_something_nothing_produces_is_an_error(mcp_server):
    bad = [{**a, "source": "data.nope"} if a["name"] == "sku" else a for a in _ARGS]
    errors = _errors(_canvas(mcp_server, {"arg_fields": bad}))

    assert any("data.nope" in e and "data.item" in e for e in errors), errors


def test_an_argument_the_tool_does_not_declare_only_warns(mcp_server):
    """The cached schema can be stale, and some servers accept extras."""
    extra = [*_ARGS, {"name": "made_up", "type": "string", "default": "x"}]
    canvas = _canvas(mcp_server, {"arg_fields": extra})

    assert _errors(canvas) == []
    assert any("does not declare" in w for w in _warnings(canvas)), _warnings(canvas)


def test_passthrough_against_a_tool_with_a_schema_warns(mcp_server):
    canvas = _canvas(mcp_server, {"arg_mode": "passthrough"})

    assert _errors(canvas) == []
    assert any("whole payload" in w for w in _warnings(canvas)), _warnings(canvas)


def test_a_node_whose_tools_were_never_fetched_still_compiles(mcp_server):
    """Typing a tool name by hand is allowed; there is simply less to check."""
    canvas = _canvas(mcp_server, {"tool_schema": {}, "arg_fields": _ARGS})
    assert _errors(canvas) == []


# ── What the next node can see ───────────────────────────────────────────────


def test_what_a_tool_returns_is_not_claimed_to_be_known(mcp_server):
    """Only the server knows, so the picker offers nothing rather than guessing."""
    from app.compiler.inputs import available_inputs, input_is_opaque

    canvas = CanvasPayload.model_validate(_canvas(mcp_server))
    canvas.nodes.append(
        CanvasPayload.model_validate(
            {**_canvas(mcp_server), "nodes": [_node("after", "TRANSFORM")]}
        ).nodes[0]
    )
    assert input_is_opaque(canvas, "end")


def test_a_result_key_is_the_one_thing_that_is_knowable(mcp_server):
    from app.compiler.inputs import available_inputs

    canvas = CanvasPayload.model_validate(
        _canvas(mcp_server, {"result_key": "quote"})
    )
    paths = {f.path for f in available_inputs(canvas, "end")}
    assert "data.quote" in paths


# ── The template and the platform agree on how to reach a server ─────────────


def test_the_platform_and_the_package_guess_the_same_endpoints(tmp_path, mcp_server):
    """Two copies of `candidates()` — one standalone, one importable here.

    A generated package cannot import `app.*` and the platform must not import
    a package in-process (its modules are named `tools`, `core`, `nodes`), so
    the logic exists twice. This runs the package's copy inside a real package
    and compares.
    """
    destination = _render(_canvas(mcp_server), "mcp-agree-0001", tmp_path)
    urls = ["http://h", "http://h/mcp", "http://h/sse", "http://h/", ""]

    result = subprocess.run(
        [sys.executable, "-c",
         "import json,sys;from tools.mcp import candidates;"
         "print(json.dumps([candidates(u) for u in json.loads(sys.argv[1])]))",
         json.dumps(urls)],
        cwd=str(destination), capture_output=True, text=True, timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    from_package = json.loads(result.stdout.strip().splitlines()[-1])

    assert [[list(pair) for pair in candidates(u)] for u in urls] == from_package
