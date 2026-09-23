"""Building one dict from another, the way the canvas describes it.

A canvas mapping is a list of fields, each saying what it is called, what type
it should be, and where its value comes from — `data.sku`, `vars.order.qty`, or
a constant. Two nodes need exactly that: a TRANSFORM in `fields` mode building
its output, and an MCP_TOOL building the arguments for a tool call.

They are the same operation, so it lives here rather than twice. The
alternative — one copy per template — is how a fix to the coercion rules ends
up applied to transforms and not to tool arguments, which is the kind of
divergence nobody notices until a tool call silently sends a string where the
server wanted an integer.

The mapping is *data*, not generated code: there is no expression to escape, so
a field name cannot inject Python.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("workflow.mapping")

# Canvas field type -> the Python type a value is coerced to.
PY_TYPES: dict[str, type] = {
    "string": str,
    "text": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


class MissingField(ValueError):
    """A required field had no value and no default."""


def resolve(path: str, data: dict, variables: dict) -> tuple[Any, bool]:
    """Follow a mapping path, returning `(value, found)`.

    `found` is separate from the value because `None` and `0` are legitimate
    values — treating a falsy result as "missing" would quietly swap a real
    zero for a default.

    Three heads: `vars.` reads a named variable, `data.` reads the payload on
    the edge, and a bare name reads the payload too, so `qty` and `data.qty`
    agree.
    """
    parts = [p for p in (path or "").split(".") if p]
    if not parts:
        return None, False

    head, *rest = parts
    if head == "vars":
        if not rest:
            return variables, True
        current: Any = variables
    elif head == "data":
        current = data
    else:
        current, rest = data, parts

    for part in rest:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None, False
    return current, True


def coerce(value: Any, target: str | None, *, where: str = "") -> Any:
    """Best-effort cast to the declared type, leaving the value alone if it cannot.

    A declared type is a statement of intent, not a reason to fail a run: `4`
    becoming `"4"` is almost always what was meant, and a value that genuinely
    cannot convert is more useful passed through with a warning than as a
    crash.
    """
    caster = PY_TYPES.get(target or "")
    if caster is None or value is None or isinstance(value, caster):
        return value
    # bool(str) is True for every non-empty string, including "false".
    if caster is bool and isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    try:
        return caster(value)
    except (TypeError, ValueError):
        logger.warning(
            "%scould not convert %r to %s; passing it through unchanged",
            f"{where}: " if where else "",
            value,
            target,
        )
        return value


def build(fields: list[dict], data: dict, variables: dict, *, where: str = "") -> dict:
    """Apply a mapping.

    A field with no value is either an error or an omission, never a null:

    * `required` and absent raises `MissingField`, naming the field and the
      path it looked at;
    * optional and absent is **left out of the result entirely**. A key that is
      present and null is a different contract from an absent key — the next
      node's `"x" in data` is how it asks, and a null answers it wrongly. For
      an MCP tool the distinction is sharper still: sending an explicit null
      fails schema validation on servers that would have accepted the argument
      being omitted.
    """
    out: dict = {}
    for field in fields or []:
        name = (field.get("name") or "").strip()
        if not name:
            continue
        value, found = resolve(field.get("source") or "", data, variables)
        if not found:
            if "default" in field:
                value = field["default"]
            elif field.get("required"):
                raise MissingField(
                    f"{where or 'mapping'}: required field {name!r} has no value — "
                    f"{(field.get('source') or '(no source)')!r} was not present "
                    "in the input."
                )
            else:
                continue
        out[name] = coerce(value, field.get("type"), where=where)
    return out
