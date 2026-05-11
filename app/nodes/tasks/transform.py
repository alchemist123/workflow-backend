from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class TransformNode(NodeDefinition):
    node_type: str = "TRANSFORM"
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Transform",
            category="flow",
            color="#64748b",
            icon="Shuffle",
            description="Reshape, filter, or enrich data between nodes",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["jmespath", "jinja2", "python"],
                    "default": "jmespath",
                },
                "expression": {
                    "type": "string",
                    "description": "Transformation expression (JMESPath query, Jinja2 template, or safe Python)",
                },
                "output_key": {
                    "type": "string",
                    "description": "Wrap result in this key; omit to return result directly",
                },
            },
            "required": ["mode", "expression"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        mode = node_config.get("mode", "jmespath")
        expression = node_config.get("expression", "")
        output_key = node_config.get("output_key")
        result: Any = None

        if mode == "jmespath":
            import jmespath  # type: ignore
            result = jmespath.search(expression, input_data)
        elif mode == "jinja2":
            from jinja2 import Environment, sandbox
            env = sandbox.SandboxedEnvironment()
            # Expose a json_true/json_false helper so templates can emit valid JSON booleans
            env.globals["true"] = "true"
            env.globals["false"] = "false"
            template = env.from_string(expression)
            rendered = template.render(**input_data)
            # If the rendered output looks like JSON, parse it so downstream nodes get a dict
            import json as _json
            try:
                result = _json.loads(rendered)
            except (_json.JSONDecodeError, ValueError):
                result = rendered
        elif mode == "python":
            import datetime as _dt
            safe_globals: dict = {
                "__builtins__": {
                    "len": len, "str": str, "int": int, "float": float, "bool": bool,
                    "list": list, "dict": dict, "tuple": tuple, "set": set,
                    "True": True, "False": False, "None": None,
                    "range": range, "enumerate": enumerate, "zip": zip,
                    "all": all, "any": any, "sum": sum, "min": min, "max": max,
                    "sorted": sorted, "reversed": reversed, "map": map, "filter": filter,
                    "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
                    "print": print, "repr": repr, "abs": abs, "round": round,
                },
                "datetime": _dt.datetime,
            }
            safe_locals = {"data": input_data, "result": None}
            exec(expression, safe_globals, safe_locals)  # noqa: S102
            result = safe_locals.get("result")

        if output_key:
            return {output_key: result}
        if isinstance(result, dict):
            return result
        return {"result": result}
