"""TRANSFORM — body.

Generated from canvas node 'body'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import merge_state
from nodes.base import NODE_KWARGS, node_error

NODE_ID = "n_transform_1"
ON_ERROR = "fail"

log = node_logger(NODE_ID)


def _transform(data: dict) -> dict:
    """The canvas expression, emitted as ordinary Python."""
    result = None
    result = {'doubled': (data.get('current_item') or 0) * 2}
    if isinstance(result, dict):
        return result
    return {"result": result} if result is not None else {}


@node(name=NODE_ID, **NODE_KWARGS(timeout=60))
async def n_transform_1(ctx: Context, node_input=None):
    data = node_input or {}
    try:
        result = _transform(data)
    except Exception as exc:  # noqa: BLE001
        if ON_ERROR != "continue":
            raise
        return node_error(NODE_ID, exc)

    log.info("transformed %d key(s) into %d", len(data), len(result))
    merge_state(ctx, result)
    return Event(output={**data, **result})
