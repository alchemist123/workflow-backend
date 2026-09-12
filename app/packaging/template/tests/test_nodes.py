"""Each node in isolation, plus the core helpers they all depend on.

Nodes are ADK `FunctionNode` objects after decoration, so the tests call the
underlying function through `node.func` (falling back to the wrapped callable)
with a stub context. That keeps a node testable without building the graph.
"""

from __future__ import annotations

from typing import Any

import pytest


class StubState:
    """Mimics ADK's `State`: get/setdefault/update/to_dict only, no iteration."""

    def __init__(self, initial: dict | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)

    def setdefault(self, key, default=None):
        return self._data.setdefault(key, default)

    def update(self, other):
        self._data.update(other)

    def to_dict(self):
        return dict(self._data)

    def has_delta(self):
        return bool(self._data)


class StubContext:
    def __init__(self, state: dict | None = None) -> None:
        self.state = StubState(state)


def call(node, node_input=None, state=None, ctx=None):
    """Invoke a decorated node's underlying async function.

    Pass `ctx` to inspect state afterwards. Note that a direct call writes to
    `ctx.state`; `event.actions.state_delta` is only populated by ADK's own
    wrapper when the node runs inside a graph.
    """
    fn = getattr(node, "func", None) or getattr(node, "_func", None)
    assert fn is not None, f"cannot reach the function behind {node!r}"
    return fn(ctx or StubContext(state), node_input)


# ── core/state.py ────────────────────────────────────────────────────────────


def test_coerce_input_handles_every_inbound_shape():
    from google.genai import types

    from core.state import coerce_input

    # A dict passes straight through.
    assert coerce_input({"a": 1}) == {"a": 1}

    # None becomes empty rather than raising.
    assert coerce_input(None) == {}

    # The A2A entry case: a genai Content carrying JSON text.
    content = types.Content(role="user", parts=[types.Part(text='{"text": "hi"}')])
    assert coerce_input(content) == {"text": "hi"}

    # Prose rather than JSON.
    prose = types.Content(role="user", parts=[types.Part(text="hello there")])
    assert coerce_input(prose) == {"text": "hello there"}

    # A JSON string.
    assert coerce_input('{"n": 2}') == {"n": 2}

    # Valid JSON that is not an object.
    assert coerce_input("[1, 2]") == {"value": [1, 2]}

    # Empty text.
    assert coerce_input("   ") == {}


def test_state_helpers_use_only_adk_state_methods():
    from core.state import full_state, merge_state, read_state, replace_state

    ctx = StubContext()

    assert read_state(ctx) == {}
    assert merge_state(ctx, {"a": 1}) == {"a": 1}
    assert merge_state(ctx, {"b": 2}) == {"a": 1, "b": 2}
    assert read_state(ctx) == {"a": 1, "b": 2}
    assert replace_state(ctx, {"c": 3}) == {"c": 3}
    assert read_state(ctx) == {"c": 3}
    assert full_state(ctx) == {"wf": {"c": 3}}


def test_as_text_renders_non_serialisable_values():
    from core.state import as_text

    assert as_text("already text") == "already text"
    assert as_text({"a": 1}) == '{"a": 1}'
    # Must not raise on something json cannot encode.
    assert as_text({"o": object()}).startswith("{")


# ── nodes/base.py ────────────────────────────────────────────────────────────


def test_node_kwargs_maps_policies_to_adk():
    from google.adk.workflow import RetryConfig

    from nodes.base import NODE_KWARGS

    # One attempt means no retry, so no RetryConfig at all.
    assert NODE_KWARGS(timeout=30, retries=1) == {"timeout": 30.0}
    assert NODE_KWARGS() == {}

    with_retry = NODE_KWARGS(timeout=0, retries=3)
    assert isinstance(with_retry["retry_config"], RetryConfig)
    assert "timeout" not in with_retry

    assert NODE_KWARGS(rerun_on_resume=True) == {"rerun_on_resume": True}


def test_terminal_event_carries_content():
    """The rule that makes an A2A task reach `completed`."""
    from nodes.base import terminal_event

    event = terminal_event({"ok": True})

    assert event.output == {"ok": True}
    assert event.content is not None
    assert event.content.parts[0].text == '{"ok": true}'


def test_route_event_sets_the_route_action():
    from nodes.base import route_event

    event = route_event("long", {"n": 1})

    assert event.actions.route == "long"
    assert event.output == {"n": 1}


def test_node_error_produces_a_branchable_payload():
    from nodes.base import node_error

    event = node_error("n_x", ValueError("boom"))

    assert event.output["_error"] is True
    assert event.output["_error_node"] == "n_x"
    assert event.output["_error_type"] == "ValueError"
    assert event.output["error"] == "boom"


# ── the workflow's own nodes ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a2a_start_seeds_state_from_the_payload():
    from google.genai import types

    from nodes.a2a_start import PAYLOAD_SCHEMA, a2a_start

    ctx = StubContext()
    content = types.Content(role="user", parts=[types.Part(text='{"text": "hi"}')])
    event = await call(a2a_start, content, ctx=ctx)

    assert event.output == {"text": "hi"}
    # The payload becomes the initial workflow state.
    assert ctx.state.to_dict() == {"wf": {"text": "hi"}}
    # The test panel renders its form from this.
    assert PAYLOAD_SCHEMA["properties"]["text"]["type"] == "string"


@pytest.mark.asyncio
async def test_normalise_measures_text():
    from nodes.n_normalise_1 import n_normalise_1

    event = await call(n_normalise_1, {"text": "  one two three  "})

    assert event.output["text"] == "one two three"
    assert event.output["word_count"] == 3
    assert event.output["char_count"] == 13


@pytest.mark.asyncio
async def test_normalise_preserves_upstream_keys():
    from nodes.n_normalise_1 import n_normalise_1

    event = await call(n_normalise_1, {"text": "hi", "trace_id": "abc"})

    assert event.output["trace_id"] == "abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("word_count", "expected"),
    [(0, "short"), (20, "short"), (21, "long"), (500, "long")],
)
async def test_route_boundaries(word_count, expected):
    from nodes.n_route_2 import n_route_2

    event = await call(n_route_2, {"word_count": word_count})

    assert event.actions.route == expected


@pytest.mark.asyncio
async def test_route_falls_back_to_default_on_missing_input():
    from nodes.n_route_2 import CONFIG, n_route_2

    event = await call(n_route_2, {})

    assert event.actions.route == CONFIG["default"]


@pytest.mark.asyncio
async def test_summarise_keeps_the_configured_sentence_count():
    from nodes.n_summarise_3 import CONFIG, n_summarise_3

    event = await call(
        n_summarise_3, {"text": "One. Two. Three. Four."}
    )

    assert CONFIG["max_sentences"] == 2
    assert event.output["summary"] == "One. Two."


@pytest.mark.asyncio
async def test_summarise_handles_empty_text():
    from nodes.n_summarise_3 import n_summarise_3

    event = await call(n_summarise_3, {"text": ""})

    assert event.output["summary"] == ""


@pytest.mark.asyncio
async def test_end_applies_the_output_mapping():
    from nodes.n_end_5 import n_end_5

    event = await call(
        n_end_5,
        {"summary": "s", "word_count": 3, "branch": "short", "dropped": "x"},
    )

    assert event.output == {"summary": "s", "word_count": 3, "branch": "short"}
    assert "dropped" not in event.output
    # Terminal nodes must emit content.
    assert event.content is not None and event.content.parts


@pytest.mark.asyncio
async def test_end_reads_from_state_when_input_is_thin():
    """State is the fallback, so a mapped key set upstream still lands."""
    from nodes.n_end_5 import n_end_5

    event = await call(
        n_end_5,
        {"summary": "from-input"},
        state={"wf": {"word_count": 7, "branch": "long"}},
    )

    assert event.output["summary"] == "from-input"
    assert event.output["word_count"] == 7
    assert event.output["branch"] == "long"


# ── tools ────────────────────────────────────────────────────────────────────


def test_mcp_url_normalisation():
    from tools.mcp import normalise_url

    assert normalise_url("http://h:9000") == "http://h:9000/mcp"
    assert normalise_url("http://h:9000/") == "http://h:9000/mcp"
    assert normalise_url("http://h:9000/mcp") == "http://h:9000/mcp"
    assert normalise_url("") == ""


def test_a2a_result_extraction_prefers_artifacts():
    from tools.a2a import extract_result

    task = {
        "result": {
            "kind": "task",
            "artifacts": [{"parts": [{"kind": "text", "text": "the answer"}]}],
            "history": [
                {"role": "agent", "parts": [{"kind": "text", "text": "older"}]}
            ],
        }
    }
    assert extract_result(task) == {"result": "the answer"}


def test_a2a_result_extraction_falls_back_through_shapes():
    from tools.a2a import extract_result

    # A Message reply rather than a Task.
    message = {
        "result": {"kind": "message", "parts": [{"kind": "text", "text": "hi"}]}
    }
    assert extract_result(message) == {"result": "hi"}

    # Only history available.
    history = {
        "result": {
            "kind": "task",
            "history": [
                {"role": "user", "parts": [{"kind": "text", "text": "q"}]},
                {"role": "agent", "parts": [{"kind": "text", "text": "a"}]},
            ],
        }
    }
    assert extract_result(history) == {"result": "a"}

    # A JSON-RPC error.
    assert extract_result({"error": {"code": -1, "message": "nope"}}) == {
        "error": "nope"
    }


def test_inline_function_runner_returns_result():
    from tools.functions import run_code

    assert run_code("result = data['a'] + 1", {"a": 1}) == 2
    # `output` is accepted for older canvases.
    assert run_code("output = 'x'", {}) == "x"


def test_inline_function_runner_blocks_imports_and_file_access():
    """SAFE_BUILTINS is a guardrail against accidents, not a sandbox.

    It does keep an ordinary transform from reaching the host by mistake.
    """
    from tools.functions import run_code

    with pytest.raises(Exception):
        run_code("import os\nresult = os.listdir('/')", {})

    with pytest.raises(Exception):
        run_code("result = open('/etc/passwd').read()", {})
