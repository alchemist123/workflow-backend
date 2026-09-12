"""Orchestrator Agent node — AI agent that uses MCP tools, remote A2A agents, and inline functions."""
from dataclasses import dataclass
from typing import Any
from app.nodes.agent_io import io_config_properties
from app.nodes.base import NodeDefinition, PaletteMetadata

_SAFE_BUILTINS = {
    "__builtins__": {
        k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
        for k in ("range", "len", "str", "int", "float", "bool", "list", "dict",
                  "tuple", "set", "isinstance", "hasattr", "getattr", "enumerate",
                  "zip", "map", "filter", "sorted", "sum", "min", "max", "abs", "round")
    }
}


@dataclass
class OrchestratorAgentNode(NodeDefinition):
    node_type: str = "ORCHESTRATOR_AGENT"
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
            label="Orchestrator Agent",
            category="ai",
            color="#f59e0b",
            icon="BrainCircuit",
            description="AI agent that orchestrates MCP tools, remote agents & functions",
            wave=1,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "model": {"type": "string", "default": "gemini-2.0-flash"},
                "system_prompt": {"type": "string"},
                "max_iterations": {"type": "integer", "default": 10},
                "output_field": {
                    "type": "string",
                    "description": "If set, extract this key from the agent result before passing downstream",
                },
                "functions": {
                    "type": "array",
                    "description": "Inline Python functions (alternative to connecting FUNCTION nodes)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "parameters": {"type": "object"},
                            "code": {"type": "string"},
                        },
                        "required": ["name", "code"],
                    },
                },
                "api_key": {
                    "type": "string",
                    "description": "Google AI Studio API key — leave empty to use GOOGLE_API_KEY env var or ADC",
                },
                "vertex_project": {
                    "type": "string",
                    "description": "Google Cloud project ID — enables Vertex AI backend for ADK",
                },
                "vertex_location": {
                    "type": "string",
                    "default": "us-central1",
                    "description": "Vertex AI region (ADK only)",
                },
                "service_account_json": {
                    "type": "string",
                    "description": "Service account key JSON for Vertex AI auth — leave empty to use ADC",
                },
                **io_config_properties(include_input=False),
                # resolved_tools is injected by the IR compiler from connected nodes
                "resolved_tools": {"type": "object"},
            },
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "result": {},
                "usage": {
                    "type": "object",
                    "properties": {
                        "input_tokens": {"type": "integer"},
                        "output_tokens": {"type": "integer"},
                    },
                },
            },
        }
        self.output_handles = ["output", "error"]

