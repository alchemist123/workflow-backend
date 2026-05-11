from pydantic import BaseModel, Field
from typing import Any


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
