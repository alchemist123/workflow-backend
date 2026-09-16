"""MCP_TOOL: call one named tool on an MCP server, inline in the flow.

The existing TOOL node is a *tool provider*: it is wired into an agent's tools
handle, and a model decides whether and when to call it. This one is the other
half of that idea — an ordinary step in the graph, called every time the flow
reaches it, with arguments the canvas maps rather than a model invents.

Which makes the interesting part the arguments. TOOL sends the whole incoming
payload as the tool's arguments, which works only when the payload happens to
match the tool's schema exactly and fails with a schema error the moment it
carries an extra key. Here the arguments are mapped field by field from what is
actually available upstream, using the same picker and the same compile-time
checking as a TRANSFORM in `fields` mode.

The server's own tool list is fetched from the config panel and cached in
`tool_schema`, which is what makes that checking possible without the compiler
touching the network.
"""

from dataclasses import dataclass
from typing import Any

from app.nodes.agent_io import FIELD_TYPES
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class McpToolNode(NodeDefinition):
    node_type: str = "MCP_TOOL"
    # Not a tool provider: this is a step, not something an agent may call.
    provides_tool: bool = False
    secret_config_keys: frozenset[str] = frozenset({"mcp_url", "auth_token"})
    supports_on_error_continue: bool = True
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="MCP Tool Call",
            category="ai",
            color="#2563eb",
            icon="PlugZap",
            description="Call one tool on an MCP server and pass on its result",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "mcp_url": {
                    "type": "string",
                    "description": (
                        "MCP server URL. A base URL, a /mcp endpoint or a /sse "
                        "endpoint all work — the client tries each."
                    ),
                },
                "auth_token": {
                    "type": "string",
                    "description": "Sent as `Authorization: Bearer ...` when set",
                },
                "tool_name": {
                    "type": "string",
                    "description": "Which tool on that server to call",
                },
                "tool_description": {
                    "type": "string",
                    "description": "What the server said this tool does, for the canvas",
                },
                "tool_schema": {
                    "type": "object",
                    "description": (
                        "The tool's input schema as the server reported it, "
                        "cached so the compiler can check the arguments without "
                        "reaching the network"
                    ),
                },
                "arg_mode": {
                    "type": "string",
                    "enum": ["fields", "passthrough"],
                    "default": "fields",
                    "description": (
                        "fields: map each argument from what is available "
                        "upstream. passthrough: send the incoming payload as "
                        "the arguments, unchanged."
                    ),
                },
                "arg_fields": {
                    "type": "array",
                    "description": "One entry per argument, saying where its value comes from",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": list(FIELD_TYPES)},
                            "source": {"type": "string"},
                            "default": {},
                            "required": {"type": "boolean"},
                        },
                        "required": ["name"],
                    },
                },
                "result_key": {
                    "type": "string",
                    "description": (
                        "Put the tool's result under this key instead of merging "
                        "its keys into the payload"
                    ),
                },
            },
            "required": ["mcp_url", "tool_name"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output", "error"]
