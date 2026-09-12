"""END — end.

Generated from canvas node 'end'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import read_state
from nodes.base import NODE_KWARGS, terminal_event

NODE_ID = "n_end_2"

# {source_key: output_key}. Empty means the whole accumulated state is returned.
OUTPUT_MAPPING: dict[str, str] = {'results': 'results'}

log = node_logger(NODE_ID)


@node(name=NODE_ID, **NODE_KWARGS(timeout=60))
async def n_end_2(ctx: Context, node_input=None):
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
