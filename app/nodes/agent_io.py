"""The input and output structure an agent node can declare.

ADK's `LlmAgent` takes two schema fields, and they are not symmetric — which is
why this module exists rather than one generic converter:

* `input_schema` must be a `type[BaseModel]`. It is documented as "the input
  schema when agent is used as a tool": when the agent is attached to another
  agent as a sub-agent, ADK derives the tool's declared parameters from it. So
  it only has an effect where the agent is *called* by a model.
* `output_schema` accepts a plain dict JSON Schema. Setting it makes ADK request
  `response_mime_type=application/json` with that schema, and — with
  `output_key` — store the *parsed* object in session state.

Both are authored in the canvas as a list of named fields, the same shape
A2A_START uses for `payload_schema`, so the config panel can render one editor
for all of them. The conversion to JSON Schema happens here; the conversion
from JSON Schema to a pydantic model happens in the generated package
(`tools/schema.py`), because only the package has pydantic at hand at the point
the agent is constructed.
"""

from __future__ import annotations

from typing import Any

# Field types offered in the canvas, and the JSON Schema each maps to. Kept in
# step with app/nodes/triggers/a2a_start.py so one editor serves both.
FIELD_TYPES: dict[str, dict] = {
    "string": {"type": "string"},
    "text": {"type": "string"},
    "number": {"type": "number"},
    "integer": {"type": "integer"},
    "boolean": {"type": "boolean"},
    "object": {"type": "object"},
    "array": {"type": "array"},
}

_SAMPLES: dict[str, Any] = {
    "string": "",
    "text": "",
    "number": 0,
    "integer": 0,
    "boolean": False,
    "object": {},
    "array": [],
}


def _fields_property(description: str) -> dict:
    return {
        "type": "object",
        "description": description,
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": sorted(FIELD_TYPES),
                            "default": "string",
                        },
                        "description": {"type": "string"},
                        "required": {"type": "boolean", "default": False},
                    },
                    "required": ["name"],
                },
            },
        },
    }


def io_config_properties(*, include_input: bool = True) -> dict[str, dict]:
    """The `input_structure` / `output_structure` / `output_key` config block.

    Spread into an agent node's `config_schema["properties"]`.

    `include_input=False` for an agent type that cannot be wired into a tools
    handle. ADK only consults `input_schema` when an agent is called as a tool,
    so offering the setting on a flow-only agent would be offering a control
    that can never do anything.
    """
    properties: dict[str, dict] = {}
    if include_input:
        properties["input_structure"] = _fields_property(
            "The arguments this agent takes when another agent calls it as a "
            "tool. ADK turns these into the tool's declared parameters. Leave "
            "empty to accept a single free-text request."
        )
    return {
        **properties,
        "output_structure": _fields_property(
            "The shape this agent must reply in. The model is constrained to "
            "return exactly this JSON. Leave empty for free-form text."
        ),
        "output_key": {
            "type": "string",
            "description": (
                "Store this agent's reply in workflow state under this key. "
                "With an output structure set, the parsed object is stored."
            ),
        },
    }


def fields_to_json_schema(structure: dict | None) -> dict | None:
    """Convert a canvas field list to JSON Schema, or None if there are none.

    None rather than a permissive `{"type": "object"}`: an agent with no
    declared structure must not be given an empty schema, because ADK would
    then constrain the model to return `{}`.
    """
    fields = (structure or {}).get("fields") or []

    properties: dict[str, dict] = {}
    required: list[str] = []
    for field in fields:
        name = (field.get("name") or "").strip()
        if not name:
            continue
        spec = dict(FIELD_TYPES.get(field.get("type") or "string", {"type": "string"}))
        description = (field.get("description") or "").strip()
        if description:
            spec["description"] = description
        properties[name] = spec
        if field.get("required"):
            required.append(name)

    if not properties:
        return None

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def structure_example(structure: dict | None) -> dict:
    """An example object matching a field list, for docs and tests."""
    fields = (structure or {}).get("fields") or []
    return {
        field["name"]: _SAMPLES.get(field.get("type") or "string", "")
        for field in fields
        if (field.get("name") or "").strip()
    }


def structure_errors(structure: dict | None, label: str) -> list[str]:
    """Validation the JSON Schema in `config_schema` cannot express.

    Duplicate and blank names, and names that are not usable as a parameter,
    because both ADK's pydantic model and the tool declaration are keyed by
    them.
    """
    fields = (structure or {}).get("fields") or []
    errors: list[str] = []
    seen: set[str] = set()

    for index, field in enumerate(fields):
        name = (field.get("name") or "").strip()
        if not name:
            errors.append(f"{label} field {index + 1} has no name.")
            continue
        if not name.isidentifier():
            errors.append(
                f"{label} field '{name}' is not a valid parameter name; use "
                "letters, digits and underscores, and do not start with a digit."
            )
        if name in seen:
            errors.append(f"{label} declares '{name}' more than once.")
        seen.add(name)

    return errors
