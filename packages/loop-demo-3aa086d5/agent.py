"""The workflow graph for Loop Demo.

Generated from the canvas. The edge list below is the whole topology, so this
file is the one place to look to understand what the package does.

Edge forms:
    (a, b)                              a plain edge
    Edge(from_node=a, to_node=b,        a conditional edge; `route` is matched
         route="x")                     against the value the source node
                                        returns as Event(route=...)
    Edge(..., route=["x", "y"])         several routes to one target, merged
                                        because ADK rejects two edges sharing
                                        the same (from, to) pair

Invariants the compiler guarantees, both enforced by ADK:
  * exactly one node wired from START, and exactly one terminal node
  * no two edges share a (from_node, to_node) pair

Canvas node -> graph node:
    a2a_start                A2A_START  (canvas 'start' — start)
    n_end_2                  END  (canvas 'end' — end)
    n_loop_3                 LOOP  (canvas 'loop' — loop)
    n_transform_1            TRANSFORM  (canvas 'body' — body)
"""

from __future__ import annotations

from google.adk import Workflow
from google.adk.workflow import START, Edge

from core.config import settings
from nodes.registry import NODES

# ── Nodes ─────────────────────────────────────────────────────────────────────
a2a_start = NODES["a2a_start"]
n_end_2 = NODES["n_end_2"]
n_loop_3 = NODES["n_loop_3"]
n_transform_1 = NODES["n_transform_1"]

# ── Graph ─────────────────────────────────────────────────────────────────────
# AGENT_NAME must be a valid Python identifier: ADK validates a Workflow's name
# as a node name. The generator defaults it to "loop_demo".
root_agent = Workflow(
    name=settings.AGENT_NAME,
    description=settings.AGENT_DESCRIPTION,
    edges=[
        (START, a2a_start),
        (a2a_start, n_loop_3),
        Edge(from_node=n_loop_3, to_node=n_transform_1, route="loop_body"),
        Edge(from_node=n_loop_3, to_node=n_end_2, route="done"),
        (n_transform_1, n_loop_3),
    ],
)
