"""END — Done.

Generated from canvas node 'done'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk.agents.context import Context

from core.logging import node_logger
from core.state import read_state
from nodes.base import NODE_KWARGS, flow_node, terminal_event

NODE_ID = "n_end_3"
# Name this node saves its result under, for later nodes to read.
OUTPUT_VARIABLE = ""

# {source_key: output_key}. Empty means the whole accumulated state is returned.
OUTPUT_MAPPING: dict[str, str] = {}

log = node_logger(NODE_ID)


@flow_node(name=NODE_ID, variable=OUTPUT_VARIABLE, **NODE_KWARGS(timeout=60))
async def n_end_3(ctx: Context, node_input=None):
    # Accumulated state first, then this path's own output, so a value set by
    # the immediately preceding node wins.
    data = {**read_state(ctx), **(node_input or {})}

    result = (
        {out_key: data.get(src) for src, out_key in OUTPUT_MAPPING.items()}
        if OUTPUT_MAPPING
        else data
    )

    log.info("workflow finished")
    # terminal_event emits content as well as output. That is load-bearing: the
    # A2A executor promotes the status message's parts into the result artifact
    # and only then marks the task `completed`. An event carrying only `output`
    # would leave the task at `working` forever with no artifacts.
    return terminal_event(result)
