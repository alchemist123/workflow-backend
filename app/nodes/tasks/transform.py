from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class TransformNode(NodeDefinition):
    node_type: str = "TRANSFORM"
    supports_on_error_continue: bool = True
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Transform",
            category="flow",
            color="#64748b",
            icon="Shuffle",
            description="Reshape, filter, or enrich data between nodes",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["jmespath", "jinja2", "python"],
                    "default": "jmespath",
                },
                "expression": {
                    "type": "string",
                    "description": "Transformation expression (JMESPath query, Jinja2 template, or safe Python)",
                },
                "output_key": {
                    "type": "string",
                    "description": "Wrap result in this key; omit to return result directly",
                },
            },
            "required": ["mode", "expression"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output"]

