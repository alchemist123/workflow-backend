"""TRANSFORM — Invoice Line.

Generated from canvas node 'invoice'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context

from core.logging import node_logger
from core.state import merge_state
from core.variables import all_variables
from nodes.base import NODE_KWARGS, flow_node, node_error

NODE_ID = "n_transform_2"
# Name this node saves its result under, for later nodes to read.
OUTPUT_VARIABLE = "invoice"
ON_ERROR = "fail"
# The output shape, declared on the canvas. Each entry says where its value
# comes from, so the mapping is data rather than code.
OUTPUT_FIELDS: list[dict] = [{'name': 'item', 'source': 'data.product_code', 'type': 'string', 'required': True}, {'name': 'quantity', 'source': 'data.units', 'type': 'integer', 'required': True}, {'name': 'ordered_sku', 'source': 'vars.order.sku', 'type': 'string'}, {'name': 'currency', 'source': '', 'type': 'string', 'default': 'EUR'}]

log = node_logger(NODE_ID)


_PY_TYPES = {
    "string": str, "text": str, "number": float,
    "integer": int, "boolean": bool, "object": dict, "array": list,
}


def _resolve(path: str, data: dict, variables: dict):
    """Follow a mapping path, returning (value, found).

    `found` is separate from the value because `None` and `0` are legitimate
    values — treating a falsy result as "missing" would quietly swap a real
    zero for a default.
    """
    parts = [p for p in (path or "").split(".") if p]
    if not parts:
        return None, False

    head, *rest = parts
    if head == "vars":
        if not rest:
            return variables, True
        current: object = variables
    elif head == "data":
        current = data
    else:
        # A bare name reads from the payload, so 'qty' and 'data.qty' agree.
        current, rest = data, parts

    for part in rest:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None, False
    return current, True


def _coerce(value, target: str | None):
    """Best-effort cast to the declared type, leaving the value alone if it cannot."""
    caster = _PY_TYPES.get(target or "")
    if caster is None or value is None or isinstance(value, caster):
        return value
    try:
        if caster is bool and isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1", "on")
        return caster(value)
    except (TypeError, ValueError):
        # A declared type is a statement of intent, not a reason to fail a run.
        log.warning(
            "could not convert %r to %s; passing it through unchanged",
            value, target,
        )
        return value


def _transform(data: dict, vars: dict) -> dict:  # noqa: A002 - `vars` is the canvas name
    """The canvas expression, emitted as ordinary Python.

    `data` is the payload from the previous node; `vars` is every variable a
    node has named, keyed by that name.
    """
    out: dict = {}
    for field in OUTPUT_FIELDS:
        value, found = _resolve(field.get("source") or "", data, vars)
        if not found:
            if "default" in field:
                value = field["default"]
            elif field.get("required"):
                raise ValueError(
                    f"{NODE_ID}: required field {field['name']!r} has no value — "
                    f"{field.get('source')!r} was not present in the input."
                )
            else:
                continue
        out[field["name"]] = _coerce(value, field.get("type"))
    return out


@flow_node(name=NODE_ID, variable=OUTPUT_VARIABLE, **NODE_KWARGS(timeout=60))
async def n_transform_2(ctx: Context, node_input=None):
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
