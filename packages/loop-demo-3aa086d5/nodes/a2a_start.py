"""A2A_START — start.

Generated from canvas node 'start'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import coerce_input, replace_state
from nodes.base import NODE_KWARGS

NODE_ID = "a2a_start"

# The payload contract this agent advertises. Rendered from the canvas node's
# payload_schema; the platform's test panel builds its form from the same data.
PAYLOAD_SCHEMA: dict = {'type': 'object', 'properties': {'items': {'type': 'array', 'description': 'Rows to process'}}, 'required': ['items']}

# An example payload, for the README and for a caller with no schema handling.
EXAMPLE_PAYLOAD: dict = {'items': []}

log = node_logger(NODE_ID)


@node(name=NODE_ID, **NODE_KWARGS(timeout=60))
async def a2a_start(ctx: Context, node_input=None):
    # This is the one node whose input is not another node's output: the runner
    # hands it the raw genai Content of the inbound A2A message. coerce_input
    # normalises that (and a JSON string, and a plain dict) into one dict shape,
    # so every node after this can assume a dict.
    payload = coerce_input(node_input)
    log.info("workflow started with %d payload key(s)", len(payload))
    # The inbound payload *is* the initial workflow state.
    replace_state(ctx, payload)
    return Event(output=payload)
