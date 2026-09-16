"""Give a tool wrapper a real signature, so the model can pass it arguments.

ADK builds a tool's declaration from its function signature. A wrapper written
as `async def impl(**kwargs)` therefore declares **no parameters at all** — the
model sees the tool's name and description but has no way to send it anything,
so it calls `search()` with nothing and the tool fails or returns garbage. That
is silent: nothing raises, and the declaration is simply thin.

The fix is to attach a synthesized `inspect.Signature` (and matching
`__annotations__`) built from the schema the tool actually accepts. ADK reads
those through `inspect.signature`, which honours `__signature__`, and produces
the full parameter schema:

    {"properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
     "required": ["query"]}

Used for MCP tools (whose `inputSchema` comes from the server) and for inline
canvas functions (whose `parameters` come from the node config).
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

# JSON Schema type → the Python annotation ADK turns back into that type.
_PY_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _annotation_for(spec: Any) -> Any:
    """The Python annotation for one JSON Schema property."""
    if not isinstance(spec, dict):
        return Any
    declared = spec.get("type")
    if isinstance(declared, list):
        # A union like ["string", "null"]: take the first concrete type.
        declared = next((t for t in declared if t != "null"), None)
    return _PY_TYPES.get(declared, Any)


def apply_schema_signature(
    func: Callable[..., Any],
    schema: dict[str, Any] | None,
    *,
    fallback_param: str | None = None,
) -> Callable[..., Any]:
    """Attach a signature derived from `schema` to a `**kwargs` wrapper.

    `schema` is a JSON Schema object; only its top-level `properties` and
    `required` are used, which is all a function declaration can express.

    When the schema has no properties, `fallback_param` (if given) becomes a
    single required string parameter. That matters because a tool with no
    parameters is one the model can only call empty-handed — for an MCP tool
    that genuinely takes no input that is correct, but for one whose schema we
    could not read it is better to accept a free-text request than nothing.

    Returns the same function, mutated. Safe on a function that already has an
    explicit signature: it is simply overwritten.
    """
    properties: dict[str, Any] = {}
    required: set[str] = set()

    if isinstance(schema, dict):
        raw_properties = schema.get("properties")
        if isinstance(raw_properties, dict):
            properties = raw_properties
        raw_required = schema.get("required")
        if isinstance(raw_required, (list, tuple, set)):
            required = {str(name) for name in raw_required}

    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}

    if properties:
        # Required parameters must come first; a parameter with a default
        # followed by one without is not a valid signature.
        ordered = sorted(properties, key=lambda name: name not in required)
        for name in ordered:
            if not name.isidentifier():
                # Not expressible as a Python parameter, so the model cannot be
                # offered it. Skipped rather than crashing the whole tool.
                continue
            annotation = _annotation_for(properties[name])
            annotations[name] = annotation
            parameters.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=(
                        inspect.Parameter.empty if name in required else None
                    ),
                    annotation=annotation,
                )
            )
    elif fallback_param:
        annotations[fallback_param] = str
        parameters.append(
            inspect.Parameter(
                fallback_param, inspect.Parameter.KEYWORD_ONLY, annotation=str
            )
        )

    annotations["return"] = dict
    func.__signature__ = inspect.Signature(parameters, return_annotation=dict)
    func.__annotations__ = annotations
    return func


def safe_tool_name(*parts: str) -> str:
    """A valid Python identifier, since it becomes the name the model sees."""
    raw = "__".join(part for part in parts if part)
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in raw)
    cleaned = cleaned.strip("_")
    if not cleaned:
        return "tool"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned
