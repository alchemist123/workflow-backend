"""END -- the single terminal node.

Two ADK rules meet here, and both are load-bearing:

  * A workflow must have exactly **one** terminal node that produces output.
    Fan-out is fine mid-graph, but every path has to reconverge or the run
    fails during finalisation with "multiple terminal nodes produced output".
  * The terminal event must carry **content**, not just `output`.  The A2A
    executor promotes the aggregated status message's parts into the result
    artifact and only then marks the task `completed`; an event with no parts
    leaves the task at `working` forever.  `terminal_event` does this.
"""

from __future__ import annotations

from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import read_state
from nodes.base import NODE_KWARGS, terminal_event

NODE_ID = "n_end_5"

# Rendered from the canvas END node's `output_mapping`: {source_key: output_key}.
CONFIG: dict = {
    "output_mapping": {
        "summary": "summary",
        "word_count": "word_count",
        "branch": "branch",
    },
}

log = node_logger(NODE_ID)


@node(name=NODE_ID, **NODE_KWARGS(timeout=30))
async def n_end_5(ctx: Context, node_input=None):
    data = {**read_state(ctx), **(node_input or {})}

    mapping = CONFIG.get("output_mapping") or {}
    result = (
        {out_key: data.get(src) for src, out_key in mapping.items()}
        if mapping
        else data
    )

    log.info("workflow finished")
    return terminal_event(result)
