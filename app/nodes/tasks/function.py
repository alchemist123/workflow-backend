"""Function node — inline Python function that runs as a standalone step or as an agent tool."""
from dataclasses import dataclass
from typing import Any
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
class FunctionNode(NodeDefinition):
    node_type: str = "FUNCTION"
    version: str = "1"
    palette: PaletteMetadata = None
    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Function",
            category="ai",
            color="#8b5cf6",
            icon="Code2",
            description="Inline Python function — use standalone or connect to Orchestrator as a tool",
            wave=1,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Function / tool name",
                },
                "description": {
                    "type": "string",
                    "description": "What the function does — shown to the LLM",
                },
                "parameters": {
                    "type": "object",
                    "description": "JSON Schema for inputs (shown to LLM when used as a tool)",
                },
                "code": {
                    "type": "string",
                    "description": "Python code. Use 'data' for input dict; set 'result' for output.",
                },
            },
            "required": ["code"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output", "error"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        code = node_config.get("code", "result = data")
        local_ns = {**input_data, "data": input_data, "input_data": input_data}
        exec(compile(code, "<function>", "exec"), dict(_SAFE_BUILTINS), local_ns)
        result = local_ns.get("result", local_ns.get("output"))
        if isinstance(result, dict):
            return result
        return {"result": result}
