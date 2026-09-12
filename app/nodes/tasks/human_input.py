"""HUMAN_INPUT — park the run and ask a person for data.

The same ADK and A2A mechanism as HUMAN_APPROVAL (a node returning
`RequestInput` becomes an interrupt, and `to_a2a` reports the task as
`input-required`), used for the other thing a person is asked for: not a
decision, but values the workflow needs and cannot work out for itself.

A separate node type rather than a mode on HUMAN_APPROVAL because the two have
different shapes on the canvas. An approval branches — `approved` and
`rejected` are routes you wire onward separately. An input request does not:
there is one way out, and the answer arrives merged into the payload. Handles
are declared per node type, so one node cannot be both without making the
canvas lie about where its edges go.

Required fields are enforced by the node, not by the schema the paused task
advertises, and both node types work that way. ADK validates that schema before
the node runs again and a failure fails the whole run — so one blank field would
destroy a workflow that had been waiting on a person, at the worst possible
moment. Asking again, naming what is missing, is the kinder answer. The names
travel on the interrupt payload as `required_fields` so a client can mark them
and check before sending.
"""

from dataclasses import dataclass

from app.nodes.agent_io import FIELD_TYPES
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class HumanInputNode(NodeDefinition):
    node_type: str = "HUMAN_INPUT"
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Human Input",
            category="flow",
            color="#8b5cf6",
            icon="MessageSquare",
            description="Pause and ask a person for values — the task waits at input-required",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "What to ask for. Shown on the paused task, so say why "
                        "the workflow needs it."
                    ),
                },
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Who should supply it. Passed to the caller on the "
                        "paused task as routing information — the packaged "
                        "agent does not notify anyone itself."
                    ),
                },
                "collect_fields": {
                    "type": "object",
                    "description": (
                        "The values to collect. These become the schema the "
                        "paused task advertises, and are merged into the "
                        "payload when the person answers."
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
                },
            },
            "required": [],
        }
        self.input_schema = {"type": "object"}
        # Shape depends on `collect_fields`, so nothing fixed to declare.
        self.output_schema = {"type": "object"}
        # One way out: the answer is data, not a decision.
        self.output_handles = ["output"]


def required_fields(config: dict) -> list[str]:
    """Fields the author marked required, enforced by the node.

    Not by the advertised schema -- see `request_json_schema`.
    """
    names: list[str] = []
    for field in ((config or {}).get("collect_fields") or {}).get("fields") or []:
        name = (field.get("name") or "").strip()
        if name and field.get("required") and name not in names:
            names.append(name)
    return names


def request_json_schema(config: dict) -> dict:
    """The schema the paused task advertises for the person's answer.

    Nothing is marked required, even when the author said so. ADK *enforces*
    this schema before the node runs again, and a failed validation fails the
    whole run -- so one blank field would destroy a workflow that had been
    waiting on a person, which is the worst possible moment to lose it.

    The node enforces them instead and asks again, naming what is missing. The
    names travel on the interrupt payload as `required_fields` so a client can
    still mark them and check before sending.
    """
    properties: dict[str, dict] = {}
    required = set(required_fields(config))

    for field in ((config or {}).get("collect_fields") or {}).get("fields") or []:
        name = (field.get("name") or "").strip()
        if not name or name in properties:
            continue
        spec = dict(FIELD_TYPES.get(field.get("type") or "string", {"type": "string"}))
        description = (field.get("description") or "").strip()
        if name in required:
            hint = "Required."
            if description and not description.endswith((".", "?", "!")):
                description += "."
            description = f"{description} {hint}".strip() if description else hint
        if description:
            spec["description"] = description
        properties[name] = spec

    return {"type": "object", "properties": properties}
