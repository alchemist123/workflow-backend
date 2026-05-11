from pydantic import BaseModel, ConfigDict
from typing import Any
from datetime import datetime

_orm = ConfigDict(from_attributes=True, protected_namespaces=())
_no_ns = ConfigDict(protected_namespaces=())


class AgentCreate(BaseModel):
    model_config = _no_ns
    name: str
    description: str = ""
    system_prompt: str = ""
    model_id: str | None = None
    tools: list[str] = []
    config: dict[str, Any] = {}


class AgentRead(BaseModel):
    model_config = _orm
    id: str
    name: str
    description: str | None
    status: str
    system_prompt: str | None
    model_id: str | None
    tools: list[Any]
    config: dict[str, Any]
    created_at: datetime


class ModelCreate(BaseModel):
    model_config = _no_ns
    name: str
    provider: str
    model_id: str
    api_key_env: str | None = None
    config: dict[str, Any] = {}


class ModelRead(BaseModel):
    model_config = _orm
    id: str
    name: str
    provider: str
    model_id: str
    status: str
    api_key_env: str | None
    config: dict[str, Any]
    created_at: datetime


class ToolCreate(BaseModel):
    name: str
    description: str = ""
    tool_type: str
    input_schema: dict[str, Any] = {}
    output_schema: dict[str, Any] = {}
    implementation: dict[str, Any] = {}


class ToolRead(BaseModel):
    model_config = _orm
    id: str
    name: str
    description: str | None
    status: str
    tool_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    implementation: dict[str, Any]
    created_at: datetime


class DataSourceCreate(BaseModel):
    name: str
    description: str = ""
    source_type: str
    connection_config: dict[str, Any] = {}


class DataSourceRead(BaseModel):
    model_config = _orm
    id: str
    name: str
    description: str | None
    status: str
    source_type: str
    connection_config: dict[str, Any]
    created_at: datetime
