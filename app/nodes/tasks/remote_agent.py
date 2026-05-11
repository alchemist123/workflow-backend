"""Remote Agent node — wraps an A2A-compatible remote agent endpoint."""
from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class RemoteAgentNode(NodeDefinition):
    node_type: str = "REMOTE_AGENT"
    version: str = "1"
    palette: PaletteMetadata = None
    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Remote Agent",
            category="ai",
            color="#6366f1",
            icon="ExternalLink",
            description="A2A remote agent — connect to Orchestrator as a tool",
            wave=1,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Agent alias (used as tool name in the orchestrator)",
                },
                "endpoint": {
                    "type": "string",
                    "description": "A2A agent base URL (e.g. http://my-agent/a2a)",
                },
                "description": {
                    "type": "string",
                    "description": "What this agent does — shown to the LLM as the tool description",
                },
                "auth_token": {
                    "type": "string",
                    "description": "Bearer token for the agent endpoint (optional)",
                },
            },
            "required": ["endpoint"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output", "error"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        """Standalone execution: forward input as a message to the A2A endpoint."""
        import httpx
        import json as _json

        endpoint = node_config.get("endpoint", "").rstrip("/")
        auth_token = node_config.get("auth_token", "")
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        message = input_data.get("message", _json.dumps(input_data))

        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.post(endpoint, json={"message": message}, headers=headers)
            resp.raise_for_status()
            return resp.json()
