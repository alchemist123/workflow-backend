from dataclasses import dataclass, field
from typing import Any
from datetime import datetime


@dataclass
class ExecutionContext:
    execution_id: str
    workflow_id: str
    version_id: str
    trigger_payload: dict[str, Any]
    node_outputs: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    loop_counters: dict[str, int] = field(default_factory=dict)
    started_at: datetime = field(default_factory=datetime.utcnow)
    db_session: Any = None
