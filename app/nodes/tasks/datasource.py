from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class DataSourceNode(NodeDefinition):
    node_type: str = "DATASOURCE"
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

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        import httpx

        mcp_url: str = node_config.get("mcp_url", "")
        tool_name: str = node_config.get("tool_name", "")
        static_args: dict = node_config.get("tool_args") or {}
        auth: dict = node_config.get("auth") or {}

        # Optionally derive args from input via JMESPath
        args_jmespath: str | None = node_config.get("args_jmespath")
        if args_jmespath:
            import jmespath
            derived = jmespath.search(args_jmespath, input_data) or {}
            arguments = {**static_args, **(derived if isinstance(derived, dict) else {})}
        else:
            arguments = {**static_args, **input_data}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                mcp_url,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
                headers=auth,
            )
            data = resp.json()
            return data.get("result", data)
