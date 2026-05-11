from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PaletteMetadata:
    label: str
    category: str          # "triggers" | "flow" | "ai" | "data"
    color: str             # hex
    icon: str              # lucide icon name
    description: str
    wave: int = 1          # 1 = first-wave, 2 = second-wave


@dataclass
class NodeDefinition(ABC):
    node_type: str
    version: str
    palette: PaletteMetadata

    # JSON Schema for the node's config block
    config_schema: dict = field(default_factory=dict)

    # JSON Schema for expected input payload
    input_schema: dict = field(default_factory=lambda: {"type": "object"})

    # JSON Schema for output payload
    output_schema: dict = field(default_factory=lambda: {"type": "object"})

    # Named output handles (e.g. CONDITION has "true" and "false")
    output_handles: list[str] = field(default_factory=lambda: ["output"])

    # Whether this node can have inbound edges
    allows_inbound: bool = True

    # Whether this node can have outbound edges
    allows_outbound: bool = True

    # Whether this node starts an execution path (is a trigger)
    is_trigger: bool = False

    # Whether this node terminates an execution path
    is_terminal: bool = False

    # Whether cycles through this node are allowed
    allows_cycle: bool = False

    @abstractmethod
    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        """Execute the node logic. Returns output dict."""
        ...

    def validate_config(self, config: dict) -> list[str]:
        """Validate node config against config_schema. Returns list of error strings."""
        from jsonschema import validate, ValidationError
        errors = []
        try:
            validate(instance=config, schema=self.config_schema)
        except ValidationError as e:
            errors.append(e.message)
        return errors
