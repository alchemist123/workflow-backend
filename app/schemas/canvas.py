from pydantic import BaseModel, Field
from typing import Any

# Bumped when the canvas shape changes in a way that requires rewriting saved
# workflows. app/compiler/canvas_migrations.py holds the migrations keyed on it.
#   1  four trigger node types
#   2  A2A_START replaces all triggers; single entry, single terminal
#   3  ORCHESTRATOR_AGENT loses tool_execution_mode; ordering is drawn with
#      SEQUENTIAL_AGENT / PARALLEL_AGENT nodes instead
CURRENT_SCHEMA_VERSION = 7


class NodePosition(BaseModel):
    x: float
    y: float


class NodeMetadata(BaseModel):
    title: str = ""
    description: str = ""


class NodePolicies(BaseModel):
    timeout_seconds: int = 60
    retry: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 1})
    on_error: str = "fail"


class NodeIO(BaseModel):
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class CanvasNode(BaseModel):
    id: str
    type: str
    version: str = "1"
    position: NodePosition
    metadata: NodeMetadata = Field(default_factory=NodeMetadata)
    config: dict[str, Any] = Field(default_factory=dict)
    io: NodeIO = Field(default_factory=NodeIO)
    policies: NodePolicies = Field(default_factory=NodePolicies)


class CanvasEdge(BaseModel):
    id: str
    source: str
    source_handle: str = "output"
    target: str
    target_handle: str = "input"
    condition: str | None = None


class CanvasPayload(BaseModel):
    nodes: list[CanvasNode]
    edges: list[CanvasEdge]

    # Defaults to the current version: a canvas arriving from the UI is always
    # current, while one loaded from the database may be older and is migrated
    # on read by app/compiler/canvas_migrations.py.
    schema_version: int = CURRENT_SCHEMA_VERSION
