"""TRANSFORM — Shipment.

Generated from canvas node 'shipment'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context

from core.logging import node_logger
from core.mapping import build as build_mapping
from core.state import merge_state
from core.variables import all_variables
from nodes.base import NODE_KWARGS, flow_node, node_error

NODE_ID = "n_transform_3"
# Name this node saves its result under, for later nodes to read.
OUTPUT_VARIABLE = "shipment"
ON_ERROR = "fail"
# The output shape, declared on the canvas. Each entry says where its value
# comes from, so the mapping is data rather than code.
OUTPUT_FIELDS: list[dict] = [{'name': 'product_code', 'source': 'data.sku', 'type': 'string', 'required': True}, {'name': 'units', 'source': 'data.qty', 'type': 'integer', 'required': True}, {'name': 'quantity_label', 'source': 'data.qty', 'type': 'string'}, {'name': 'carrier', 'source': '', 'type': 'string', 'default': 'standard-post'}, {'name': 'gift_message', 'source': 'data.gift_message', 'type': 'string'}]

log = node_logger(NODE_ID)


def _transform(data: dict, vars: dict) -> dict:  # noqa: A002 - `vars` is the canvas name
    """The canvas expression, emitted as ordinary Python.

    `data` is the payload from the previous node; `vars` is every variable a
    node has named, keyed by that name.
    """
    return build_mapping(OUTPUT_FIELDS, data, vars, where=NODE_ID)


@flow_node(name=NODE_ID, variable=OUTPUT_VARIABLE, **NODE_KWARGS(timeout=60))
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
