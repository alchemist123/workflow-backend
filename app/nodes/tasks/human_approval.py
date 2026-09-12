"""HUMAN_APPROVAL — park the run and ask a person, over A2A.

ADK graphs have a first-class mechanism for this and the packaged agent needs
no machinery of its own. A node that returns a `RequestInput` is turned into an
interrupt event (`BaseNode.run`: "RequestInput -> convert to interrupt Event"),
and `to_a2a` maps that to A2A task state **`input-required`**. The task simply
parks there until someone answers.

The caller answers by sending another `message/send` with the **same task id**
and a data part marked `adk_type: function_response`, carrying the interrupt id
and the response. On that message the node runs a second time and reads the
answer from `ctx.resume_inputs[interrupt_id]`.

Two details make or break it, and both are handled in the render template
rather than left to whoever draws the canvas:

* the node must set `rerun_on_resume=True`, or it is marked complete on resume
  and never sees the answer;
* the interrupt id must be derived from `ctx.node_path` rather than generated,
  or the second run cannot look the answer up and asks again forever.

What this node deliberately does not have is a timeout. It returns and the
graph parks — nothing is waiting in-process for a clock to fire — so expiring
an approval needs a scheduler outside the package. The old `timeout_seconds` /
`on_timeout` settings were never implemented and are dropped by the canvas
migration rather than left as controls that do nothing.
"""

from dataclasses import dataclass

from app.nodes.agent_io import FIELD_TYPES
from app.nodes.base import NodeDefinition, PaletteMetadata

# Always asked for, whatever else the node collects. `approved` is what the
# two output handles branch on.
DECISION_FIELD = "approved"
COMMENT_FIELD = "comment"


@dataclass
class HumanApprovalNode(NodeDefinition):
    node_type: str = "HUMAN_APPROVAL"
    uses_named_routes: bool = True
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Human Approval",
            category="flow",
            color="#a855f7",
            icon="UserCheck",
            description="Pause and ask a person — the A2A task waits at input-required",
            wave=2,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "The question put to the approver. Shown on the paused "
                        "task, so say what is being approved."
                    ),
                },
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Who should decide. Passed through to the caller on the "
                        "paused task as routing information — the packaged agent "
                        "does not notify anyone itself."
                    ),
                },
                "collect_fields": {
                    "type": "object",
                    "description": (
                        "Extra values to ask for alongside the decision. These "
                        "become part of the response schema advertised on the "
                        "paused task, and are merged into the payload on resume."
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
        self.output_schema = {
            "type": "object",
            "properties": {
                DECISION_FIELD: {"type": "boolean"},
                COMMENT_FIELD: {"type": "string"},
            },
        }
        # The decision is a route, so the canvas branches on it directly.
        self.output_handles = ["approved", "rejected"]


def required_extra_fields(config: dict) -> list[str]:
    """Collected fields the author marked required.

    Enforced by the node on approval rather than by the advertised schema --
    see `response_json_schema` for why.
    """
    names: list[str] = []
    for field in ((config or {}).get("collect_fields") or {}).get("fields") or []:
        name = (field.get("name") or "").strip()
        if name and name not in (DECISION_FIELD, COMMENT_FIELD) and field.get("required"):
            names.append(name)
    return names


def response_json_schema(config: dict) -> dict:
    """The schema the paused task advertises for the approver's reply.

    Always `approved` and `comment`, plus whatever `collect_fields` adds. ADK
    passes this through as `RequestInput.response_schema` and *enforces* it:
    an answer missing a required key is rejected before the node ever sees it.

    Which is why only `approved` is required here, even when the author marked
    a collected field required. Those fields describe an approval -- a cost
    centre, a ticket number -- and demanding them up front makes a *rejection*
    impossible without inventing values for fields that do not apply. The node
    enforces them on approval instead, and asks again if one is missing.
    """
    properties: dict[str, dict] = {
        DECISION_FIELD: {
            "type": "boolean",
            "description": "True to approve, false to reject.",
        },
        COMMENT_FIELD: {
            "type": "string",
            "description": "Optional note explaining the decision.",
        },
    }
    required = [DECISION_FIELD]

    for field in ((config or {}).get("collect_fields") or {}).get("fields") or []:
        name = (field.get("name") or "").strip()
        if not name or name in properties:
            continue
        spec = dict(FIELD_TYPES.get(field.get("type") or "string", {"type": "string"}))
        description = (field.get("description") or "").strip()
        if description:
            spec["description"] = description
        if field.get("required"):
            # Say so in the description, since the schema cannot.
            hint = "Required when approving."
            existing = (spec.get("description") or "").rstrip()
            if existing and not existing.endswith((".", "?", "!")):
                existing += "."
            spec["description"] = f"{existing} {hint}".strip()
        properties[name] = spec

    return {"type": "object", "properties": properties, "required": required}
