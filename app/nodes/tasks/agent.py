from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class AgentNode(NodeDefinition):
    node_type: str = "AGENT"
    version: str = "1"
    palette: PaletteMetadata = None
    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Agent",
            category="ai",
            color="#f59e0b",
            icon="Bot",
            description="AI agent (ADK) with MCP tools and A2A connections",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "default": "gemini-2.0-flash",
                    "description": "Gemini model ID (e.g. gemini-2.0-flash, gemini-1.5-pro)",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "Agent system prompt / instructions",
                },
                "mcp_servers": {
                    "type": "array",
                    "description": "MCP servers to connect as tools or data sources",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Server alias used as tool-name prefix"},
                            "url": {"type": "string", "description": "MCP server base URL"},
                            "transport": {"type": "string", "enum": ["http", "sse"], "default": "http"},
                            "auth": {"type": "object", "description": "Optional HTTP headers for auth"},
                        },
                        "required": ["name", "url"],
                    },
                },
                "a2a_agents": {
                    "type": "array",
                    "description": "Other agents reachable via the A2A protocol",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "endpoint": {"type": "string", "description": "A2A agent endpoint URL"},
                            "description": {"type": "string", "description": "What this agent does (shown to the LLM)"},
                        },
                        "required": ["name", "endpoint"],
                    },
                },
                "max_iterations": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max agentic-loop iterations before giving up",
                },
                "api_key": {
                    "type": "string",
                    "description": "Google AI Studio API key — leave empty to use GOOGLE_API_KEY env var or ADC",
                },
                "vertex_project": {
                    "type": "string",
                    "description": "GCP project ID — enables Vertex AI backend",
                },
                "vertex_location": {
                    "type": "string",
                    "default": "us-central1",
                    "description": "Vertex AI region",
                },
            },
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "result": {},
                "usage": {"type": "object"},
            },
        }
        self.output_handles = ["output", "error"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        return await _run_adk_agent(node_config, input_data, context)


# ── Google ADK agent ──────────────────────────────────────────────────────────

async def _run_adk_agent(config: dict, input_data: dict, ctx=None) -> dict:
    try:
        from google.adk.agents import LlmAgent
        from google.adk.sessions import InMemorySessionService
        from google.adk.runners import Runner
        from google.adk.tools import FunctionTool
        import google.genai.types as genai_types
    except ImportError:
        return {"error": "google-adk not installed. Run: pip install google-adk", "result": None}

    import json as _json
    import uuid
    import os
    import httpx

    from app.nodes.tasks.orchestrator_agent import (
        _mcp_url, _mcp_init_session, _mcp_headers,
        _mcp_parse_tools_list, _mcp_parse_call_result, _a2a_call,
    )

    vertex_project = config.get("vertex_project") or ""
    vertex_location = config.get("vertex_location") or "us-central1"
    api_key = config.get("api_key") or ""

    if vertex_project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = vertex_project
        os.environ["GOOGLE_CLOUD_LOCATION"] = vertex_location
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
        os.environ.pop("GOOGLE_API_KEY", None)
    elif api_key:
        os.environ["GOOGLE_API_KEY"] = api_key
        os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)

    mcp_servers: list = config.get("mcp_servers") or []
    a2a_agents: list = config.get("a2a_agents") or []
    adk_tools: list = []

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
                    def _make_mcp_tool(srv_cfg, tool_name, full_name, desc):
                        async def mcp_fn(**kwargs) -> dict:
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
                                return _mcp_parse_call_result(r)
                            except Exception as exc:
                                return {"error": str(exc)}
                        mcp_fn.__name__ = full_name.replace("-", "_").replace(".", "_")
                        mcp_fn.__doc__ = desc or f"MCP tool {full_name}"
                        return FunctionTool(mcp_fn)
                    full = f"{srv['name']}__{tool['name']}"
                    adk_tools.append(_make_mcp_tool(srv, tool["name"], full, tool.get("description", "")))
            except Exception:
                pass

    for ag in a2a_agents:
        def _make_a2a(agent_cfg):
            async def call_agent(message: str) -> dict:
                try:
                    async with httpx.AsyncClient(timeout=60) as h:
                        return await _a2a_call(h, agent_cfg["endpoint"], message, agent_cfg.get("auth_token", ""))
                except Exception as exc:
                    return {"error": str(exc)}
            call_agent.__name__ = f"a2a_{agent_cfg['name'].replace('-', '_').replace(' ', '_')}"
            call_agent.__doc__ = agent_cfg.get("description", f"Delegate to remote agent '{agent_cfg['name']}'")
            return FunctionTool(call_agent)
        adk_tools.append(_make_a2a(ag))

    instruction = config.get("system_prompt", "You are a helpful assistant.")
    agent = LlmAgent(
        name="workflow_agent",
        model=config.get("model", "gemini-2.0-flash"),
        description=instruction,
        instruction=instruction,
        tools=adk_tools,
    )
    session_service = InMemorySessionService()
    app_name, user_id = "nocode_workflow", "runner"
    session_id = str(uuid.uuid4())
    await session_service.create_session(app_name=app_name, user_id=user_id, session_id=session_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)

    content = genai_types.Content(role="user", parts=[genai_types.Part(text=_json.dumps(input_data))])
    final_text = ""
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    final_text += part.text

    try:
        return _json.loads(final_text)
    except Exception:
        return {"result": final_text}
