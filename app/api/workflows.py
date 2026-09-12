import uuid
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks  # BackgroundTasks used by execute
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.workflow import Workflow, WorkflowVersion, WorkflowExecution, ExecutionStatus, WorkflowStatus
from app.schemas.workflow import (
    AnswerRequest,
    WorkflowCreate, WorkflowUpdate, WorkflowRead, WorkflowVersionRead,
    SaveCanvasRequest, CompileResponse, ExecutionRead, TestRunRequest,
)
from app.compiler import run_compiler
from app.compiler.canvas_migrations import migrate_canvas, needs_migration
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
    return [_read_version(v) for v in result.scalars().all()]


@router.get("/{workflow_id}/versions/{version_id}", response_model=WorkflowVersionRead)
async def get_version(workflow_id: str, version_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.id == version_id, WorkflowVersion.workflow_id == workflow_id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    return _read_version(version)


@router.post("/{workflow_id}/versions/{version_id}/test", response_model=ExecutionRead)
async def test_workflow(
    workflow_id: str,
    version_id: str,
    body: TestRunRequest | None = None,
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
):
    """Run this version by driving its generated package.

    Unlike `/execute`, which runs the platform's own engine, this renders the
    package and drives it over its A2A surface — so a test exercises the exact
    artefact that ships. The run is recorded as a WorkflowExecution with
    per-node logs, which the existing polling and canvas overlay already read.
    """
    request = body or TestRunRequest()

    result = await db.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.id == version_id, WorkflowVersion.workflow_id == workflow_id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    if not version.is_valid or not version.ir_json:
        raise HTTPException(
            status_code=422,
            detail="This version has validation errors. Save & Compile before testing it.",
        )

    wf = await _get_or_404(db, Workflow, workflow_id)
    plan = _graph_plan_for(version, wf)

    execution = WorkflowExecution(
        id=str(uuid.uuid4()),
        workflow_id=workflow_id,
        version_id=version_id,
        status=ExecutionStatus.PENDING,
        trigger_payload={"payload": request.payload, "mode": request.mode},
    )
    db.add(execution)
    # Commit before the background task starts: Starlette runs background tasks
    # before dependency teardown, so get_db's auto-commit would land after the
    # task's first write.
    await db.commit()

    background_tasks.add_task(
        _run_package_test, execution.id, plan, request.payload, request.mode, request.rebuild
    )
    return execution


async def _run_package_test(
    execution_id: str,
    plan,
    payload: dict,
    mode: str,
    rebuild: bool,
    answer: dict | None = None,
) -> None:
    """Drive the package and record the run against the execution row."""
    from datetime import datetime

    from app.database import AsyncSessionLocal
    from app.runtime.package_runner import run_workflow_package

    async with AsyncSessionLocal() as session:
        execution = await session.get(WorkflowExecution, execution_id)
        if execution is None:
            return
        execution.status = ExecutionStatus.RUNNING
        execution.started_at = datetime.utcnow()
        await session.commit()

    run = await run_workflow_package(
        plan, payload, mode=mode, rebuild=rebuild, answer=answer
    )
    await _record_run(execution_id, run)


async def _resume_package_test(
    execution_id: str,
    plan,
    task_id: str,
    context_id: str | None,
    interrupt_id: str,
    response: dict,
) -> None:
    """Answer a parked task and record the run that follows."""
    from app.database import AsyncSessionLocal
    from app.runtime.package_runner import resume_workflow_package

    async with AsyncSessionLocal() as session:
        execution = await session.get(WorkflowExecution, execution_id)
        if execution is None:
            return
        execution.status = ExecutionStatus.RUNNING
        await session.commit()

    run = await resume_workflow_package(
        plan,
        task_id=task_id,
        context_id=context_id,
        interrupt_id=interrupt_id,
        response=response,
    )
    await _record_run(execution_id, run)


async def _record_run(execution_id: str, run) -> None:
    """Write one RunResult onto its execution row, with per-node logs."""
    from datetime import datetime

    from app.database import AsyncSessionLocal
    from app.models.workflow import NodeExecutionLog

    async with AsyncSessionLocal() as session:
        execution = await session.get(WorkflowExecution, execution_id)
        if execution is None:
            return

        # A workflow waiting on a human is neither finished nor broken, and
        # the status enum already has the right word for it. Recording it as
        # FAILED would send the user looking for a bug that is not there.
        if run.input_required:
            execution.status = ExecutionStatus.WAITING
        else:
            execution.status = (
                ExecutionStatus.SUCCESS if run.ok else ExecutionStatus.FAILED
            )
        execution.finished_at = datetime.utcnow()
        execution.error = run.error
        execution.output = {
            "result": run.result,
            "a2a": {
                "task_id": run.task_id,
                # Needed to answer this task later, so it has to be recorded
                # at the moment the run parks.
                "context_id": run.context_id,
                "state": run.state,
                "mode": run.mode,
                "polls": run.polls,
            },
            "duration_ms": run.duration_ms,
            "package_dir": run.package_dir,
            "warnings": run.warnings,
            # Present when the run parked on a HUMAN_APPROVAL node.
            "input_required": run.input_required,
        }

        # One log row per node event, keyed by canvas node so the existing
        # /node_logs endpoint and the canvas overlay work unchanged. A looping
        # node produces one row per iteration.
        finished = datetime.utcnow()
        for step in run.steps:
            if not step.canvas_id:
                continue
            session.add(
                NodeExecutionLog(
                    id=str(uuid.uuid4()),
                    execution_id=execution_id,
                    node_id=step.canvas_id,
                    node_type=step.node_type or "",
                    status=ExecutionStatus.FAILED if step.error else ExecutionStatus.SUCCESS,
                    output_data=step.output if isinstance(step.output, dict) else {"value": step.output},
                    error=step.error,
                    started_at=finished,
                    finished_at=finished,
                )
            )
        await session.commit()


@router.post(
    "/{workflow_id}/executions/{execution_id}/answer", response_model=ExecutionRead
)
async def answer_execution(
    workflow_id: str,
    execution_id: str,
    body: AnswerRequest,
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
):
    """Answer a run that parked on a HUMAN_APPROVAL node.

    A real resume: `run_once.py` keeps the A2A task and the ADK session in a
    SQLite file beside the package, so the workflow carries on from the
    approval node and nothing before it runs a second time.

    The original execution row is reused, so one decision shows as one run.
    """
    execution = await db.get(WorkflowExecution, execution_id)
    if not execution or execution.workflow_id != workflow_id:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.status != ExecutionStatus.WAITING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This run is {execution.status.value}, not waiting for input. "
                "Only a run parked on a human approval can be answered."
            ),
        )

    result = await db.execute(
        select(WorkflowVersion).where(WorkflowVersion.id == execution.version_id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    wf = await _get_or_404(db, Workflow, workflow_id)
    plan = _graph_plan_for(version, wf)

    # The ids of the task this run parked on, recorded when it parked.
    output = execution.output or {}
    pending = output.get("input_required") or {}
    task_id = (output.get("a2a") or {}).get("task_id")
    interrupt_id = pending.get("interrupt_id")
    if not task_id or not interrupt_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "This run did not record what it was waiting for, so it cannot "
                "be resumed. Run it again."
            ),
        )

    execution.status = ExecutionStatus.PENDING
    execution.error = None
    await db.commit()

    background_tasks.add_task(
        _resume_package_test,
        execution.id,
        plan,
        task_id,
        output.get("context_id") or (output.get("a2a") or {}).get("context_id"),
        interrupt_id,
        body.response,
    )
    return execution


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
    # The display name is passed through as-is: the graph plan derives its own
    # Python identifier (workflow_slug) for the ADK graph and AGENT_NAME, and
    # the builder derives the directory slug. Pre-mangling it here would only
    # put a hyphenated name on the agent card.
    workflow_name = wf.name or "workflow"

    plan = _graph_plan_for(version, wf)

    # Rendering shells out (byte-compile, import probe, ruff, git), so keep it
    # off the event loop.
    import asyncio
    import functools

    from app.packaging.builder import RenderError, build_runner_package

    loop = asyncio.get_running_loop()
    try:
        pkg = await loop.run_in_executor(
            None, functools.partial(build_runner_package, plan, workflow_name=workflow_name)
        )
    except RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return pkg


# ─── helpers ─────────────────────────────────────────────────────────────────


def _graph_plan_for(version: WorkflowVersion, workflow: Workflow):
    """Compile a stored version into an ADK graph plan, or raise 422.

    Shared by /test and /package so a workflow is never tested against a
    different plan than the one that would be packaged.
    """
    from app.compiler import GraphPlanError, build_graph_plan
    from app.compiler.graph_check import verify_plan_builds
    from app.compiler.ir import IR

    try:
        plan = build_graph_plan(
            IR.from_dict(version.ir_json),
            workflow_name=workflow.name or "workflow",
            workflow_description=workflow.description or "",
            canvas_schema_version=(version.canvas_json or {}).get("schema_version", 1),
        )
    except GraphPlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Let ADK's own validator judge the graph before anything is rendered.
    graph_errors = verify_plan_builds(plan)
    if graph_errors:
        raise HTTPException(status_code=422, detail=" ".join(graph_errors))

    if plan.unsupported:
        listed = ", ".join(f"'{n.canvas_id}' ({n.node_type})" for n in plan.unsupported)
        raise HTTPException(
            status_code=422,
            detail=(
                f"These nodes cannot be packaged yet: {listed}. "
                "Remove them or replace them with supported nodes."
            ),
        )
    return plan


def _read_version(version: WorkflowVersion) -> WorkflowVersionRead:
    """Serialise a stored version, migrating its canvas to the current schema.

    Applied on read so an old workflow opens correctly without waiting for the
    backfill (scripts/migrate_canvases.py). Deliberately builds a new response
    object rather than assigning to `version.canvas_json`: get_db commits at the
    end of every request, so mutating the ORM instance would turn this GET into
    a silent write. Re-saving from the UI is what persists the migration, along
    with a freshly compiled IR.
    """
    read = WorkflowVersionRead.model_validate(version)
    if isinstance(read.canvas_json, dict) and needs_migration(read.canvas_json):
        migrated, notes = migrate_canvas(read.canvas_json)
        read.canvas_json = migrated
        # An old canvas has an IR compiled against node types that no longer
        # exist, so it cannot be executed or packaged until it is re-saved.
        read.is_valid = False
        read.validation_errors = [
            "This workflow was built before the move to A2A graph workflows and "
            "has been migrated. Save & Compile to apply it.",
            *(f"Migration: {note}" for note in notes),
        ]
    return read

async def _get_or_404(db: AsyncSession, model, id: str):
    result = await db.execute(select(model).where(model.id == id))
    obj = result.scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return obj
