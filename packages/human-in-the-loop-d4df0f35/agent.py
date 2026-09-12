"""The workflow graph for Human In The Loop.

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
    a2a_start                A2A_START  (canvas 'trigger' — Expense Request)
    n_condition_7            CONDITION  (canvas 'size' — Needs approval?)
    n_end_4                  END  (canvas 'end' — End)
    n_human_approval_5       HUMAN_APPROVAL  (canvas 'gate' — Finance Sign-off)
    n_human_input_3          HUMAN_INPUT  (canvas 'details' — Payment Details)
    n_transform_1            TRANSFORM  (canvas 'auto' — Auto-approve)
    n_transform_2            TRANSFORM  (canvas 'declined' — Record Decline)
    n_transform_6            TRANSFORM  (canvas 'paid' — Record Payment)
"""

from __future__ import annotations

from google.adk import Workflow
from google.adk.workflow import START, Edge

from core.config import settings
from nodes.registry import NODES

# ── Nodes ─────────────────────────────────────────────────────────────────────
a2a_start = NODES["a2a_start"]
n_condition_7 = NODES["n_condition_7"]
n_end_4 = NODES["n_end_4"]
n_human_approval_5 = NODES["n_human_approval_5"]
n_human_input_3 = NODES["n_human_input_3"]
n_transform_1 = NODES["n_transform_1"]
n_transform_2 = NODES["n_transform_2"]
n_transform_6 = NODES["n_transform_6"]

# ── Graph ─────────────────────────────────────────────────────────────────────
# AGENT_NAME must be a valid Python identifier: ADK validates a Workflow's name
# as a node name. The generator defaults it to "human_in_the_loop".
root_agent = Workflow(
    name=settings.AGENT_NAME,
    description=settings.AGENT_DESCRIPTION,
    edges=[
        (START, a2a_start),
        (a2a_start, n_condition_7),
        Edge(from_node=n_condition_7, to_node=n_human_approval_5, route="large"),
        Edge(from_node=n_condition_7, to_node=n_transform_1, route="small"),
        Edge(from_node=n_human_approval_5, to_node=n_human_input_3, route="approved"),
        Edge(from_node=n_human_approval_5, to_node=n_transform_2, route="rejected"),
        (n_human_input_3, n_transform_6),
        (n_transform_1, n_end_4),
        (n_transform_2, n_end_4),
        (n_transform_6, n_end_4),
    ],
)
