import uuid
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks  # BackgroundTasks used by execute
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.workflow import Workflow, WorkflowVersion, WorkflowExecution, ExecutionStatus, WorkflowStatus
from app.schemas.workflow import (
    WorkflowCreate, WorkflowUpdate, WorkflowRead, WorkflowVersionRead,
    SaveCanvasRequest, CompileResponse, ExecutionRead,
)
from app.compiler import run_compiler
from app.runtime.engine import WorkflowEngine
from app.nodes.registry import get_palette

router = APIRouter(prefix="/workflows", tags=["workflows"])


@router.get("/palette")
async def get_node_palette():
    """Return all registered node types with palette metadata and schemas."""
    return get_palette()


@router.post("", response_model=WorkflowRead)
async def create_workflow(body: WorkflowCreate, db: AsyncSession = Depends(get_db)):
    wf = Workflow(id=str(uuid.uuid4()), name=body.name, description=body.description)
    db.add(wf)
    await db.flush()
    return wf


@router.get("", response_model=list[WorkflowRead])
async def list_workflows(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Workflow).order_by(Workflow.updated_at.desc()))
    return result.scalars().all()


@router.get("/{workflow_id}", response_model=WorkflowRead)
async def get_workflow(workflow_id: str, db: AsyncSession = Depends(get_db)):
    wf = await _get_or_404(db, Workflow, workflow_id)
    return wf


@router.patch("/{workflow_id}", response_model=WorkflowRead)
async def update_workflow(workflow_id: str, body: WorkflowUpdate, db: AsyncSession = Depends(get_db)):
    wf = await _get_or_404(db, Workflow, workflow_id)
    if body.name is not None:
        wf.name = body.name
    if body.description is not None:
        wf.description = body.description
    if body.status is not None:
        wf.status = WorkflowStatus(body.status)
    await db.flush()
    return wf


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, db: AsyncSession = Depends(get_db)):
    wf = await _get_or_404(db, Workflow, workflow_id)
    await db.delete(wf)


@router.post("/{workflow_id}/versions", response_model=CompileResponse)
async def save_and_compile(
    workflow_id: str,
    body: SaveCanvasRequest,
    db: AsyncSession = Depends(get_db),
):
    """Save canvas JSON, run the full compiler pipeline, and persist a new version."""
    await _get_or_404(db, Workflow, workflow_id)

    version_number_result = await db.execute(
        select(func.count(WorkflowVersion.id)).where(WorkflowVersion.workflow_id == workflow_id)
    )
    version_number = (version_number_result.scalar() or 0) + 1
    version_id = str(uuid.uuid4())

    errors, warnings, ir = run_compiler(body.canvas, version_id)
    is_valid = len(errors) == 0

    version = WorkflowVersion(
        id=version_id,
        workflow_id=workflow_id,
        version_number=version_number,
        canvas_json=body.canvas.model_dump(),
        ir_json=ir.to_dict() if ir else None,
        validation_errors=errors,
        is_valid=is_valid,
    )
    db.add(version)
    await db.flush()

    return CompileResponse(
        version_id=version_id,
        is_valid=is_valid,
        errors=errors,
        warnings=warnings,
        ir=ir.to_dict() if ir else None,
    )


@router.get("/{workflow_id}/versions", response_model=list[WorkflowVersionRead])
async def list_versions(workflow_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version_number.desc())
    )
    return result.scalars().all()


@router.get("/{workflow_id}/versions/{version_id}", response_model=WorkflowVersionRead)
async def get_version(workflow_id: str, version_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.id == version_id, WorkflowVersion.workflow_id == workflow_id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    return version


@router.post("/{workflow_id}/versions/{version_id}/execute", response_model=ExecutionRead)
async def execute_workflow(
    workflow_id: str,
    version_id: str,
    payload: dict = None,
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
):
    """Start an asynchronous workflow execution."""
    result = await db.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.id == version_id, WorkflowVersion.workflow_id == workflow_id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    if not version.is_valid:
        raise HTTPException(status_code=422, detail="Version has validation errors and cannot be executed")

    # Wrap the raw payload as an HTTP trigger envelope so trigger nodes see body/headers/query
    wrapped_payload = {
        "body": payload or {},
        "headers": {},
        "query": {},
        "method": "POST",
    }

    execution = WorkflowExecution(
        id=str(uuid.uuid4()),
        workflow_id=workflow_id,
        version_id=version_id,
        status=ExecutionStatus.PENDING,
        trigger_payload=wrapped_payload,
    )
    db.add(execution)
    # Commit now so the execution row exists before the background task starts.
    # In Starlette 1.x background tasks run before dependency teardown, meaning
    # the get_db auto-commit would fire after the background task's first DB write.
    await db.commit()

    background_tasks.add_task(_run_execution, execution.id, version.ir_json, wrapped_payload)
    return execution


async def _run_execution(execution_id: str, ir_dict: dict, payload: dict):
    from app.database import AsyncSessionLocal
    from app.compiler.ir import IR
    async with AsyncSessionLocal() as session:
        engine = WorkflowEngine(db_session=session)
        ir = IR.from_dict(ir_dict)
        await engine.execute(ir, execution_id, payload)


@router.get("/{workflow_id}/executions", response_model=list[ExecutionRead])
async def list_executions(workflow_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(WorkflowExecution)
        .where(WorkflowExecution.workflow_id == workflow_id)
        .order_by(WorkflowExecution.created_at.desc())
        .limit(50)
    )
    return result.scalars().all()


@router.get("/{workflow_id}/executions/{execution_id}", response_model=ExecutionRead)
async def get_execution(workflow_id: str, execution_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(WorkflowExecution)
        .where(WorkflowExecution.id == execution_id, WorkflowExecution.workflow_id == workflow_id)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    return execution


@router.get("/{workflow_id}/executions/{execution_id}/node_logs")
async def get_node_logs(workflow_id: str, execution_id: str, db: AsyncSession = Depends(get_db)):
    """Return per-node execution logs for a given execution."""
    from app.models.workflow import NodeExecutionLog
    result = await db.execute(
        select(NodeExecutionLog)
        .where(NodeExecutionLog.execution_id == execution_id)
        .order_by(NodeExecutionLog.started_at)
    )
    logs = result.scalars().all()
    return [
        {
            "node_id": log.node_id,
            "node_type": log.node_type,
            "status": log.status.value if hasattr(log.status, "value") else str(log.status),
            "started_at": log.started_at.isoformat() if log.started_at else None,
            "finished_at": log.finished_at.isoformat() if log.finished_at else None,
            "error": log.error,
        }
        for log in logs
    ]


@router.post("/{workflow_id}/versions/{version_id}/package")
async def package_workflow(
    workflow_id: str,
    version_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Generate a fully self-contained project directory for this workflow version.

    The directory contains main.py, Dockerfile, docker-compose.yml, requirements.txt,
    .env.example, ir.json and a README.  It has zero dependency on this backend.
    Run `docker compose up --build` inside it to start the workflow as a service.
    """
    result = await db.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.id == version_id, WorkflowVersion.workflow_id == workflow_id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    if not version.is_valid or not version.ir_json:
        raise HTTPException(status_code=422, detail="Version must be valid and compiled before packaging")

    wf = await _get_or_404(db, Workflow, workflow_id)
    workflow_name = (wf.name or "workflow").lower().replace(" ", "-")

    from app.packaging.builder import build_runner_package
    import asyncio
    loop = asyncio.get_event_loop()
    pkg = await loop.run_in_executor(None, build_runner_package, version.ir_json, version_id, workflow_name)
    return pkg


# ─── helpers ─────────────────────────────────────────────────────────────────

async def _get_or_404(db: AsyncSession, model, id: str):
    result = await db.execute(select(model).where(model.id == id))
    obj = result.scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return obj
