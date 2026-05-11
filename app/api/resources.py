import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.resources import AgentResource, ModelResource, ToolResource, DataSourceResource
from app.schemas.resources import (
    AgentCreate, AgentRead, ModelCreate, ModelRead,
    ToolCreate, ToolRead, DataSourceCreate, DataSourceRead,
)

router = APIRouter(tags=["resources"])


# ─── Agents ──────────────────────────────────────────────────────────────────

agent_router = APIRouter(prefix="/agents")


@agent_router.post("", response_model=AgentRead)
async def create_agent(body: AgentCreate, db: AsyncSession = Depends(get_db)):
    agent = AgentResource(id=str(uuid.uuid4()), **body.model_dump())
    db.add(agent)
    await db.flush()
    return agent


@agent_router.get("", response_model=list[AgentRead])
async def list_agents(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AgentResource).order_by(AgentResource.name))
    return result.scalars().all()


@agent_router.get("/{agent_id}", response_model=AgentRead)
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    return await _get_or_404(db, AgentResource, agent_id)


@agent_router.patch("/{agent_id}", response_model=AgentRead)
async def update_agent(agent_id: str, body: AgentCreate, db: AsyncSession = Depends(get_db)):
    agent = await _get_or_404(db, AgentResource, agent_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(agent, k, v)
    await db.flush()
    return agent


@agent_router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await _get_or_404(db, AgentResource, agent_id)
    await db.delete(agent)


# ─── Models ──────────────────────────────────────────────────────────────────

model_router = APIRouter(prefix="/models")


@model_router.post("", response_model=ModelRead)
async def create_model(body: ModelCreate, db: AsyncSession = Depends(get_db)):
    model = ModelResource(id=str(uuid.uuid4()), **body.model_dump())
    db.add(model)
    await db.flush()
    return model


@model_router.get("", response_model=list[ModelRead])
async def list_models(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelResource).order_by(ModelResource.name))
    return result.scalars().all()


@model_router.get("/{model_id}", response_model=ModelRead)
async def get_model(model_id: str, db: AsyncSession = Depends(get_db)):
    return await _get_or_404(db, ModelResource, model_id)


@model_router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: str, db: AsyncSession = Depends(get_db)):
    model = await _get_or_404(db, ModelResource, model_id)
    await db.delete(model)


# ─── Tools ───────────────────────────────────────────────────────────────────

tool_router = APIRouter(prefix="/tools")


@tool_router.post("", response_model=ToolRead)
async def create_tool(body: ToolCreate, db: AsyncSession = Depends(get_db)):
    tool = ToolResource(id=str(uuid.uuid4()), **body.model_dump())
    db.add(tool)
    await db.flush()
    return tool


@tool_router.get("", response_model=list[ToolRead])
async def list_tools(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ToolResource).order_by(ToolResource.name))
    return result.scalars().all()


@tool_router.get("/{tool_id}", response_model=ToolRead)
async def get_tool(tool_id: str, db: AsyncSession = Depends(get_db)):
    return await _get_or_404(db, ToolResource, tool_id)


@tool_router.delete("/{tool_id}", status_code=204)
async def delete_tool(tool_id: str, db: AsyncSession = Depends(get_db)):
    tool = await _get_or_404(db, ToolResource, tool_id)
    await db.delete(tool)


# ─── DataSources ─────────────────────────────────────────────────────────────

datasource_router = APIRouter(prefix="/datasources")


@datasource_router.post("", response_model=DataSourceRead)
async def create_datasource(body: DataSourceCreate, db: AsyncSession = Depends(get_db)):
    ds = DataSourceResource(id=str(uuid.uuid4()), **body.model_dump())
    db.add(ds)
    await db.flush()
    return ds


@datasource_router.get("", response_model=list[DataSourceRead])
async def list_datasources(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DataSourceResource).order_by(DataSourceResource.name))
    return result.scalars().all()


@datasource_router.delete("/{ds_id}", status_code=204)
async def delete_datasource(ds_id: str, db: AsyncSession = Depends(get_db)):
    ds = await _get_or_404(db, DataSourceResource, ds_id)
    await db.delete(ds)


# ─── helpers ─────────────────────────────────────────────────────────────────

async def _get_or_404(db: AsyncSession, model, id: str):
    result = await db.execute(select(model).where(model.id == id))
    obj = result.scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return obj


def include_resource_routers(app):
    app.include_router(agent_router)
    app.include_router(model_router)
    app.include_router(tool_router)
    app.include_router(datasource_router)
