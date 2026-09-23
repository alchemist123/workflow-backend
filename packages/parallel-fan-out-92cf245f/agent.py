"""The workflow graph for Parallel Fan-out.

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
    a2a_start                A2A_START  (canvas 'trigger' — Order)
    n_end_3                  END  (canvas 'done' — Done)
    n_merge_5                MERGE  (canvas 'join' — Join)
    n_parallel_fork_4        PARALLEL_FORK  (canvas 'fork' — Fan Out)
    n_transform_1            TRANSFORM  (canvas 'audit' — Audit)
    n_transform_2            TRANSFORM  (canvas 'discount' — Discount)
    n_transform_6            TRANSFORM  (canvas 'shipping' — Shipping)
    n_transform_7            TRANSFORM  (canvas 'tax' — Tax)
    n_transform_8            TRANSFORM  (canvas 'total' — Grand Total)
"""

from __future__ import annotations

from google.adk import Workflow
from google.adk.workflow import START

from core.config import settings
from nodes.registry import NODES

# ── Nodes ─────────────────────────────────────────────────────────────────────
a2a_start = NODES["a2a_start"]
n_end_3 = NODES["n_end_3"]
n_merge_5 = NODES["n_merge_5"]
n_parallel_fork_4 = NODES["n_parallel_fork_4"]
n_transform_1 = NODES["n_transform_1"]
n_transform_2 = NODES["n_transform_2"]
n_transform_6 = NODES["n_transform_6"]
n_transform_7 = NODES["n_transform_7"]
n_transform_8 = NODES["n_transform_8"]

# ── Graph ─────────────────────────────────────────────────────────────────────
# AGENT_NAME must be a valid Python identifier: ADK validates a Workflow's name
# as a node name. The generator defaults it to "parallel_fan_out".
root_agent = Workflow(
    name=settings.AGENT_NAME,
    description=settings.AGENT_DESCRIPTION,
    edges=[
        (START, a2a_start),
        (a2a_start, n_parallel_fork_4),
        (n_merge_5, n_transform_8),
        (n_parallel_fork_4, n_transform_7),
        (n_parallel_fork_4, n_transform_6),
        (n_parallel_fork_4, n_transform_2),
        (n_parallel_fork_4, n_transform_1),
        (n_transform_1, n_merge_5),
        (n_transform_2, n_merge_5),
        (n_transform_6, n_merge_5),
        (n_transform_7, n_merge_5),
        (n_transform_8, n_end_3),
    ],
)
