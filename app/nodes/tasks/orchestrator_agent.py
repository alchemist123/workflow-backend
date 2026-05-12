"""Orchestrator Agent node — AI agent that uses MCP tools, remote A2A agents, and inline functions."""
from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata

_SAFE_BUILTINS = {
    "__builtins__": {
        k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
        for k in ("range", "len", "str", "int", "float", "bool", "list", "dict",
                  "tuple", "set", "isinstance", "hasattr", "getattr", "enumerate",
                  "zip", "map", "filter", "sorted", "sum", "min", "max", "abs", "round")
    }
}


@dataclass
class OrchestratorAgentNode(NodeDefinition):
    node_type: str = "ORCHESTRATOR_AGENT"
    version: str = "1"
    palette: PaletteMetadata = None
    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Orchestrator Agent",
            category="ai",
            color="#f59e0b",
            icon="BrainCircuit",
            description="AI agent that orchestrates MCP tools, remote agents & functions",
            wave=1,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "model": {"type": "string", "default": "gemini-2.0-flash"},
                "system_prompt": {"type": "string"},
                "max_iterations": {"type": "integer", "default": 10},
                "tool_execution_mode": {
                    "type": "string",
                    "enum": ["sequential", "parallel"],
                    "default": "sequential",
                    "description": "How tool calls in one turn are executed: one by one or all concurrently",
                },
                "output_field": {
                    "type": "string",
                    "description": "If set, extract this key from the agent result before passing downstream",
                },
                "functions": {
                    "type": "array",
                    "description": "Inline Python functions (alternative to connecting FUNCTION nodes)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "parameters": {"type": "object"},
                            "code": {"type": "string"},
                        },
                        "required": ["name", "code"],
                    },
                },
                "api_key": {
                    "type": "string",
                    "description": "Google AI Studio API key — leave empty to use GOOGLE_API_KEY env var or ADC",
                },
                "vertex_project": {
                    "type": "string",
                    "description": "Google Cloud project ID — enables Vertex AI backend for ADK",
                },
                "vertex_location": {
                    "type": "string",
                    "default": "us-central1",
                    "description": "Vertex AI region (ADK only)",
                },
                "service_account_json": {
                    "type": "string",
                    "description": "Service account key JSON for Vertex AI auth — leave empty to use ADC",
                },
                # resolved_tools is injected by the IR compiler from connected nodes
                "resolved_tools": {"type": "object"},
            },
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "result": {},
                "usage": {
                    "type": "object",
                    "properties": {
                        "input_tokens": {"type": "integer"},
                        "output_tokens": {"type": "integer"},
                    },
                },
            },
        }
        self.output_handles = ["output", "error"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        result = await _run_adk(node_config, input_data, context)

        # If output_field is configured, extract that key for downstream nodes
        output_field = node_config.get("output_field")
        if output_field and isinstance(result, dict) and output_field in result:
            return {output_field: result[output_field], "_full": result}
        return result


# ── A2A helpers ──────────────────────────────────────────────────────────────

async def _a2a_call(http, endpoint: str, message: str, auth_token: str = "") -> dict:
    """Call an A2A agent using the A2A JSON-RPC protocol (message/send)."""
    import json as _j, uuid
    hdrs = {"Content-Type": "application/json"}
    if auth_token:
        hdrs["Authorization"] = f"Bearer {auth_token}"
    url = endpoint.rstrip("/") + "/"
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message}],
                "messageId": str(uuid.uuid4()),
            }
        },
    }
    r = await http.post(url, json=body, headers=hdrs)
    data = r.json()
    # Extract the final agent text from the A2A response
    result = data.get("result", data)
    if isinstance(result, dict):
        # Task result: look in artifacts or history for the agent's last text
        for artifact in result.get("artifacts", []):
            for part in artifact.get("parts", []):
                if part.get("kind") == "text":
                    return {"result": part["text"]}
        history = result.get("history", [])
        for msg in reversed(history):
            if msg.get("role") == "agent":
                for part in msg.get("parts", []):
                    if part.get("kind") == "text":
                        return {"result": part["text"]}
    return result


# ── Tool-node logging ────────────────────────────────────────────────────────

async def _log_tool_node(ctx, node_id: str | None, node_type: str, status: str, error: str | None = None) -> None:
    """Write/update a NodeExecutionLog row for a tool-provider canvas node.

    Uses its own DB session to avoid conflicting with the engine's session
    which may already be mid-flush when this is called.
    """
    if not ctx or not getattr(ctx, "execution_id", None) or not node_id:
        return
    try:
        from app.models.workflow import NodeExecutionLog, ExecutionStatus
        from app.database import AsyncSessionLocal
        from datetime import datetime
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(NodeExecutionLog)
                .where(
                    NodeExecutionLog.execution_id == ctx.execution_id,
                    NodeExecutionLog.node_id == node_id,
                )
                .order_by(NodeExecutionLog.started_at.desc())
            )
            log = result.scalars().first()
            if status == "running":
                if not log or log.status.value != "running":
                    log = NodeExecutionLog(
                        execution_id=ctx.execution_id,
                        node_id=node_id,
                        node_type=node_type,
                        status=ExecutionStatus.RUNNING,
                        started_at=datetime.utcnow(),
                    )
                    db.add(log)
            elif log:
                log.status = ExecutionStatus(status)
                log.error = error
                log.finished_at = datetime.utcnow()
            await db.commit()
    except Exception:
        pass  # tool-node status is best-effort; never let logging break execution


async def _run_anthropic(config: dict, input_data: dict, ctx=None) -> dict:
    """Kept for backwards compat — delegates to ADK."""
    return await _run_adk(config, input_data, ctx)


# ── Google ADK ────────────────────────────────────────────────────────────────

async def _run_adk(config: dict, input_data: dict, ctx=None) -> dict:
    try:
        from google.adk.agents import LlmAgent
        from google.adk.sessions import InMemorySessionService
        from google.adk.runners import Runner
        import google.genai.types as genai_types
    except ImportError:
        return {"error": "google-adk not installed", "result": None}

    import json as _json
    import uuid
    from app.config import get_settings

    import os
    settings = get_settings()
    vertex_project = config.get("vertex_project") or settings.effective_vertex_project
    vertex_location = config.get("vertex_location") or settings.effective_vertex_location
    sa_json = config.get("service_account_json") or settings.google_service_account_json
    api_key = config.get("api_key") or settings.google_api_key

    if vertex_project:
        # Tell google-genai (which ADK uses internally) to route through Vertex AI.
        # GOOGLE_APPLICATION_CREDENTIALS / ADC handles auth; no vertexai.init() needed.
        os.environ["GOOGLE_CLOUD_PROJECT"] = vertex_project
        os.environ["GOOGLE_CLOUD_LOCATION"] = vertex_location
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
        os.environ.pop("GOOGLE_API_KEY", None)  # ensure AI Studio key is not used

        if sa_json:
            # Write SA key to a temp file and point GOOGLE_APPLICATION_CREDENTIALS at it
            try:
                import tempfile
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
                tmp.write(sa_json)
                tmp.flush()
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
            except Exception:
                pass
    elif api_key:
        os.environ["GOOGLE_API_KEY"] = api_key
        os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)

    import asyncio
    import httpx
    from google.adk.tools import FunctionTool

    resolved = config.get("resolved_tools") or {}
    mcp_servers = (resolved.get("mcp_servers") or []) + (config.get("mcp_servers") or [])
    a2a_agents  = (resolved.get("a2a_agents")  or []) + (config.get("a2a_agents")  or [])
    functions   = (resolved.get("functions")   or []) + (config.get("functions")   or [])

    adk_tools: list = []

    # ── MCP servers — discover tools via official MCP client ──────────────────
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession as _McpSession
        _mcp_ok = True
    except ImportError:
        _mcp_ok = False

    def _make_mcp_tool(srv_cfg: dict, tool, mcp_url: str, extra_hdrs: dict):
        """Wrap a single MCP tool as an ADK FunctionTool using the MCP client."""
        nid = srv_cfg.get("node_id")
        ntype = srv_cfg.get("node_type", "TOOL")
        tname = tool.name
        desc = tool.description or f"MCP tool {tname}"

        async def mcp_fn(**kwargs) -> dict:
            await _log_tool_node(ctx, nid, ntype, "running")
            try:
                async with streamablehttp_client(
                    mcp_url, headers=extra_hdrs or None, timeout=30
                ) as (r, w, _):
                    async with _McpSession(r, w) as s:
                        await s.initialize()
                        res = await s.call_tool(tname, arguments=kwargs or None)
                if res.isError:
                    raise RuntimeError(
                        "; ".join(c.text for c in res.content if hasattr(c, "text"))
                        or "MCP tool returned isError=True"
                    )
                await _log_tool_node(ctx, nid, ntype, "success")
                # Prefer structured content (dict); fall back to joined text parts
                if res.structuredContent:
                    return res.structuredContent
                texts = [c.text for c in res.content if hasattr(c, "text")]
                return {"result": "\n".join(texts)} if texts else {"result": str(res.content)}
            except Exception as exc:
                await _log_tool_node(ctx, nid, ntype, "failed", str(exc))
                return {"error": str(exc)}

        fn_name = f"{srv_cfg['name']}__{tname}".replace("-", "_").replace(".", "_")
        mcp_fn.__name__ = fn_name
        mcp_fn.__doc__ = desc
        return FunctionTool(mcp_fn)

    for srv in mcp_servers:
        if not _mcp_ok:
            break
        raw = srv["url"].rstrip("/")
        mcp_url = raw if raw.endswith("/mcp") else raw + "/mcp"
        extra_hdrs = srv.get("auth") or {}
        try:
            async with streamablehttp_client(
                mcp_url, headers=extra_hdrs or None, timeout=15
            ) as (read, write, _):
                async with _McpSession(read, write) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    for tool in tools_result.tools:
                        adk_tools.append(_make_mcp_tool(srv, tool, mcp_url, extra_hdrs))
        except Exception:
            pass

    # ── A2A remote agents as callable tools ───────────────────────────────────
    for ag in a2a_agents:
        def _make_a2a(agent_cfg: dict):
            nid = agent_cfg.get("node_id")
            ntype = agent_cfg.get("node_type", "REMOTE_AGENT")
            async def call_remote_agent(message: str) -> dict:
                await _log_tool_node(ctx, nid, ntype, "running")
                try:
                    async with httpx.AsyncClient(timeout=60) as http:
                        result = await _a2a_call(http, agent_cfg["endpoint"], message, agent_cfg.get("auth_token", ""))
                    await _log_tool_node(ctx, nid, ntype, "success")
                    return result
                except Exception as exc:
                    await _log_tool_node(ctx, nid, ntype, "failed", str(exc))
                    return {"error": str(exc)}
            call_remote_agent.__name__ = f"a2a_{agent_cfg['name'].replace('-', '_').replace(' ', '_')}"
            call_remote_agent.__doc__ = agent_cfg.get(
                "description", f"Delegate task to remote agent '{agent_cfg['name']}'. Args: message (str) — the task to delegate."
            )
            return FunctionTool(call_remote_agent)
        adk_tools.append(_make_a2a(ag))

    # ── Inline Python functions ────────────────────────────────────────────────
    for fn in functions:
        fn_name = fn.get("name", "")
        if not fn_name:
            continue
        def _make_fn(fn_def: dict):
            nid = fn_def.get("node_id")
            ntype = fn_def.get("node_type", "FUNCTION")
            async def inline_fn(**kwargs):
                await _log_tool_node(ctx, nid, ntype, "running")
                try:
                    local_ns = {**kwargs, "data": kwargs, "input_data": kwargs}
                    exec(compile(fn_def["code"], "<function>", "exec"), dict(_SAFE_BUILTINS), local_ns)
                    result = local_ns.get("result", local_ns.get("output"))
                    out = {"result": result} if not isinstance(result, dict) else result
                    await _log_tool_node(ctx, nid, ntype, "success")
                    return out
                except Exception as exc:
                    await _log_tool_node(ctx, nid, ntype, "failed", str(exc))
                    return {"error": str(exc)}
            inline_fn.__name__ = fn_def["name"]
            inline_fn.__doc__ = fn_def.get("description", f"Function {fn_def['name']}")
            return FunctionTool(inline_fn)
        adk_tools.append(_make_fn(fn))

    base_instruction = config.get("system_prompt", "You are a helpful assistant.")
    if adk_tools:
        tool_names = ", ".join(
            t.func.__name__ if hasattr(t, "func") else str(t)
            for t in adk_tools
        )
        base_instruction = (
            f"{base_instruction}\n\n"
            f"You MUST use the available tools to handle every request. "
            f"Available tools: {tool_names}. "
            f"Never answer from your own knowledge — always delegate to a tool."
        )

    agent = LlmAgent(
        name="workflow_agent",
        model=config.get("model", "gemini-2.0-flash"),
        description=config.get("system_prompt", ""),
        instruction=base_instruction,
        tools=adk_tools,
    )
    session_service = InMemorySessionService()
    app_name, user_id = "nocode_workflow", "runner"
    session_id = str(uuid.uuid4())
    await session_service.create_session(app_name=app_name, user_id=user_id, session_id=session_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)

    content = genai_types.Content(role="user", parts=[genai_types.Part(text=_json.dumps(input_data))])
    final_text = ""
    try:
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if part.text:
                        final_text += part.text
    except Exception as exc:
        err = str(exc)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            await asyncio.sleep(5)
            return {"error": f"Google API rate limit: {err}", "_error": True}
        return {"error": err, "_error": True}
    try:
        return _json.loads(final_text)
    except Exception:
        return {"result": final_text}
