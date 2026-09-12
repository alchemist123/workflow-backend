"""The workflow graph.

Generated from the canvas. The edge list is the whole topology, so this file is
the one place to look to understand what the package does.

Edge forms the compiler emits:

    (START, node_a, node_b, node_c)     a sequential chain
    (router, {"x": node_x, ...})        conditional dispatch on Event(route=...)
    Edge(from_node=a, to_node=b,        several route values to one target --
         route=["x", "y"])              required, because two edges sharing the
                                        same (from, to) pair are rejected as
                                        "Duplicate edge found"
    (fork, left), (fork, right)         fan-out
    (left, join), (right, join)         fan-in via JoinNode
    (guard, {"AGAIN": body, ...})       a loop, as a back-edge to an earlier node

Invariants the compiler guarantees, both enforced by ADK:
  * exactly one node wired from START, and exactly one terminal node
  * no two edges share a (from_node, to_node) pair
"""

from __future__ import annotations

from google.adk import Workflow
from google.adk.workflow import START

from core.config import settings
from nodes.registry import NODES

# ── Nodes ─────────────────────────────────────────────────────────────────────
a2a_start = NODES["a2a_start"]
n_normalise_1 = NODES["n_normalise_1"]
n_route_2 = NODES["n_route_2"]
n_summarise_3 = NODES["n_summarise_3"]
n_passthrough_4 = NODES["n_passthrough_4"]
n_end_5 = NODES["n_end_5"]

# ── Graph ─────────────────────────────────────────────────────────────────────
root_agent = Workflow(
    name=settings.AGENT_NAME,
    description=settings.AGENT_DESCRIPTION,
    edges=[
        # entry -> normalise -> route
        (START, a2a_start, n_normalise_1, n_route_2),
        # route branches on word count
        (
            n_route_2,
            {
                "long": n_summarise_3,
                "short": n_passthrough_4,
            },
        ),
        # both branches reconverge on the single terminal node
        (n_summarise_3, n_end_5),
        (n_passthrough_4, n_end_5),
    ],
)
