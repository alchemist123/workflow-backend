"""Standalone FastAPI workflow — new-test (auto-generated, do not edit)."""
import asyncio, json, os
from datetime import datetime
from typing import Any
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse

app = FastAPI(title='new-test', version='1.0.0')
import httpx

_SAFE_GLOBALS: dict = {
    "__builtins__": {
        "len": len, "str": str, "int": int, "float": float, "bool": bool,
        "list": list, "dict": dict, "tuple": tuple, "set": set,
        "True": True, "False": False, "None": None,
        "range": range, "enumerate": enumerate, "zip": zip,
        "all": all, "any": any, "sum": sum, "min": min, "max": max,
        "sorted": sorted, "reversed": reversed, "map": map, "filter": filter,
        "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
        "print": print, "repr": repr, "abs": abs, "round": round,
    },
    "datetime": __import__("datetime").datetime,
}



# ── MCP / A2A protocol helpers ────────────────────────────────────────────────

def _orch_mcp_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    if not url.endswith("/mcp"):
        return url + "/mcp"
    return url

async def _orch_mcp_init_session(http, url: str, extra_headers: dict):
    try:
        r = await http.post(
            url,
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "standalone-workflow", "version": "1.0"}}},
            headers={"Accept": "application/json, text/event-stream", **extra_headers},
        )
        return r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
    except Exception:
        return None

def _orch_mcp_headers(extra: dict, session_id) -> dict:
    h = {"Accept": "application/json, text/event-stream", **extra}
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h

def _orch_mcp_parse_tools(r) -> list:
    text = r.text if hasattr(r, "text") else ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            try:
                obj = json.loads(payload)
                tools = obj.get("result", {}).get("tools", [])
                if isinstance(tools, list):
                    return tools
            except Exception:
                pass
    try:
        return r.json().get("result", {}).get("tools", [])
    except Exception:
        return []

def _orch_mcp_parse_result(r):
    text = r.text if hasattr(r, "text") else ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            try:
                obj = json.loads(payload)
                result = obj.get("result")
                if result is not None:
                    return result
            except Exception:
                pass
    try:
        return r.json().get("result", r.json())
    except Exception:
        return {"error": "Could not parse MCP response"}

async def _orch_a2a_call(http, endpoint: str, message: str, auth_token: str = "") -> dict:
    import uuid as _uu
    hdrs = {"Content-Type": "application/json"}
    if auth_token:
        hdrs["Authorization"] = f"Bearer {auth_token}"
    url = endpoint.rstrip("/") + "/"
    body = {
        "jsonrpc": "2.0", "id": str(_uu.uuid4()), "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message}],
                "messageId": str(_uu.uuid4()),
            }
        },
    }
    r = await http.post(url, json=body, headers=hdrs)
    data = r.json()
    result = data.get("result", data)
    if isinstance(result, dict):
        for artifact in result.get("artifacts", []):
            for part in artifact.get("parts", []):
                if part.get("kind") == "text":
                    return {"result": part["text"]}
        for msg in reversed(result.get("history", [])):
            if msg.get("role") == "agent":
                for part in msg.get("parts", []):
                    if part.get("kind") == "text":
                        return {"result": part["text"]}
    return result


# ── Shared tool-collection helper (MCP / A2A) ─────────────────────────────────

async def _collect_tools_adk(mcp_servers, a2a_agents, functions) -> list:
    """Return list of ADK FunctionTool objects from MCP servers and A2A agents."""
    from google.adk.tools import FunctionTool
    adk_tools: list = []

    async with httpx.AsyncClient(timeout=15) as http:
        for srv in mcp_servers:
            try:
                mcp_base = _orch_mcp_url(srv["url"])
                extra_hdrs = srv.get("auth") or {}
                sid = await _orch_mcp_init_session(http, mcp_base, extra_hdrs)
                resp = await http.post(
                    mcp_base,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers=_orch_mcp_headers(extra_hdrs, sid),
                )
                for tool in _orch_mcp_parse_tools(resp):
                    def _make_mcp(srv_cfg, tn, fn, d):
                        async def mcp_fn(**kwargs) -> dict:
                            try:
                                base = _orch_mcp_url(srv_cfg["url"])
                                eh = srv_cfg.get("auth") or {}
                                async with httpx.AsyncClient(timeout=30) as h:
                                    sid2 = await _orch_mcp_init_session(h, base, eh)
                                    r = await h.post(base,
                                        json={"jsonrpc":"2.0","id":1,"method":"tools/call",
                                              "params":{"name":tn,"arguments":kwargs}},
                                        headers=_orch_mcp_headers(eh, sid2))
                                return _orch_mcp_parse_result(r)
                            except Exception as exc:
                                return {"error": str(exc)}
                        mcp_fn.__name__ = fn.replace("-","_").replace(".","_")
                        mcp_fn.__doc__ = d or f"MCP tool {fn}"
                        return FunctionTool(mcp_fn)
                    full = f"{srv['name']}__{tool['name']}"
                    adk_tools.append(_make_mcp(srv, tool["name"], full, tool.get("description","")))
            except Exception:
                pass

    for ag in a2a_agents:
        def _make_a2a_ag(agent_cfg):
            async def call_agent(message: str) -> dict:
                try:
                    async with httpx.AsyncClient(timeout=60) as h:
                        return await _orch_a2a_call(h, agent_cfg["endpoint"], message, agent_cfg.get("auth_token",""))
                except Exception as exc:
                    return {"error": str(exc)}
            call_agent.__name__ = f"a2a_{agent_cfg['name'].replace('-','_').replace(' ','_')}"
            call_agent.__doc__ = agent_cfg.get("description", f"Delegate to '{agent_cfg['name']}'")
            return FunctionTool(call_agent)
        adk_tools.append(_make_a2a_ag(ag))

    for fn in functions:
        fn_name = fn.get("name", "")
        if not fn_name:
            continue
        def _make_fn(fn_def):
            async def inline_fn(**kwargs):
                try:
                    local_ns = {**kwargs, "data": kwargs, "input_data": kwargs}
                    exec(compile(fn_def["code"], "<function>", "exec"), dict(_SAFE_GLOBALS), local_ns)
                    result = local_ns.get("result", local_ns.get("output"))
                    return {"result": result} if not isinstance(result, dict) else result
                except Exception as exc:
                    return {"error": str(exc)}
            inline_fn.__name__ = fn_def["name"]
            inline_fn.__doc__ = fn_def.get("description", f"Function {fn_def['name']}")
            return FunctionTool(inline_fn)
        adk_tools.append(_make_fn(fn))

    return adk_tools


# ── AGENT node — Google ADK ───────────────────────────────────────────────────

async def _run_agent(config: dict, state: dict) -> dict:
    """AGENT node — Google ADK (Gemini/Vertex AI) with MCP servers and A2A agents."""
    try:
        from google.adk.agents import LlmAgent
        from google.adk.sessions import InMemorySessionService
        from google.adk.runners import Runner
        import google.genai.types as genai_types
    except ImportError:
        return {"error": "google-adk not installed", "result": None}

    import uuid as _uu

    vertex_project = config.get("vertex_project") or os.environ.get("VERTEX_PROJECT", "")
    vertex_location = config.get("vertex_location") or os.environ.get("VERTEX_LOCATION", "us-central1")
    api_key = config.get("api_key") or os.environ.get("GOOGLE_API_KEY", "")

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
    adk_tools = await _collect_tools_adk(mcp_servers, a2a_agents, [])

    instruction = config.get("system_prompt", "You are a helpful assistant.")
    agent = LlmAgent(name="agent", model=config.get("model", "gemini-2.0-flash"),
                     description=instruction, instruction=instruction, tools=adk_tools)
    session_service = InMemorySessionService()
    sid = str(_uu.uuid4())
    await session_service.create_session(app_name="wf", user_id="u", session_id=sid)
    runner = Runner(agent=agent, app_name="wf", session_service=session_service)
    content = genai_types.Content(role="user", parts=[genai_types.Part(text=json.dumps(state))])
    final_text = ""
    try:
        async for event in runner.run_async(user_id="u", session_id=sid, new_message=content):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if part.text: final_text += part.text
    except Exception as exc:
        return {"error": str(exc)}
    try: return json.loads(final_text)
    except Exception: return {"result": final_text}


# ── Orchestrator dispatcher ───────────────────────────────────────────────────

async def _run_orchestrator(config: dict, state: dict) -> dict:
    return await _run_orchestrator_adk(config, state)


async def _run_orchestrator_adk(config: dict, state: dict) -> dict:
    """Orchestrator Agent — Google ADK (Gemini/Vertex AI) with MCP/A2A/inline-function tools."""
    try:
        from google.adk.agents import LlmAgent
        from google.adk.sessions import InMemorySessionService
        from google.adk.runners import Runner
        import google.genai.types as genai_types
    except ImportError:
        return {"error": "google-adk not installed — add it to requirements.txt", "result": None}

    import uuid as _uu

    vertex_project = config.get("vertex_project") or os.environ.get("VERTEX_PROJECT", "")
    vertex_location = config.get("vertex_location") or os.environ.get("VERTEX_LOCATION", "us-central1")
    sa_json = config.get("service_account_json") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    api_key = config.get("api_key") or os.environ.get("GOOGLE_API_KEY", "")

    if vertex_project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = vertex_project
        os.environ["GOOGLE_CLOUD_LOCATION"] = vertex_location
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
        os.environ.pop("GOOGLE_API_KEY", None)
        if sa_json:
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

    resolved = config.get("resolved_tools") or {}
    mcp_servers = (resolved.get("mcp_servers") or []) + (config.get("mcp_servers") or [])
    a2a_agents  = (resolved.get("a2a_agents") or []) + (config.get("a2a_agents") or [])
    functions   = (resolved.get("functions") or []) + (config.get("functions") or [])

    adk_tools = await _collect_tools_adk(mcp_servers, a2a_agents, functions)

    base_instruction = config.get("system_prompt", "You are a helpful assistant.")
    if adk_tools:
        tool_names = ", ".join(
            t.func.__name__ if hasattr(t, "func") else str(t) for t in adk_tools
        )
        base_instruction = (
            f"{base_instruction}\n\n"
            f"You MUST use the available tools for every request. "
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
    app_name, user_id = "standalone_workflow", "runner"
    session_id = str(_uu.uuid4())
    await session_service.create_session(app_name=app_name, user_id=user_id, session_id=session_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)

    content = genai_types.Content(role="user", parts=[genai_types.Part(text=json.dumps(state))])
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
            return {"error": f"Google API rate limit: {err}"}
        return {"error": err}
    try:
        return json.loads(final_text)
    except Exception:
        return {"result": final_text}


async def run_workflow(trigger_payload: dict) -> dict:
    state = trigger_payload
    _next: str | None = 'node_1778395978595_1'

    while _next:
        if _next == 'node_1778395978595_1':
            if isinstance(state.get('body'), dict):
                state = state['body']
            _next = 'node_1778397297641_3'
        elif _next == 'node_1778395983825_2':
            state = await _run_agent({}, state)
            _next = None
        elif _next == 'node_1778397297641_3':
            _orch_result = await _run_orchestrator({'model': 'gemini-2.0-flash', 'api_key': 'AIzaSyAHSuhm9c39zHNvyvApA_uJSJ0YqOoupbQ', 'framework': 'adk', 'functions': [], 'system_prompt': 'you are diet manager use tool to generate report, use remote agents for any needed', 'max_iterations': 6, 'vertex_project': 'arabbank-dev-agentic-prj', 'tool_execution_mode': 'parallel', 'model_provider': 'google', 'resolved_tools': {'mcp_servers': [{'name': 'TOOL', 'url': 'http://host.docker.internal:9002/mcp', 'transport': 'http', 'node_id': 'node_1778398206624_9', 'node_type': 'TOOL'}], 'a2a_agents': [{'name': 'diet agent', 'endpoint': 'http://host.docker.internal:8083', 'description': 'diet manager', 'auth_token': '', 'node_id': 'node_1778398268881_11', 'node_type': 'REMOTE_AGENT'}], 'functions': []}}, state)
            state = _orch_result
            _next = 'node_1778398299167_12'
        elif _next == 'node_1778398206624_9':
            async with httpx.AsyncClient(timeout=30) as _hc:
                _args = {**{}, **state}
                _mr = await _hc.post('http://host.docker.internal:9002/mcp',
                    json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                          'params':{'name':'diet test','arguments':_args}})
                state = _mr.json().get('result', _mr.json())
            _next = None
        elif _next == 'node_1778398268881_11':
            _next = None
        elif _next == 'node_1778398299167_12':
            _vp = 'arabbank-dev-agentic-prj' or os.environ.get('GOOGLE_CLOUD_PROJECT', '')
            _vl = 'us-central1' or os.environ.get('VERTEX_LOCATION', 'us-central1')
            if not _vp:
                state = {'error': 'GOOGLE_CLOUD_PROJECT not set — add it to .env', '_error': True}
            else:
                from google import genai as _genai
                from google.genai import types as _genai_types
                _vc = _genai.Client(vertexai=True, project=_vp, location=_vl)
                _m_prompt = 'summerize the content' + ('\n\n' if 'summerize the content' else '') + json.dumps(state)
                try:
                    _vpr = await _vc.aio.models.generate_content(model='gemini-2.0-flash-001', contents=_m_prompt, config=_genai_types.GenerateContentConfig(max_output_tokens=1024, temperature=0.7, system_instruction='summerize the content'))
                    _mt = _vpr.text or ''
                    try: state = {'content': _mt, 'parsed': json.loads(_mt)}
                    except: state = {'content': _mt}
                except Exception as _ve:
                    state = {'error': str(_ve), '_error': True}
            _next = 'node_1778475500133_2'
        elif _next == 'node_1778475500133_2':
            _next = None
        else:
            _next = None

    return state

@app.post("/runner")
async def workflow_trigger(body: dict = Body(default={})):
    """Trigger endpoint for workflow: new-test."""
    result = await run_workflow(body)
    return JSONResponse(content=result)


@app.get("/health")
async def _health():
    return {"status": "ok"}
