from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class LoopNode(NodeDefinition):
    node_type: str = "LOOP"
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
            description="Iterate over a list or repeat until exit condition",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["for_each", "while"],
                    "description": "for_each iterates over a list; while repeats until condition is false",
                },
                "items_path": {
                    "type": "string",
                    "description": "JSON path to the list when mode=for_each, e.g. 'data.items'",
                },
                "exit_condition": {
                    "type": "string",
                    "description": "Python expression evaluated each iteration for mode=while",
                },
                "max_iterations": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10000,
                    "default": 100,
                    "description": "Hard cap — required for both modes",
                },
            },
            "required": [],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "results": {"type": "array"},
                "iterations": {"type": "integer"},
                "current_item": {},
                "current_index": {"type": "integer"},
            },
        }
        # "loop_body" continues the loop body; "done" exits after all iterations
        self.output_handles = ["loop_body", "done"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        # The runtime engine handles the loop bookkeeping; this returns state metadata.
        return {"data": input_data, "_loop_config": node_config}
