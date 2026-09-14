"""TRANSFORM — reshape the payload between nodes.

Four modes. Three are expressions (JMESPath, Jinja2, safe Python); the fourth,
`fields`, is the no-code one: declare the output shape and say where each field
comes from, picked from what the canvas knows is available at that point.

`fields` is the only mode whose output shape the compiler can see, which is what
lets a later node offer those fields in its own picker and lets a mapping be
checked before it runs. An expression is opaque by nature — the compiler cannot
know what a JMESPath query returns without running it.
"""

from dataclasses import dataclass

from app.nodes.agent_io import FIELD_TYPES
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class TransformNode(NodeDefinition):
    node_type: str = "TRANSFORM"
    supports_on_error_continue: bool = True
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
                    "enum": ["fields", "jmespath", "jinja2", "python"],
                    "default": "fields",
                    "description": (
                        "fields: declare the output shape and map each field "
                        "from something available. The others are expressions."
                    ),
                },
                "expression": {
                    "type": "string",
                    "description": "Transformation expression (JMESPath query, Jinja2 template, or safe Python)",
                },
                "output_fields": {
                    "type": "array",
                    "description": (
                        "The shape to build, for mode=fields. Each entry names "
                        "an output field and where its value comes from."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": sorted(FIELD_TYPES),
                                "default": "string",
                            },
                            "source": {
                                "type": "string",
                                "description": (
                                    "Where the value comes from: a path like "
                                    "'data.qty' or 'vars.order.sku', picked "
                                    "from what is available at this node."
                                ),
                            },
                            "default": {
                                "description": "Used when the source is absent."
                            },
                            "required": {
                                "type": "boolean",
                                "default": False,
                                "description": (
                                    "Fail the node when the source is absent "
                                    "and there is no default."
                                ),
                            },
                        },
                        "required": ["name"],
                    },
                },
                "output_key": {
                    "type": "string",
                    "description": "Wrap result in this key; omit to return result directly",
                },
            },
            "required": ["mode"],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output"]

