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
