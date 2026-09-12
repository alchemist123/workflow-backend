"""LOOP — loop.

Generated from canvas node 'loop'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import merge_state, read_state
from nodes.base import NODE_KWARGS, route_event

NODE_ID = "n_loop_3"

MODE = "for_each"
MAX_ITERATIONS = 100
ITEMS_PATH = "items"

# Route values, matched against this node's outgoing edges in agent.py.
BODY_ROUTE = "loop_body"
DONE_ROUTE = "done"

# Iteration state, which has to survive the back-edge because `node_input` on
# re-entry is whatever the body produced, not what the loop last saw.
#
# Every key is namespaced by node id. A shared key would be overwritten by an
# inner loop, so an outer loop would resume against the inner one's list.
_STATE_PREFIX = f"_loop_{NODE_ID}"
_COUNTER_KEY = f"{_STATE_PREFIX}_i"
_RESULTS_KEY = f"{_STATE_PREFIX}_results"
_ITEMS_KEY = f"{_STATE_PREFIX}_items"

log = node_logger(NODE_ID)


def _items(data: dict) -> list:
    """Resolve ITEMS_PATH ('a.b.c') against the payload."""
    current = data
    for part in [p for p in ITEMS_PATH.split(".") if p]:
        if not isinstance(current, dict):
            return []
        current = current.get(part)
    return current if isinstance(current, list) else []


def _finish(ctx: Context, data: dict, index: int, results: list, extra: dict) -> object:
    """Leave the loop, clearing its state so a later re-entry starts clean.

    A loop can legitimately run more than once in one session -- nested in
    another loop, or on a second message reusing the same context -- and stale
    counters or a stale result list would silently corrupt that run.
    """
    merge_state(ctx, {_COUNTER_KEY: 0, _RESULTS_KEY: [], _ITEMS_KEY: []})
    log.info("loop finished after %d iteration(s)", index)
    return route_event(
        DONE_ROUTE, {**data, "results": results, "iterations": index, **extra}
    )


@node(name=NODE_ID, **NODE_KWARGS(timeout=60))
async def n_loop_3(ctx: Context, node_input=None):
    # A loop is a back-edge in the graph: the body routes back here, and this
    # guard decides whether to go round again.
    data = node_input or {}
    state = read_state(ctx)
    index = int(state.get(_COUNTER_KEY) or 0)
    results = list(state.get(_RESULTS_KEY) or [])

    # Collect the previous iteration's output before deciding again.
    if index > 0:
        results.append(data)
    # The list is resolved once, on entry, and pinned for the whole loop.
    #
    # Re-resolving it every pass would read it back out of the *body's* output,
    # so a body that filters or consumes the list would change what the loop is
    # iterating over half way through -- iterating [1,2,3] with a body that
    # drops the first element visited 1 then 3 and stopped after two passes.
    if index == 0:
        items = _items(data)
        merge_state(ctx, {_ITEMS_KEY: items})
    else:
        items = list(state.get(_ITEMS_KEY) or [])

    total = len(items)
    limit = min(total, MAX_ITERATIONS)

    if index >= limit:
        extra: dict = {"total_items": total}
        if total > MAX_ITERATIONS:
            # Say so in the payload as well as the log: a truncated result that
            # looks complete is worse than a slow loop.
            log.warning(
                "list has %d item(s) but max_iterations is %d; "
                "stopped early and marked the result truncated",
                total,
                MAX_ITERATIONS,
            )
            extra["truncated"] = True
        return _finish(ctx, data, index, results, extra)

    merge_state(ctx, {_COUNTER_KEY: index + 1, _RESULTS_KEY: results})
    log.info("iteration %d of %d", index + 1, limit)
    return route_event(
        BODY_ROUTE,
        {**data, "current_item": items[index], "current_index": index},
    )
