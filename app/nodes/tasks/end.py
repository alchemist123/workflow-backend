from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class EndNode(NodeDefinition):
    node_type: str = "END"
    version: str = "1"
    palette: PaletteMetadata = None
    allows_outbound: bool = False
    is_terminal: bool = True

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="End",
            category="flow",
            color="#1e293b",
            icon="Square",
            description="Terminates an execution path and captures final output",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "output_mapping": {
                    "type": "object",
                    "description": "Fields to surface as workflow output",
                },
                "status": {
                    "type": "string",
                    "enum": ["success", "failed"],
                    "default": "success",
                },
            },
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = []

