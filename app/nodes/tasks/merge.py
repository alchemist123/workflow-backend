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
            description="Wait for and combine outputs from parallel branches",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "enum": ["wait_all", "wait_first"],
                    "default": "wait_all",
                    "description": "wait_all waits for every upstream branch; wait_first continues on the first",
                },
                "merge_mode": {
                    "type": "string",
                    "enum": ["merge", "array", "first"],
                    "default": "merge",
                },
            },
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        return input_data
