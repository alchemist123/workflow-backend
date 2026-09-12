"""CONDITION — Needs approval?.

Generated from canvas node 'size'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import merge_state
from nodes.base import NODE_KWARGS, route_event

NODE_ID = "n_condition_6"

# Branch names, in canvas order. Each is matched against the keys of the dict on
# this node's outgoing edge in agent.py.
BRANCHES: list[str] = ['large', 'small']
DEFAULT_ROUTE = None

log = node_logger(NODE_ID)


def _evaluate(data: dict) -> str | None:
    """The canvas branch expressions, in order. First match wins."""
    # large: data.get('amount', 0) >= 1000
    try:
        if data.get('amount', 0) >= 1000:
            return "large"
    except Exception:  # noqa: BLE001 - a branch that cannot be evaluated is not a match
        log.warning("branch %r could not be evaluated", "large")
    # small: True
    try:
        if True:
            return "small"
    except Exception:  # noqa: BLE001 - a branch that cannot be evaluated is not a match
        log.warning("branch %r could not be evaluated", "small")
    return None


@node(name=NODE_ID, **NODE_KWARGS(timeout=120))
async def n_condition_6(ctx: Context, node_input=None):
    data = node_input or {}
    branch = _evaluate(data) or DEFAULT_ROUTE
    if branch is None:
        # No branch matched and no default edge exists, so the graph would
        # dead-end here. Fail loudly rather than hang the A2A task.
        raise ValueError(
            f"{NODE_ID}: no branch matched and no default route is wired. "
            f"Branches: {BRANCHES}"
        )
    log.info("routing to %r", branch)
    merge_state(ctx, {"_branch": branch})
    return route_event(branch, data)
