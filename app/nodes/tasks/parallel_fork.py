from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class ParallelForkNode(NodeDefinition):
    node_type: str = "PARALLEL_FORK"
    uses_named_routes: bool = True
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Parallel Fork",
            category="flow",
            color="#0d9488",
            icon="GitFork",
            description="Split execution into parallel branches",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "branches": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Named output branches to activate in parallel",
                    "minItems": 2,
                },
            },
            "required": ["branches"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = []  # dynamic from config.branches

