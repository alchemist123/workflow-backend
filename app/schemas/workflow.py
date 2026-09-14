from pydantic import BaseModel
from typing import Any
from datetime import datetime
from app.schemas.canvas import CanvasPayload


class WorkflowCreate(BaseModel):
    name: str
    description: str = ""


class WorkflowUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None


class WorkflowRead(BaseModel):
    id: str
    name: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WorkflowVersionRead(BaseModel):
    id: str
    workflow_id: str
    version_number: int
    canvas_json: dict[str, Any]
    ir_json: dict[str, Any] | None
    validation_errors: list[Any] | None
    is_valid: bool
    created_at: datetime

    class Config:
        from_attributes = True


class SaveCanvasRequest(BaseModel):
    canvas: CanvasPayload


class CompileResponse(BaseModel):
    version_id: str
    is_valid: bool
    errors: list[str]
    warnings: list[str] = []
    ir: dict[str, Any] | None


class ExecutionRead(BaseModel):
    id: str
    workflow_id: str
    version_id: str
    status: str
    trigger_payload: dict[str, Any] | None
    output: dict[str, Any] | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class NodeInputsRequest(BaseModel):
    """Ask what a node can read, for the mapping picker.

    Takes the canvas rather than a saved version id: the picker is used while
    editing, before anything is saved.
    """

    canvas: dict[str, Any]
    node_id: str


class AnswerRequest(BaseModel):
    """The reply to a run parked on a HUMAN_APPROVAL node.

    `response` is validated against the schema the paused task advertised, by
    ADK, before the node sees it -- so it must at least carry `approved`.
    """

    response: dict[str, Any] = {}


class TestRunRequest(BaseModel):
    """Input for a test run against the generated package."""

    payload: dict[str, Any] = {}

    # message: one blocking `message/send`, which is how most callers invoke an
    # agent. task: submit without blocking, then poll `tasks/get` — the path a
    # caller uses for a workflow too slow to hold a connection open for.
    mode: str = "message"

    # Re-render the package even if one already exists for this version. Needed
    # after editing the template or the renderer, not after a canvas change
    # (which produces a new version).
    rebuild: bool = False
