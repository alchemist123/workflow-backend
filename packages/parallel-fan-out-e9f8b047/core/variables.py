"""Named variables: values a node saves so a later node can use them by name.

## How ADK does it

ADK graphs have no separate variable concept -- variables *are* session state
keys, and the binding is by name. Verified against 2.8.0:

    @node(name="setter")
    async def setter(ctx, node_input=None):
        return Event(output={...}, state={"customer": "ACME", "score": 7})

    @node(name="reader")
    async def reader(ctx, customer=None, score=None, node_input=None):
        ...  # customer == "ACME", score == 7

`Event(state=...)` writes the delta, and a node's parameters are bound from
`ctx.state` by name (`parameter_binding='state'`, the default). `node_input` is
special-cased as the edge payload rather than a state key, and the session ends
up holding a flat `{'customer': 'ACME', 'score': 7}`.

So variables here are **flat, top-level state keys**, which is also what
`LlmAgent.output_key` already writes. They are readable by ADK's own binding
and by anything else sharing the session.

## What that costs, and the guard

A flat namespace can collide. The workflow's own accumulated payload lives
under `wf`, a loop keeps counters under `_loop_<node>_*`, and ADK reserves
anything containing `:` (`app:`, `user:`, `temp:`). `RESERVED_PREFIXES` and
`is_valid_name` keep canvas variables out of all of it; the compiler rejects a
bad name rather than letting it quietly shadow something.
"""

from __future__ import annotations

import re
from typing import Any

# The workflow's own accumulated payload, and a loop's iteration bookkeeping.
# A canvas variable may not take these.
RESERVED_NAMES = frozenset({"wf"})
RESERVED_PREFIXES = ("_loop_", "app:", "user:", "temp:", "_")

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def is_valid_name(name: str) -> bool:
    """Whether `name` is usable as a variable.

    Identifier-shaped, because ADK binds node parameters by name and a
    parameter cannot be called `my var`. Nothing reserved, and no `:` -- ADK
    treats a colon as a scope prefix and skips schema validation for it.
    """
    name = (name or "").strip()
    if not name or not _NAME.match(name):
        return False
    if name in RESERVED_NAMES:
        return False
    return not name.startswith(RESERVED_PREFIXES)


def set_variable(ctx: Any, name: str, value: Any) -> None:
    """Save `value` under `name`, as a top-level state key.

    Writing through `ctx.state` records a delta that ADK attaches to the event,
    so the session service persists it and a later node reads it by name.
    """
    if not is_valid_name(name):
        return
    ctx.state[name] = value


def all_variables(ctx: Any) -> dict[str, Any]:
    """Every canvas variable currently set, by name.

    Filtered rather than raw state: the workflow's own `wf` payload, a loop's
    counters and ADK's prefixed keys are not variables anyone named, and
    showing them in an expression's scope would invite writing against
    internals.
    """
    try:
        state = ctx.state.to_dict()
    except Exception:  # noqa: BLE001 - never break a run over introspection
        return {}
    return {
        key: value
        for key, value in state.items()
        if is_valid_name(key)
    }
