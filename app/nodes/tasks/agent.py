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
            description="AI agent with MCP tools and A2A connections",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "framework": {
                    "type": "string",
                    "enum": ["anthropic", "langgraph", "adk"],
                    "default": "anthropic",
                    "description": "Agent execution framework",
                },
                "model": {
                    "type": "string",
                    "default": "claude-opus-4-7",
                    "description": "LLM model ID (e.g. claude-opus-4-7, gemini-2.0-flash)",
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
                            "name": {
                                "type": "string",
                                "description": "Server alias used as tool-name prefix",
                            },
                            "url": {
                                "type": "string",
                                "description": "MCP server base URL (JSON-RPC over HTTP/SSE)",
                            },
                            "transport": {
                                "type": "string",
                                "enum": ["http", "sse"],
                                "default": "http",
                            },
                            "auth": {
                                "type": "object",
                                "description": "Optional HTTP headers for auth",
                            },
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
                            "endpoint": {
                                "type": "string",
                                "description": "A2A agent endpoint URL",
                            },
                            "description": {
                                "type": "string",
                                "description": "What this agent does (shown to the LLM)",
                            },
                        },
                        "required": ["name", "endpoint"],
                    },
                },
                "max_iterations": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max agentic-loop iterations before giving up",
                },
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
            return await _run_anthropic_agent(node_config, input_data)
        if framework == "langgraph":
            return await _run_langgraph_agent(node_config, input_data)
        if framework == "adk":
            return await _run_adk_agent(node_config, input_data)
        raise ValueError(f"Unknown agent framework: {framework!r}")


# ── Anthropic agentic loop with MCP + A2A ─────────────────────────────────────

async def _run_anthropic_agent(config: dict, input_data: dict) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic package not installed", "result": None}

    import json as _json
    import httpx

    model = config.get("model", "claude-opus-4-7")
    system_prompt = config.get("system_prompt", "You are a helpful assistant.")
    mcp_servers: list[dict] = config.get("mcp_servers") or []
    a2a_agents: list[dict] = config.get("a2a_agents") or []
    max_iterations: int = config.get("max_iterations", 10)

    client = anthropic.Anthropic()
    tools: list[dict] = []
    mcp_tool_map: dict[str, tuple[dict, str]] = {}  # full_name -> (server, real_tool_name)

    # Discover tools from every MCP server
    async with httpx.AsyncClient(timeout=15) as http:
        for srv in mcp_servers:
            try:
                resp = await http.post(
                    srv["url"],
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers=srv.get("auth") or {},
                )
                for tool in resp.json().get("result", {}).get("tools", []):
                    full = f"{srv['name']}__{tool['name']}"
                    tools.append({
                        "name": full,
                        "description": tool.get("description", ""),
                        "input_schema": tool.get("inputSchema", {"type": "object"}),
                    })
                    mcp_tool_map[full] = (srv, tool["name"])
            except Exception:
                pass  # unreachable server — skip silently

    # Expose A2A peers as callable tools
    for ag in a2a_agents:
        tools.append({
            "name": f"a2a__{ag['name']}",
            "description": ag.get("description", f"Delegate a task to the '{ag['name']}' agent"),
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string", "description": "Task description for the agent"}},
                "required": ["message"],
            },
        })

    messages: list[dict] = [{"role": "user", "content": _json.dumps(input_data)}]
    in_tokens = out_tokens = 0

    for _ in range(max_iterations):
        kwargs: dict = {
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        resp = client.messages.create(**kwargs)
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
        tool_results: list[dict] = []

        async with httpx.AsyncClient(timeout=30) as http:
            for tu in (b for b in resp.content if b.type == "tool_use"):
                if tu.name in mcp_tool_map:
                    srv, real_name = mcp_tool_map[tu.name]
                    try:
                        r = await http.post(
                            srv["url"],
                            json={
                                "jsonrpc": "2.0", "id": 1,
                                "method": "tools/call",
                                "params": {"name": real_name, "arguments": tu.input},
                            },
                            headers=srv.get("auth") or {},
                        )
                        content = _json.dumps(r.json().get("result", r.json()))
                    except Exception as exc:
                        content = _json.dumps({"error": str(exc)})

                elif tu.name.startswith("a2a__"):
                    ag_name = tu.name[5:]
                    ag = next((a for a in a2a_agents if a["name"] == ag_name), None)
                    try:
                        assert ag
                        r = await http.post(
                            ag["endpoint"],
                            json={"message": tu.input.get("message", _json.dumps(tu.input))},
                        )
                        content = _json.dumps(r.json())
                    except Exception as exc:
                        content = _json.dumps({"error": str(exc)})

                else:
                    content = _json.dumps({"error": f"Unknown tool: {tu.name}"})

                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})

        messages.append({"role": "user", "content": tool_results})

    return {
        "result": None,
        "error": "max_iterations_reached",
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
    }


# ── LangGraph ─────────────────────────────────────────────────────────────────

async def _run_langgraph_agent(config: dict, input_data: dict) -> dict:
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langgraph.prebuilt import create_react_agent
    except ImportError:
        return {"error": "langgraph, langchain-anthropic, or langchain-mcp-adapters not installed", "result": None}

    import json as _json

    mcp_servers: list[dict] = config.get("mcp_servers") or []
    mcp_config = {
        s["name"]: {"url": s["url"], "transport": s.get("transport", "http")}
        for s in mcp_servers
    }
    llm = ChatAnthropic(model=config.get("model", "claude-opus-4-7"))

    async with MultiServerMCPClient(mcp_config) as mcp:
        tools = mcp.get_tools()
        agent = create_react_agent(llm, tools)
        result = await agent.ainvoke({
            "messages": [{"role": "user", "content": _json.dumps(input_data)}]
        })
        last = result["messages"][-1]
        content = last.content if hasattr(last, "content") else str(last)
        try:
            return _json.loads(content)
        except Exception:
            return {"result": content}


# ── Google ADK ────────────────────────────────────────────────────────────────

async def _run_adk_agent(config: dict, input_data: dict) -> dict:
    try:
        from google.adk.agents import LlmAgent
        from google.adk.sessions import InMemorySessionService
        from google.adk.runners import Runner
        import google.genai.types as genai_types
    except ImportError:
        return {"error": "google-adk not installed. Run: pip install google-adk", "result": None}

    import json as _json
    import uuid

    model_id = config.get("model", "gemini-2.0-flash")
    system_prompt = config.get("system_prompt", "You are a helpful assistant.")

    agent = LlmAgent(
        name="workflow_agent",
        model=model_id,
        description=system_prompt,
        instruction=system_prompt,
    )
    session_service = InMemorySessionService()
    app_name = "nocode_workflow"
    user_id = "runner"
    session_id = str(uuid.uuid4())

    await session_service.create_session(app_name=app_name, user_id=user_id, session_id=session_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)

    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=_json.dumps(input_data))],
    )
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
