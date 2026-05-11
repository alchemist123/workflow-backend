from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class ConditionNode(NodeDefinition):
    node_type: str = "CONDITION"
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

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        branches = node_config.get("branches", [])
        safe_globals: dict = {"__builtins__": {}}
        safe_locals = {"data": input_data}

        for branch in branches:
            try:
                result = eval(branch["expression"], safe_globals, safe_locals)  # noqa: S307
                if result:
                    # Merge input_data at the top level so downstream nodes access fields directly
                    return {**input_data, "matched_branch": branch["name"], "_branch": branch["name"]}
            except Exception:
                continue

        return {**input_data, "matched_branch": "default", "_branch": "default"}
