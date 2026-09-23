"""Helpers for moving data between graph nodes.

Two ADK details make this module worth having rather than inlining:

1.  The first node after ``START`` receives the raw ``google.genai.types.Content``
    user message, while every later node receives its predecessor's ``output``.
    ``coerce_input`` collapses those two shapes into one dict so nodes after the
    start node only ever see a dict.

2.  ``ctx.state`` is an ADK ``State``, not a dict.  It exposes only ``get``,
    ``setdefault``, ``update``, ``to_dict`` and ``has_delta`` -- ``dict(state)``
    raises ``KeyError: 0`` and ``**state`` does not work.  Everything here goes
    through the supported surface.
"""

from __future__ import annotations

import json
from typing import Any

# Top-level key under which the workflow keeps its accumulated state.  Namespaced
# so it cannot collide with ADK's own reserved `app:` / `user:` / `temp:` keys.
STATE_KEY = "wf"


def coerce_input(node_input: Any) -> dict[str, Any]:
    """Normalise whatever the runner handed a node into a dict.

    Accepts a dict (passed straight through), a ``Content``-like object with
    ``.parts`` (the A2A entry case), a JSON string, or anything else.  Text that
    parses as a JSON object is used as the payload; text that does not is wrapped
    as ``{"text": ...}`` so a plain-language A2A message still reaches the graph.
    """
    if node_input is None:
        return {}

    if isinstance(node_input, dict):
        return node_input

    # google.genai.types.Content / Part -- the shape the A2A layer delivers.
    parts = getattr(node_input, "parts", None)
    if parts is not None:
        text = "".join(getattr(part, "text", "") or "" for part in parts)
        return _from_text(text)

    if isinstance(node_input, str):
        return _from_text(node_input)

    # A pydantic model (e.g. a node with a declared output_schema).
    dump = getattr(node_input, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:  # noqa: BLE001 - fall through to the generic wrapper
            pass

    return {"value": node_input}


def _from_text(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {"text": text}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def read_state(ctx: Any) -> dict[str, Any]:
    """The workflow's accumulated state as a plain dict."""
    current = ctx.state.get(STATE_KEY)
    return dict(current) if isinstance(current, dict) else {}


def merge_state(ctx: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge `values` into workflow state and return the new state.

    Writing through ``ctx.state`` records a state delta that ADK attaches to the
    emitted event, so the session service persists it.
    """
    merged = {**read_state(ctx), **(values or {})}
    ctx.state[STATE_KEY] = merged
    return merged


def replace_state(ctx: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Overwrite workflow state wholesale."""
    new_state = dict(values or {})
    ctx.state[STATE_KEY] = new_state
    return new_state


def full_state(ctx: Any) -> dict[str, Any]:
    """Every key in the session state, including any set outside the workflow."""
    try:
        return ctx.state.to_dict()
    except Exception:  # noqa: BLE001 - state dumping must never break a run
        return {}


def as_text(value: Any) -> str:
    """Render a node result as the text an A2A artifact carries."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
