from dataclasses import dataclass
from typing import Any
from app.nodes.agent_io import io_config_properties
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class AgentNode(NodeDefinition):
    node_type: str = "AGENT"
    accepts_tools: bool = True
    is_agent: bool = True
    secret_config_keys: frozenset[str] = frozenset({'api_key', 'service_account_json'})
    supports_on_error_continue: bool = True
    version: str = "1"
    palette: PaletteMetadata = None
    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Agent",
            category="ai",
            color="#f59e0b",
            icon="Bot",
            description="AI agent (ADK) with MCP tools and A2A connections",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "default": "gemini-2.0-flash",
                    "description": "Gemini model ID (e.g. gemini-2.0-flash, gemini-1.5-pro)",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "Agent system prompt / instructions",
                },
                "mcp_servers": {
                    "type": "array",
                    "description": "MCP servers to connect as tools or data sources",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Server alias used as tool-name prefix"},
                            "url": {"type": "string", "description": "MCP server base URL"},
                            "transport": {"type": "string", "enum": ["http", "sse"], "default": "http"},
                            "auth": {"type": "object", "description": "Optional HTTP headers for auth"},
                        },
                        "required": ["name", "url"],
                    },
                },
                "a2a_agents": {
                    "type": "array",
                    "description": "Other agents reachable via the A2A protocol",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "endpoint": {"type": "string", "description": "A2A agent endpoint URL"},
                            "description": {"type": "string", "description": "What this agent does (shown to the LLM)"},
                        },
                        "required": ["name", "endpoint"],
                    },
                },
                "max_iterations": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max agentic-loop iterations before giving up",
                },
                "api_key": {
                    "type": "string",
                    "description": "Google AI Studio API key — leave empty to use GOOGLE_API_KEY env var or ADC",
                },
                "vertex_project": {
                    "type": "string",
                    "description": "GCP project ID — enables Vertex AI backend",
                },
                "vertex_location": {
                    "type": "string",
                    "default": "us-central1",
                    "description": "Vertex AI region",
                },
                **io_config_properties(include_input=False),
            },
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "result": {},
                "usage": {"type": "object"},
            },
        }
        self.output_handles = ["output", "error"]

