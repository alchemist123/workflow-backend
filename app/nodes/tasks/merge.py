from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class MergeNode(NodeDefinition):
    node_type: str = "MERGE"
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Merge",
            category="flow",
            color="#0d9488",
            icon="Merge",
            description="Wait for every parallel branch, then combine their outputs",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                # No `strategy`: a MERGE compiles to an ADK JoinNode, whose
                # `_requires_all_predecessors` is True and not configurable.
                # It always waits for every branch, so a "continue on the
                # first" setting could only ever have been a control that did
                # nothing. Canvases carrying one are migrated in v6 -> v7.
                "merge_mode": {
                    "type": "string",
                    "enum": ["merge", "array", "first"],
                    "default": "merge",
                    "description": (
                        "How the branch outputs become one payload. "
                        "merge: one object with every branch's keys, later "
                        "branches winning a clash. array: {'results': [...]} "
                        "in branch order. first: only the first branch's "
                        "output."
                    ),
                },
            },
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output"]

