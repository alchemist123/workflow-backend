from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class QueueTriggerNode(NodeDefinition):
    node_type: str = "QUEUE_TRIGGER"
    version: str = "1"
    palette: PaletteMetadata = None
    allows_inbound: bool = False
    is_trigger: bool = True

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Queue Trigger",
            category="triggers",
            color="#f43f5e",
            icon="Inbox",
            description="Starts workflow when a message arrives on a queue",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "queue_name": {"type": "string"},
                "batch_size": {"type": "integer", "minimum": 1, "default": 1},
                "visibility_timeout": {"type": "integer", "default": 30},
            },
            "required": ["queue_name"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "message": {},
                "message_id": {"type": "string"},
                "queue": {"type": "string"},
            },
        }
        self.output_handles = ["output"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        return {
            "message": input_data.get("message"),
            "message_id": input_data.get("message_id", ""),
            "queue": node_config.get("queue_name", ""),
        }
