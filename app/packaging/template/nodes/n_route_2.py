"""CONDITION -- route on the measured length.

A CONDITION node compiles to a router: it returns `Event(route=...)` and the
outgoing edge in `agent.py` carries a dict mapping each route value to a target
node.  Branches are evaluated in canvas order, first match wins, and an
unmatched payload falls through to the `default` route -- which the compiler
always emits so a graph can never dead-end on an unexpected value.
"""

from __future__ import annotations

from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import merge_state
from nodes.base import NODE_KWARGS, route_event

NODE_ID = "n_route_2"

CONFIG: dict = {
    "branches": [
        {"name": "long", "description": "More than 20 words."},
        {"name": "short", "description": "20 words or fewer."},
    ],
    "default": "short",
}

log = node_logger(NODE_ID)


def _evaluate(data: dict) -> str:
    """The canvas branch expressions, emitted as ordinary Python."""
    if data.get("word_count", 0) > 20:
        return "long"
    return "short"


@node(name=NODE_ID, **NODE_KWARGS(timeout=30))
async def n_route_2(ctx: Context, node_input=None):
    data = node_input or {}
    branch = _evaluate(data) or CONFIG["default"]
    log.info("routing to %r", branch)
    merge_state(ctx, {"branch": branch})
    return route_event(branch, data)
