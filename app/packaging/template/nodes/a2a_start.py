"""A2A_START -- the workflow's only entry node.

This is the one node whose input is not another node's output: the runner hands
it the raw `google.genai.types.Content` of the inbound A2A message.
`coerce_input` normalises that (and a JSON string, and a plain dict) into a
single dict shape, so every node after this one can assume a dict.

`PAYLOAD_SCHEMA` is what the platform's test panel renders its form from. It is
documentation for callers rather than enforcement -- an A2A caller can send
anything, and a workflow that needs strict validation should follow this node
with a validating TRANSFORM.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import coerce_input, replace_state
from nodes.base import NODE_KWARGS

NODE_ID = "a2a_start"

# Rendered from the canvas node's `payload_schema` config.
PAYLOAD_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The text to triage.",
        },
    },
    "required": ["text"],
}

log = node_logger(NODE_ID)


@node(name=NODE_ID, **NODE_KWARGS(timeout=30))
async def a2a_start(ctx: Context, node_input=None):
    payload = coerce_input(node_input)
    log.info("workflow started with %d payload key(s)", len(payload))
    # The inbound payload *is* the initial workflow state.
    replace_state(ctx, payload)
    return Event(output=payload)
