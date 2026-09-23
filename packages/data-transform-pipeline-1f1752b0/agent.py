"""The workflow graph for Data Transform Pipeline.

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
    a2a_start                A2A_START  (canvas 'trigger' — Start)
    n_end_1                  END  (canvas 'end' — Done)
    n_transform_2            TRANSFORM  (canvas 'extract' — Extract Fields)
    n_transform_3            TRANSFORM  (canvas 'format' — Format Output)
"""

from __future__ import annotations

from google.adk import Workflow
from google.adk.workflow import START

from core.config import settings
from nodes.registry import NODES

# ── Nodes ─────────────────────────────────────────────────────────────────────
a2a_start = NODES["a2a_start"]
n_end_1 = NODES["n_end_1"]
n_transform_2 = NODES["n_transform_2"]
n_transform_3 = NODES["n_transform_3"]

# ── Graph ─────────────────────────────────────────────────────────────────────
# AGENT_NAME must be a valid Python identifier: ADK validates a Workflow's name
# as a node name. The generator defaults it to "data_transform_pipeline".
root_agent = Workflow(
    name=settings.AGENT_NAME,
    description=settings.AGENT_DESCRIPTION,
    edges=[
        (START, a2a_start),
        (a2a_start, n_transform_2),
        (n_transform_2, n_transform_3),
        (n_transform_3, n_end_1),
    ],
)
