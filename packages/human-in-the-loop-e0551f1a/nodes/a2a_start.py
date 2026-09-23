"""A2A_START — Expense Request.

Generated from canvas node 'trigger'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context

from core.logging import node_logger
from core.state import coerce_input, replace_state
from nodes.base import NODE_KWARGS, flow_node

NODE_ID = "a2a_start"
# Name this node saves its result under, for later nodes to read.
OUTPUT_VARIABLE = ""

# The payload contract this agent advertises. Rendered from the canvas node's
# payload_schema; the platform's test panel builds its form from the same data.
PAYLOAD_SCHEMA: dict = {'type': 'object', 'properties': {'amount': {'type': 'number', 'description': 'Amount being claimed'}, 'reason': {'type': 'string', 'description': 'What the money is for', 'x-ui-widget': 'textarea'}}, 'required': ['amount', 'reason']}

# An example payload, for the README and for a caller with no schema handling.
EXAMPLE_PAYLOAD: dict = {'amount': 0, 'reason': ''}

log = node_logger(NODE_ID)


@flow_node(name=NODE_ID, variable=OUTPUT_VARIABLE, **NODE_KWARGS(timeout=120))
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
