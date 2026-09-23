"""Every node module in Condition Router, checked against its contract.

Generated. These are the invariants ADK enforces at run time, asserted here at
test time instead — a hand-edit that breaks one fails `pytest` rather than a
deploy.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

import pytest

from nodes.registry import CANVAS_IDS, NODES, NODE_TYPES, ENTRY_NODE, TERMINAL_NODE

# Nodes with a generated module. A MERGE is a JoinNode built in the registry and
# has no module of its own.
MODULE_NODES = ['a2a_start', 'n_condition_1', 'n_end_2', 'n_transform_3', 'n_transform_4']


class StubState:
    """Mimics ADK's State: get/setdefault/update/to_dict only, no iteration."""

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


def _function_of(node) -> Any:
    """The plain async function behind a decorated node."""
    return getattr(node, "func", None) or getattr(node, "_func", None)


# ── Registry consistency ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name", MODULE_NODES)
def test_module_declares_its_own_node_id(name):
    """A node's NODE_ID must match its registry key, or run traces mis-attribute."""
    module = importlib.import_module(f"nodes.{name}")
    assert module.NODE_ID == name


@pytest.mark.parametrize("name", MODULE_NODES)
def test_module_exports_the_node(name):
    module = importlib.import_module(f"nodes.{name}")
    assert hasattr(module, name), f"nodes/{name}.py does not define {name}"
    assert NODES[name] is getattr(module, name)


def test_every_node_is_traceable_to_a_canvas_node():
    for name in NODES:
        assert CANVAS_IDS.get(name), name
        assert NODE_TYPES.get(name), name


# ── The ADK node authoring contract ──────────────────────────────────────────


@pytest.mark.parametrize("name", MODULE_NODES)
def test_signature_is_ctx_then_node_input(name):
    """ADK's default 'state' binding requires this exact shape.

    A parameter literally named `node_input` receives the upstream node's output;
    any other parameter would be looked up in `ctx.state` and raise if absent.
    """
    function = _function_of(NODES[name])
    assert function is not None, name

    parameters = list(inspect.signature(function).parameters)
    assert parameters[:2] == ["ctx", "node_input"], (name, parameters)


@pytest.mark.parametrize("name", MODULE_NODES)
def test_no_return_annotation(name):
    """A `-> Event` hint is inferred as the node's output_schema, and every
    downstream edge then fails ADK's edge validation with 'Schema mismatch'."""
    function = _function_of(NODES[name])
    assert inspect.signature(function).return_annotation is inspect.Signature.empty, name


@pytest.mark.parametrize("name", MODULE_NODES)
def test_node_is_async(name):
    assert inspect.iscoroutinefunction(_function_of(NODES[name])), name


# ── Entry and terminal nodes ─────────────────────────────────────────────────


def test_entry_node_documents_its_payload():
    module = importlib.import_module(f"nodes.{ENTRY_NODE}")

    assert isinstance(module.PAYLOAD_SCHEMA, dict)
    assert module.PAYLOAD_SCHEMA.get("type") == "object"
    assert isinstance(module.EXAMPLE_PAYLOAD, dict)


@pytest.mark.asyncio
async def test_entry_node_accepts_every_inbound_shape():
    """The first node after START gets a genai Content; a caller may also send
    prose or a JSON string. core/state.py::coerce_input normalises all three."""
    from google.genai import types

    entry = _function_of(NODES[ENTRY_NODE])

    content = types.Content(role="user", parts=[types.Part(text='{"a": 1}')])
    event = await entry(StubContext(), content)
    assert event.output == {"a": 1}

    event = await entry(StubContext(), "just prose")
    assert event.output == {"text": "just prose"}

    event = await entry(StubContext(), None)
    assert event.output == {}


@pytest.mark.asyncio
async def test_terminal_node_emits_content():
    """An A2A task only reaches `completed` if the terminal event carries content
    parts: the executor promotes them into the result artifact. An event with
    only `output` leaves the task at `working` forever."""
    terminal = _function_of(NODES[TERMINAL_NODE])

    event = await terminal(StubContext({"wf": {"done": True}}), {"done": True})

    assert event.content is not None, "terminal node emitted no content"
    assert event.content.parts, "terminal node emitted empty content"
    assert event.content.parts[0].text
