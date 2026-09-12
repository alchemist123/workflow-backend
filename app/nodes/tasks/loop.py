"""LOOP — a back-edge in the graph, guarded by this node.

ADK graph workflows have no loop construct: a loop is drawn as a cycle, and
ADK's only rule is that the cycle must contain at least one *routed* edge
(`_detect_unconditional_cycles`: "Cycles must include at least one conditional
(routed) edge to avoid infinite loops"). This node supplies that edge — it
routes to `loop_body` to go round again and to `done` to leave — and it holds
the iteration counter, because on re-entry `node_input` is whatever the body
produced rather than what the loop last saw.

ADK imposes no step limit of its own, so `max_iterations` is the only thing
standing between a mis-written condition and a graph that spins forever. It is
a hard stop in both modes, and hitting it marks the result `truncated` instead
of returning a partial answer that looks complete.
"""

from dataclasses import dataclass

from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class LoopNode(NodeDefinition):
    node_type: str = "LOOP"
    uses_named_routes: bool = True
    version: str = "1"
    palette: PaletteMetadata = None
    allows_cycle: bool = True

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Loop",
            category="flow",
            color="#f97316",
            icon="RefreshCw",
            description="Iterate over a list, or repeat until a condition becomes true",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["for_each", "while"],
                    "default": "for_each",
                    "description": (
                        "for_each walks a list; while repeats until the exit "
                        "condition becomes true"
                    ),
                },
                "items_path": {
                    "type": "string",
                    "description": (
                        "Dotted path to the list, e.g. 'data.items'. Required "
                        "for for_each. The list is resolved once on entry and "
                        "pinned, so a body that changes it cannot alter what "
                        "this loop is walking."
                    ),
                },
                "exit_condition": {
                    "type": "string",
                    "description": (
                        "Required for while. A Python expression checked before "
                        "each pass; the loop leaves when it is true. `data` is "
                        "the latest payload and `i` the iteration count, so "
                        "'i >= 5' runs the body five times."
                    ),
                },
                "max_iterations": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10000,
                    "default": 100,
                    "description": (
                        "Hard cap for both modes. Reaching it sets `truncated` "
                        "on the result."
                    ),
                },
            },
            "required": [],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                # On the "done" handle.
                "results": {"type": "array"},
                "iterations": {"type": "integer"},
                "total_items": {"type": "integer"},
                "truncated": {"type": "boolean"},
                # On the "loop_body" handle, for the body to read.
                "current_item": {},
                "current_index": {"type": "integer"},
            },
        }
        # "loop_body" continues the loop body; "done" exits after all iterations
        self.output_handles = ["loop_body", "done"]

