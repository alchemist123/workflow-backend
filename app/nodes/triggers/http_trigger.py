from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class HttpTriggerNode(NodeDefinition):
    node_type: str = "HTTP_TRIGGER"
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
            label="HTTP Trigger",
            category="triggers",
            color="#6366f1",
            icon="Webhook",
            description="Starts workflow via HTTP POST request",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "URL path suffix, e.g. /my-workflow"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH"], "default": "POST"},
                "auth_required": {"type": "boolean", "default": False},
                "response_mode": {
                    "type": "string",
                    "enum": ["immediate", "async"],
                    "default": "async",
                },
            },
            "required": [],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "body": {"type": "object"},
                "headers": {"type": "object"},
                "query": {"type": "object"},
                "method": {"type": "string"},
            },
        }
        self.output_handles = ["output"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        # At runtime this node is activated by the HTTP endpoint — input_data is the request payload.
        return {
            "body": input_data.get("body", {}),
            "headers": input_data.get("headers", {}),
            "query": input_data.get("query", {}),
            "method": input_data.get("method", "POST"),
        }
