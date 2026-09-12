"""A2A_START — the only entry node a workflow can have.

Replaces HTTP_TRIGGER, SCHEDULE_TRIGGER, WEBHOOK_TRIGGER and QUEUE_TRIGGER.  A
packaged workflow is an A2A agent, so it is never started by a path, a cron
expression or a queue subscription: a caller sends it a message and the payload
in that message is the workflow's input.

`payload_schema` is the contract this node advertises.  It is stored in the
canvas in an authoring-friendly shape (a list of named fields) because that is
what the test panel renders its form from; `payload_json_schema()` converts it
to the JSON Schema the generated package embeds as `PAYLOAD_SCHEMA`.
"""

from dataclasses import dataclass
from typing import Any

from app.nodes.base import NodeDefinition, PaletteMetadata

# Field types offered in the canvas, and the JSON Schema each maps to.
# `text` is a string the UI renders as a textarea rather than a single line.
_FIELD_TYPES: dict[str, dict] = {
    "string": {"type": "string"},
    "text": {"type": "string"},
    "number": {"type": "number"},
    "integer": {"type": "integer"},
    "boolean": {"type": "boolean"},
    "object": {"type": "object"},
    "array": {"type": "array"},
}

DEFAULT_STATE_KEY = "wf"


@dataclass
class A2aStartNode(NodeDefinition):
    node_type: str = "A2A_START"
    version: str = "1"
    palette: PaletteMetadata = None
    allows_inbound: bool = False
    is_trigger: bool = True

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="A2A Start",
            category="triggers",
            color="#6366f1",
            icon="Play",
            description="Entry point — receives the A2A message payload",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "payload_schema": {
                    "type": "object",
                    "description": (
                        "The payload this workflow expects. Drives the test form "
                        "and the agent card's documented input."
                    ),
                    "properties": {
                        "fields": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": sorted(_FIELD_TYPES),
                                        "default": "string",
                                    },
                                    "description": {"type": "string"},
                                    "required": {"type": "boolean", "default": False},
                                },
                                "required": ["name"],
                            },
                        },
                    },
                },
                "input_mode": {
                    "type": "string",
                    "enum": ["json", "text"],
                    "default": "json",
                    "description": (
                        "json: the caller sends a JSON object. "
                        "text: the message is prose and arrives as {'text': ...}."
                    ),
                },
                "state_key": {
                    "type": "string",
                    "default": DEFAULT_STATE_KEY,
                    "description": "Top-level key holding workflow state.",
                },
            },
            "required": [],
        }
        # The A2A message payload — shape is described by payload_schema.
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output"]


# ── Conversions used by the packaging layer ──────────────────────────────────


def payload_json_schema(config: dict) -> dict:
    """Convert the canvas `payload_schema` fields into JSON Schema.

    An empty or missing field list yields a permissive object schema, which is
    honest: the workflow accepts anything and the test panel falls back to a raw
    JSON editor.
    """
    fields = ((config or {}).get("payload_schema") or {}).get("fields") or []

    if not fields:
        if (config or {}).get("input_mode") == "text":
            return {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The message text."}
                },
                "required": ["text"],
            }
        return {"type": "object"}

    properties: dict[str, dict] = {}
    required: list[str] = []
    for field in fields:
        name = (field.get("name") or "").strip()
        if not name:
            continue
        spec = dict(_FIELD_TYPES.get(field.get("type") or "string", {"type": "string"}))
        description = (field.get("description") or "").strip()
        if description:
            spec["description"] = description
        # Preserve the authoring hint that a string should render as a textarea.
        if field.get("type") == "text":
            spec["x-ui-widget"] = "textarea"
        properties[name] = spec
        if field.get("required"):
            required.append(name)

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def example_payload(config: dict) -> dict:
    """A payload matching `payload_schema`, for docs and the test form default."""
    fields = ((config or {}).get("payload_schema") or {}).get("fields") or []
    if not fields:
        if (config or {}).get("input_mode") == "text":
            return {"text": "hello"}
        return {}

    samples: dict[str, Any] = {
        "string": "",
        "text": "",
        "number": 0,
        "integer": 0,
        "boolean": False,
        "object": {},
        "array": [],
    }
    return {
        field["name"]: samples.get(field.get("type") or "string", "")
        for field in fields
        if (field.get("name") or "").strip()
    }
