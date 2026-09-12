"""The node register: every graph node in this workflow, by name.

`agent.py` builds its edge list from this mapping rather than importing each
module directly, which keeps the wiring in one place.

`NODES` keys are the ADK node names, which are also what appears in
`event.node_info.path` at run time (`<workflow>@1/<node>@<iteration>`).
`CANVAS_IDS` maps them back to canvas nodes so a run can be painted onto the
canvas.
"""

from __future__ import annotations

from typing import Any
from nodes.a2a_start import a2a_start
from nodes.n_end_2 import n_end_2
from nodes.n_loop_3 import n_loop_3
from nodes.n_transform_1 import n_transform_1

# ── The register ─────────────────────────────────────────────────────────────
NODES: dict[str, Any] = {
    "a2a_start": a2a_start,
    "n_end_2": n_end_2,
    "n_loop_3": n_loop_3,
    "n_transform_1": n_transform_1,
}

# The entry node, wired to START in agent.py.
ENTRY_NODE = "a2a_start"

# The single terminal node. A workflow must have exactly one.
TERMINAL_NODE = "n_end_2"

# Generated node name -> the canvas node it came from.
CANVAS_IDS: dict[str, str] = {
    "a2a_start": "start",
    "n_end_2": "end",
    "n_loop_3": "loop",
    "n_transform_1": "body",
}

# Generated node name -> canvas node type, for status display and debugging.
NODE_TYPES: dict[str, str] = {
    "a2a_start": "A2A_START",
    "n_end_2": "END",
    "n_loop_3": "LOOP",
    "n_transform_1": "TRANSFORM",
}

# Generated node name -> the title the canvas gave it, for readable run logs.
NODE_TITLES: dict[str, str] = {
    "a2a_start": "start",
    "n_end_2": "end",
    "n_loop_3": "loop",
    "n_transform_1": "body",
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

    `event.node_info.path` looks like `wf@1/n_transform_3@2` -- the trailing
    `@N` is the loop iteration. Returns None for the workflow's own events,
    which carry no node segment.
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
