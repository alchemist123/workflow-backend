from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class WebhookTriggerNode(NodeDefinition):
    node_type: str = "WEBHOOK_TRIGGER"
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
            label="Webhook Trigger",
            category="triggers",
            color="#06b6d4",
            icon="Zap",
            description="Starts workflow via a unique webhook URL",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "secret": {"type": "string", "description": "HMAC secret for signature verification"},
                "verify_signature": {"type": "boolean", "default": True},
                "allowed_ips": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional IP allowlist",
                },
            },
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "body": {"type": "object"},
                "headers": {"type": "object"},
                "signature_verified": {"type": "boolean"},
            },
        }
        self.output_handles = ["output"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        return {
            "body": input_data.get("body", {}),
            "headers": input_data.get("headers", {}),
            "signature_verified": input_data.get("signature_verified", False),
        }
