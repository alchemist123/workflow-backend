from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class SubworkflowNode(NodeDefinition):
    node_type: str = "SUBWORKFLOW"
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Sub-workflow",
            category="flow",
            color="#7c3aed",
            icon="Layers",
            description="Invoke another workflow as a reusable building block",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "version": {"type": "string", "description": "Specific version or 'latest'"},
                "input_mapping": {"type": "object"},
                "wait_for_completion": {"type": "boolean", "default": True},
            },
            "required": ["workflow_id"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output", "error"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        from app.runtime.handlers import run_subworkflow
        return await run_subworkflow(node_config, input_data, context)
