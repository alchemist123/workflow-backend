"""The node register: every graph node in this workflow, by name.

`agent.py` builds edges by looking nodes up here rather than importing each
module directly, which keeps the wiring in one place and lets the tests
enumerate nodes without knowing the workflow.

`NODES` keys are the ADK node names, which are also what appears in
`event.node_info.path` at run time (`<workflow>@1/<node>@<iteration>`).  The
platform maps those back to canvas nodes through `CANVAS_IDS`.
"""

from __future__ import annotations

from typing import Any

from nodes.a2a_start import a2a_start
from nodes.n_end_5 import n_end_5
from nodes.n_normalise_1 import n_normalise_1
from nodes.n_passthrough_4 import n_passthrough_4
from nodes.n_route_2 import n_route_2
from nodes.n_summarise_3 import n_summarise_3

# ── The register ─────────────────────────────────────────────────────────────
NODES: dict[str, Any] = {
    "a2a_start": a2a_start,
    "n_normalise_1": n_normalise_1,
    "n_route_2": n_route_2,
    "n_summarise_3": n_summarise_3,
    "n_passthrough_4": n_passthrough_4,
    "n_end_5": n_end_5,
}

# The entry node, wired to START in agent.py.
ENTRY_NODE = "a2a_start"

# The single terminal node. A workflow must have exactly one.
TERMINAL_NODE = "n_end_5"

# Generated node name -> the canvas node id it came from, so run events can be
# painted back onto the canvas.
CANVAS_IDS: dict[str, str] = {
    "a2a_start": "node_example_start",
    "n_normalise_1": "node_example_normalise",
    "n_route_2": "node_example_route",
    "n_summarise_3": "node_example_summarise",
    "n_passthrough_4": "node_example_passthrough",
    "n_end_5": "node_example_end",
}

# Generated node name -> canvas node type, for status display and debugging.
NODE_TYPES: dict[str, str] = {
    "a2a_start": "A2A_START",
    "n_normalise_1": "TRANSFORM",
    "n_route_2": "CONDITION",
    "n_summarise_3": "FUNCTION",
    "n_passthrough_4": "TRANSFORM",
    "n_end_5": "END",
}


# Generated node name -> the title the canvas gave it, for readable run logs.
NODE_TITLES: dict[str, str] = {
    "a2a_start": "A2A Start",
    "n_normalise_1": "Normalise",
    "n_route_2": "Route by length",
    "n_summarise_3": "Summarise",
    "n_passthrough_4": "Pass through",
    "n_end_5": "End",
}


def get(name: str) -> Any:
    """Look up a node, failing with the available names rather than a KeyError."""
    try:
        return NODES[name]
    except KeyError:
        raise KeyError(
            f"No node named {name!r} in this workflow. "
            f"Available: {', '.join(sorted(NODES))}"
        ) from None


def canvas_id(node_name: str) -> str | None:
    """The canvas node a generated node came from."""
    return CANVAS_IDS.get(node_name)


def node_name_from_path(path: str) -> str | None:
    """Parse a node name out of an ADK event path.

    `event.node_info.path` looks like `text_triage@1/n_normalise_1@2` -- the
    trailing `@N` is the loop iteration. Returns None for the workflow's own
    events, which carry no node segment.
    """
    if not path or "/" not in path:
        return None
    segment = path.rsplit("/", 1)[-1]
    name = segment.rsplit("@", 1)[0] if "@" in segment else segment
    return name if name in NODES else None


def iteration_from_path(path: str) -> int:
    """The loop iteration in an ADK event path; 1 when unsuffixed."""
    if not path or "/" not in path:
        return 1
    segment = path.rsplit("/", 1)[-1]
    if "@" not in segment:
        return 1
    try:
        return int(segment.rsplit("@", 1)[-1])
    except ValueError:
        return 1
