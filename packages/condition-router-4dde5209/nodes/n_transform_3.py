"""TRANSFORM — Premium Response.

Enrich response for high-score users

Generated from canvas node 'transform_high'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from jinja2.sandbox import SandboxedEnvironment
from google.adk import Event
from google.adk.agents.context import Context

from core.logging import node_logger
from core.state import merge_state
from core.variables import all_variables
from nodes.base import NODE_KWARGS, flow_node, node_error

NODE_ID = "n_transform_3"
# Name this node saves its result under, for later nodes to read.
OUTPUT_VARIABLE = ""
ON_ERROR = "fail"
TEMPLATE = "{\"tier\": \"premium\", \"message\": \"{{ message }}\", \"score\": {{ score }}, \"perk\": \"10% discount applied\"}"

log = node_logger(NODE_ID)


def _transform(data: dict, vars: dict) -> dict:  # noqa: A002 - `vars` is the canvas name
    """The canvas expression, emitted as ordinary Python.

    `data` is the payload from the previous node; `vars` is every variable a
    node has named, keyed by that name.
    """
    rendered = SandboxedEnvironment().from_string(TEMPLATE).render(vars=vars, **data)
    import json

    try:
        parsed = json.loads(rendered)
    except ValueError:
        return {"result": rendered}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


@flow_node(name=NODE_ID, variable=OUTPUT_VARIABLE, **NODE_KWARGS(timeout=30))
async def n_transform_3(ctx: Context, node_input=None):
    data = node_input or {}
    try:
        result = _transform(data, all_variables(ctx))
    except Exception as exc:  # noqa: BLE001
        if ON_ERROR != "continue":
            raise
        return node_error(NODE_ID, exc)

    log.info("transformed %d key(s) into %d", len(data), len(result))
    merge_state(ctx, result)
    return Event(output={**data, **result})
