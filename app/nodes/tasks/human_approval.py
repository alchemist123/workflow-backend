from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class HumanApprovalNode(NodeDefinition):
    node_type: str = "HUMAN_APPROVAL"
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Human Approval",
            category="flow",
            color="#a855f7",
            icon="UserCheck",
            description="Pause execution until a human approves or rejects",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Email addresses of approvers",
                },
                "prompt": {"type": "string", "description": "Message shown to approver"},
                "timeout_seconds": {"type": "integer", "default": 86400},
                "on_timeout": {
                    "type": "string",
                    "enum": ["reject", "approve", "fail"],
                    "default": "reject",
                },
            },
            "required": ["assignees"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "approver": {"type": "string"},
                "comment": {"type": "string"},
                "data": {},
            },
        }
        self.output_handles = ["approved", "rejected"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        # Suspends execution — runtime engine marks execution as WAITING
        return {"_suspend": True, "reason": "human_approval", "data": input_data}
