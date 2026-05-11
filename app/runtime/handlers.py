"""Concrete execution handlers for each node kind."""
from typing import Any
import httpx
from app.config import get_settings

settings = get_settings()


async def run_agent(config: dict, input_data: dict, context: Any) -> dict:
    """Invoke an AgentResource via Anthropic API."""
    try:
        import anthropic
        from app.database import AsyncSessionLocal
        from app.models.resources import AgentResource, ModelResource
        from sqlalchemy import select

        agent_id = config["agent_id"]
        async with AsyncSessionLocal() as session:
            agent = (await session.execute(select(AgentResource).where(AgentResource.id == agent_id))).scalar_one_or_none()
            if not agent:
                return {"error": f"Agent '{agent_id}' not found", "_error": True}

            model_id = agent.model_id
            model_name = "claude-sonnet-4-6"
            if model_id:
                model_res = (await session.execute(select(ModelResource).where(ModelResource.id == model_id))).scalar_one_or_none()
                if model_res:
                    model_name = model_res.model_id

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        messages = [{"role": "user", "content": str(input_data)}]

        response = await client.messages.create(
            model=model_name,
            max_tokens=1024,
            system=agent.system_prompt or "",
            messages=messages,
        )
        content = response.content[0].text if response.content else ""
        return {
            "result": content,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }
    except Exception as e:
        return {"error": str(e), "_error": True}


async def run_model(config: dict, input_data: dict, context: Any) -> dict:
    """Call an LLM via a ModelResource."""
    try:
        from jinja2 import Environment, sandbox
        env = sandbox.SandboxedEnvironment()
        prompt = env.from_string(config.get("prompt_template", "")).render(**input_data)

        import anthropic
        from app.database import AsyncSessionLocal
        from app.models.resources import ModelResource
        from sqlalchemy import select

        model_id = config["model_id"]
        async with AsyncSessionLocal() as session:
            model_res = (await session.execute(select(ModelResource).where(ModelResource.id == model_id))).scalar_one_or_none()

        model_name = model_res.model_id if model_res else "claude-sonnet-4-6"
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        response = await client.messages.create(
            model=model_name,
            max_tokens=config.get("max_tokens", 1024),
            system=config.get("system_prompt", ""),
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.content[0].text if response.content else ""

        parsed = None
        if config.get("response_format") == "json":
            import json
            try:
                parsed = json.loads(content)
            except Exception:
                parsed = None

        return {
            "content": content,
            "parsed": parsed,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }
    except Exception as e:
        return {"error": str(e), "_error": True}


async def run_tool(config: dict, input_data: dict, context: Any) -> dict:
    """Execute a ToolResource."""
    try:
        from app.database import AsyncSessionLocal
        from app.models.resources import ToolResource
        from sqlalchemy import select

        tool_id = config["tool_id"]
        async with AsyncSessionLocal() as session:
            tool = (await session.execute(select(ToolResource).where(ToolResource.id == tool_id))).scalar_one_or_none()
            if not tool:
                return {"error": f"Tool '{tool_id}' not found", "_error": True}

        impl = tool.implementation
        tool_type = tool.tool_type

        if tool_type == "http":
            url = impl.get("url", "")
            method = impl.get("method", "POST").upper()
            headers = impl.get("headers", {})
            mapping = config.get("input_mapping", {})
            body = {k: input_data.get(v, v) for k, v in mapping.items()} if mapping else input_data

            async with httpx.AsyncClient() as client:
                resp = await client.request(method, url, json=body, headers=headers, timeout=30)
                return {"status_code": resp.status_code, "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text}

        return {"result": None, "warning": f"Unsupported tool_type: {tool_type}"}
    except Exception as e:
        return {"error": str(e), "_error": True}


async def run_datasource(config: dict, input_data: dict, context: Any) -> dict:
    return {"rows": [], "count": 0, "note": "DataSource execution not yet implemented"}


async def run_subworkflow(config: dict, input_data: dict, context: Any) -> dict:
    return {"result": None, "note": "SubWorkflow execution not yet implemented"}
