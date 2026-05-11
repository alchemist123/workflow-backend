from dataclasses import dataclass
from typing import Any
from datetime import datetime
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class ScheduleTriggerNode(NodeDefinition):
    node_type: str = "SCHEDULE_TRIGGER"
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
            label="Schedule Trigger",
            category="triggers",
            color="#8b5cf6",
            icon="Clock",
            description="Starts workflow on a cron schedule",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "cron": {"type": "string", "description": "Cron expression e.g. '0 9 * * 1-5'"},
                "timezone": {"type": "string", "default": "UTC"},
                "enabled": {"type": "boolean", "default": True},
            },
            "required": ["cron"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "triggered_at": {"type": "string", "format": "date-time"},
                "cron": {"type": "string"},
            },
        }
        self.output_handles = ["output"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        return {
            "triggered_at": datetime.utcnow().isoformat(),
            "cron": node_config.get("cron", ""),
        }
