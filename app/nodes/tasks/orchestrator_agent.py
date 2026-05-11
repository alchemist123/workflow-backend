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
                "framework": {
                    "type": "string",
                    "enum": ["anthropic", "langgraph", "adk"],
                    "default": "anthropic",
                },
                "model": {"type": "string", "default": "claude-opus-4-7"},
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
                    "description": "API key — Anthropic key for anthropic/langgraph, Google AI Studio key for ADK without Vertex AI",
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
        framework = node_config.get("framework", "anthropic")
        if framework == "anthropic":
            result = await _run_anthropic(node_config, input_data, context)
        elif framework == "langgraph":
            result = await _run_langgraph(node_config, input_data)
        elif framework == "adk":
            result = await _run_adk(node_config, input_data, context)
        else:
            raise ValueError(f"Unknown framework: {framework!r}")

        # If output_field is configured, extract that key for downstream nodes
        output_field = node_config.get("output_field")
        if output_field and isinstance(result, dict) and output_field in result:
            return {output_field: result[output_field], "_full": result}
        return result


# ── Shared MCP / A2A helpers ─────────────────────────────────────────────────

def _mcp_url(base_url: str) -> str:
    """Return the correct MCP endpoint URL.
    FastMCP Streamable HTTP mounts at /mcp; plain MCP servers use the base URL."""
    url = base_url.rstrip("/")
    if not url.endswith("/mcp"):
        return url + "/mcp"
    return url


async def _mcp_init_session(http, url: str, extra_headers: dict) -> str | None:
    """Initialize a FastMCP Streamable HTTP session and return the session ID."""
    try:
        r = await http.post(
            url,
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05",
                             "capabilities": {},
                             "clientInfo": {"name": "nocode-platform", "version": "1.0"}}},
            headers={"Accept": "application/json, text/event-stream", **extra_headers},
        )
        return r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
    except Exception:
        return None


def _mcp_headers(extra: dict, session_id: str | None) -> dict:
    h = {"Accept": "application/json, text/event-stream", **extra}
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _mcp_parse_tools_list(r) -> list:
    """Parse tools from either SSE event-stream or plain JSON response."""
    import json as _j
    text = r.text if hasattr(r, "text") else ""
    # SSE: lines like "data: {...}"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            try:
                obj = _j.loads(payload)
                tools = obj.get("result", {}).get("tools", [])
                if isinstance(tools, list):
                    return tools
            except Exception:
                pass
    # Plain JSON fallback
    try:
        return r.json().get("result", {}).get("tools", [])
    except Exception:
        return []


def _mcp_parse_call_result(r):
    """Parse a tools/call result from SSE or plain JSON response."""
    import json as _j
    text = r.text if hasattr(r, "text") else ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            try:
                obj = _j.loads(payload)
                result = obj.get("result")
                if result is not None:
                    return result
            except Exception:
                pass
    try:
        return r.json().get("result", r.json())
    except Exception:
        return {"error": "Could not parse MCP response"}


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


# ── Anthropic agentic loop ────────────────────────────────────────────────────

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


async def _dispatch_tool_call(tu, mcp_tool_map, a2a_map, fn_map, _json, ctx=None) -> dict:
    """Execute one tool call and return the tool_result block."""
    import httpx

    if tu.name in mcp_tool_map:
        srv, real_name = mcp_tool_map[tu.name]
        node_id = srv.get("node_id")
        node_type = srv.get("node_type", "TOOL")
        await _log_tool_node(ctx, node_id, node_type, "running")
        try:
            mcp_base = _mcp_url(srv["url"])
            extra_hdrs = srv.get("auth") or {}
            async with httpx.AsyncClient(timeout=30) as http:
                sid = await _mcp_init_session(http, mcp_base, extra_hdrs)
                r = await http.post(
                    mcp_base,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": real_name, "arguments": tu.input}},
                    headers=_mcp_headers(extra_hdrs, sid),
                )
            content = _json.dumps(_mcp_parse_call_result(r))
            await _log_tool_node(ctx, node_id, node_type, "success")
        except Exception as exc:
            content = _json.dumps({"error": str(exc)})
            await _log_tool_node(ctx, node_id, node_type, "failed", str(exc))

    elif tu.name in a2a_map:
        ag = a2a_map[tu.name]
        node_id = ag.get("node_id")
        node_type = ag.get("node_type", "REMOTE_AGENT")
        await _log_tool_node(ctx, node_id, node_type, "running")
        try:
            msg = tu.input.get("message", _json.dumps(tu.input))
            async with httpx.AsyncClient(timeout=60) as http:
                result = await _a2a_call(http, ag["endpoint"], msg, ag.get("auth_token", ""))
            content = _json.dumps(result)
            await _log_tool_node(ctx, node_id, node_type, "success")
        except Exception as exc:
            content = _json.dumps({"error": str(exc)})
            await _log_tool_node(ctx, node_id, node_type, "failed", str(exc))

    elif tu.name in fn_map:
        fn = fn_map[tu.name]
        node_id = fn.get("node_id")
        node_type = fn.get("node_type", "FUNCTION")
        await _log_tool_node(ctx, node_id, node_type, "running")
        try:
            local_ns = {**tu.input, "data": tu.input, "input_data": tu.input}
            exec(compile(fn["code"], "<function>", "exec"), dict(_SAFE_BUILTINS), local_ns)
            result = local_ns.get("result", local_ns.get("output"))
            content = _json.dumps({"result": result} if not isinstance(result, dict) else result)
            await _log_tool_node(ctx, node_id, node_type, "success")
        except Exception as exc:
            content = _json.dumps({"error": str(exc)})
            await _log_tool_node(ctx, node_id, node_type, "failed", str(exc))

    else:
        content = _json.dumps({"error": f"Unknown tool: {tu.name}"})

    return {"type": "tool_result", "tool_use_id": tu.id, "content": content}


async def _run_anthropic(config: dict, input_data: dict, ctx=None) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic package not installed", "result": None}

    import asyncio
    import json as _json
    import httpx

    model = config.get("model", "claude-opus-4-7")
    system_prompt = config.get("system_prompt", "You are a helpful assistant.")
    max_iterations: int = config.get("max_iterations", 10)
    parallel: bool = config.get("tool_execution_mode", "sequential") == "parallel"

    # resolved_tools are injected by the IR compiler from connected nodes.
    # Falls back to inline config fields for standalone / backwards compat.
    resolved = config.get("resolved_tools") or {}
    mcp_servers = (resolved.get("mcp_servers") or []) + (config.get("mcp_servers") or [])
    a2a_agents  = (resolved.get("a2a_agents")  or []) + (config.get("a2a_agents")  or [])
    functions   = (resolved.get("functions")   or []) + (config.get("functions")   or [])

    from app.config import get_settings
    settings = get_settings()
    api_key = config.get("api_key") or settings.anthropic_api_key
    client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
    tools: list[dict] = []
    mcp_tool_map: dict[str, tuple[dict, str]] = {}
    a2a_map: dict[str, dict] = {}
    fn_map: dict[str, dict] = {}

    # Discover tools from every MCP server
    async with httpx.AsyncClient(timeout=15) as http:
        for srv in mcp_servers:
            try:
                mcp_base = _mcp_url(srv["url"])
                extra_hdrs = srv.get("auth") or {}
                sid = await _mcp_init_session(http, mcp_base, extra_hdrs)
                resp = await http.post(
                    mcp_base,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers=_mcp_headers(extra_hdrs, sid),
                )
                for tool in _mcp_parse_tools_list(resp):
                    full = f"{srv['name']}__{tool['name']}"
                    tools.append({
                        "name": full,
                        "description": tool.get("description", ""),
                        "input_schema": tool.get("inputSchema", {"type": "object"}),
                    })
                    mcp_tool_map[full] = (srv, tool["name"])
            except Exception:
                pass

    # A2A remote agents as callable tools
    for ag in a2a_agents:
        tname = f"a2a__{ag['name']}"
        tools.append({
            "name": tname,
            "description": ag.get("description", f"Delegate task to '{ag['name']}'"),
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string", "description": "Task description"}},
                "required": ["message"],
            },
        })
        a2a_map[tname] = ag

    # Inline Python functions as callable tools
    for fn in functions:
        fn_name = fn.get("name", "")
        if not fn_name:
            continue
        tools.append({
            "name": fn_name,
            "description": fn.get("description", f"Function {fn_name}"),
            "input_schema": fn.get("parameters") or {"type": "object"},
        })
        fn_map[fn_name] = fn

    messages: list[dict] = [{"role": "user", "content": _json.dumps(input_data)}]
    in_tokens = out_tokens = 0

    for iteration in range(max_iterations):
        kwargs: dict = {
            "model": model, "max_tokens": 4096,
            "system": system_prompt, "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            # Force at least one tool call on the first turn so the orchestrator
            # always delegates rather than answering from its own knowledge.
            if iteration == 0:
                kwargs["tool_choice"] = {"type": "any"}

        resp = await client.messages.create(**kwargs)
        in_tokens += resp.usage.input_tokens
        out_tokens += resp.usage.output_tokens

        if resp.stop_reason == "end_turn":
            texts = [b.text for b in resp.content if hasattr(b, "text")]
            text = texts[-1] if texts else ""
            try:
                data = _json.loads(text)
            except Exception:
                data = {"result": text}
            return {**data, "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens}}

        if resp.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": resp.content})
        tool_use_blocks = [b for b in resp.content if b.type == "tool_use"]

        if parallel:
            # All tool calls in this turn run concurrently
            tool_results = await asyncio.gather(*[
                _dispatch_tool_call(tu, mcp_tool_map, a2a_map, fn_map, _json, ctx)
                for tu in tool_use_blocks
            ])
            tool_results = list(tool_results)
        else:
            # Sequential — one at a time (default; safe for dependent calls)
            tool_results = []
            for tu in tool_use_blocks:
                tool_results.append(
                    await _dispatch_tool_call(tu, mcp_tool_map, a2a_map, fn_map, _json, ctx)
                )

        messages.append({"role": "user", "content": tool_results})

    return {"result": None, "error": "max_iterations_reached",
            "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens}}


# ── LangGraph ─────────────────────────────────────────────────────────────────

async def _run_langgraph(config: dict, input_data: dict) -> dict:
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langgraph.prebuilt import create_react_agent
    except ImportError:
        return {"error": "langgraph / langchain-anthropic / langchain-mcp-adapters not installed", "result": None}

    import json as _json
    from app.config import get_settings

    settings = get_settings()
    api_key = config.get("api_key") or settings.anthropic_api_key

    resolved = config.get("resolved_tools") or {}
    mcp_servers = (resolved.get("mcp_servers") or []) + (config.get("mcp_servers") or [])
    mcp_config = {s["name"]: {"url": s["url"], "transport": s.get("transport", "http")} for s in mcp_servers}

    llm_kwargs = {"model": config.get("model", "claude-opus-4-7")}
    if api_key:
        llm_kwargs["anthropic_api_key"] = api_key
    llm = ChatAnthropic(**llm_kwargs)
    async with MultiServerMCPClient(mcp_config) as mcp:
        agent = create_react_agent(llm, mcp.get_tools())
        result = await agent.ainvoke({"messages": [{"role": "user", "content": _json.dumps(input_data)}]})
        last = result["messages"][-1]
        content = last.content if hasattr(last, "content") else str(last)
        try:
            return _json.loads(content)
        except Exception:
            return {"result": content}


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

    # ── MCP servers — discover tools then wrap each as a FunctionTool ─────────
    async with httpx.AsyncClient(timeout=15) as http:
        for srv in mcp_servers:
            try:
                mcp_base = _mcp_url(srv["url"])
                extra_hdrs = srv.get("auth") or {}
                sid = await _mcp_init_session(http, mcp_base, extra_hdrs)
                resp = await http.post(
                    mcp_base,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers=_mcp_headers(extra_hdrs, sid),
                )
                for tool in _mcp_parse_tools_list(resp):
                    def _make_mcp_tool(srv_cfg: dict, tool_name: str, full_name: str, desc: str):
                        nid = srv_cfg.get("node_id")
                        ntype = srv_cfg.get("node_type", "TOOL")
                        async def mcp_fn(**kwargs) -> dict:
                            await _log_tool_node(ctx, nid, ntype, "running")
                            try:
                                base = _mcp_url(srv_cfg["url"])
                                eh = srv_cfg.get("auth") or {}
                                async with httpx.AsyncClient(timeout=30) as h:
                                    s = await _mcp_init_session(h, base, eh)
                                    r = await h.post(
                                        base,
                                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": tool_name, "arguments": kwargs}},
                                        headers=_mcp_headers(eh, s),
                                    )
                                result = _mcp_parse_call_result(r)
                                await _log_tool_node(ctx, nid, ntype, "success")
                                return result
                            except Exception as exc:
                                await _log_tool_node(ctx, nid, ntype, "failed", str(exc))
                                return {"error": str(exc)}
                        mcp_fn.__name__ = full_name.replace("-", "_").replace(".", "_")
                        mcp_fn.__doc__ = desc or f"MCP tool {full_name}"
                        return FunctionTool(mcp_fn)
                    full = f"{srv['name']}__{tool['name']}"
                    adk_tools.append(_make_mcp_tool(srv, tool["name"], full, tool.get("description", "")))
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
