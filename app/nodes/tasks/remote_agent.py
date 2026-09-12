"""Remote Agent node — wraps an A2A-compatible remote agent endpoint."""
from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class RemoteAgentNode(NodeDefinition):
    node_type: str = "REMOTE_AGENT"
    provides_tool: bool = True
    secret_config_keys: frozenset[str] = frozenset({'auth_token', 'endpoint'})
    supports_on_error_continue: bool = True
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

