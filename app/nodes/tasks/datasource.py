from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class DataSourceNode(NodeDefinition):
    node_type: str = "DATASOURCE"
    provides_tool: bool = True
    secret_config_keys: frozenset[str] = frozenset({'mcp_url', 'auth_token'})
    supports_on_error_continue: bool = True
    version: str = "1"
    palette: PaletteMetadata = None
    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Data Source",
            category="data",
            color="#0ea5e9",
            icon="Database",
            description="Query data via an MCP server tool",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "mcp_url": {
                    "type": "string",
                    "description": "MCP server base URL (JSON-RPC over HTTP)",
                },
                "tool_name": {
                    "type": "string",
                    "description": "MCP tool name to invoke",
                },
                "tool_args": {
                    "type": "object",
                    "description": "Static arguments merged with input data",
                },
                "args_jmespath": {
                    "type": "string",
                    "description": "JMESPath expression to build tool args from input (overrides tool_args)",
                },
                "auth": {
                    "type": "object",
                    "description": "Optional HTTP auth headers",
                },
            },
            "required": ["mcp_url", "tool_name"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output", "error"]

