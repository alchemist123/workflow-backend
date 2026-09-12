"""TRANSFORM -- the `short` branch needs no summary, so it labels and passes on."""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import merge_state
from nodes.base import NODE_KWARGS

NODE_ID = "n_passthrough_4"

CONFIG: dict = {"mode": "python", "on_error": "fail"}

log = node_logger(NODE_ID)


@node(name=NODE_ID, **NODE_KWARGS(timeout=30))
async def n_passthrough_4(ctx: Context, node_input=None):
    data = node_input or {}
    result = {"summary": str(data.get("text", ""))}
    log.info("passing through unchanged")
    merge_state(ctx, result)
    return Event(output={**data, **result})
