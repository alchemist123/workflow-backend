from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class ConditionNode(NodeDefinition):
    node_type: str = "CONDITION"
    uses_named_routes: bool = True
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Condition",
            category="flow",
            color="#ef4444",
            icon="GitBranch",
            description="Branches execution based on a boolean expression",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Python-safe boolean expression using input fields, e.g. 'data.score > 0.5'",
                },
                "branches": {
                    "type": "array",
                    "description": "Named branches with conditions evaluated in order",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "expression": {"type": "string"},
                        },
                        "required": ["name", "expression"],
                    },
                    "minItems": 1,
                },
            },
            "required": ["branches"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "matched_branch": {"type": "string"},
                "data": {},
            },
        }
        # CONDITION nodes: named branches + a default fallthrough
        self.output_handles = ["true", "false", "default"]

