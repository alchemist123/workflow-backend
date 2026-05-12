"""
Generate a fully self-contained standalone FastAPI app from a compiled workflow IR.

The output main.py has ZERO dependency on the main backend (app.*).  All node logic
is inlined as Python code so the container can run without any shared volume or
database connection.
"""
from __future__ import annotations

import json as _json


# ── Reusable code snippets ────────────────────────────────────────────────────

_SAFE_GLOBALS = """\
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
"""

# ADK helpers are included via _ORCHESTRATOR_HELPERS when any agentic node is present
# (consolidated — no separate Anthropic helper needed)

# ADK helpers for all agentic nodes — inlined in standalone main.py, no app.* imports
_ORCHESTRATOR_HELPERS = r'''
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
'''

_HEALTH = """\
@app.get("/health")
async def _health():
    return {"status": "ok"}
"""


# ── Public entry point ────────────────────────────────────────────────────────

def generate_standalone_app(ir_dict: dict, workflow_name: str = "workflow") -> str:
    """Return the text of a complete standalone FastAPI main.py."""
    nodes: dict[str, dict] = ir_dict["nodes"]
    entrypoints: list[str] = ir_dict.get("entrypoints", [])
    trigger_id = entrypoints[0] if entrypoints else next(iter(nodes))

    trigger_cfg = nodes.get(trigger_id, {}).get("config", {})
    trigger_path = trigger_cfg.get("path", "/run")
    if not trigger_path.startswith("/"):
        trigger_path = "/" + trigger_path
    trigger_method = trigger_cfg.get("method", "POST").lower()

    has_jmespath = any(
        n.get("node_type") == "TRANSFORM" and n.get("config", {}).get("mode") == "jmespath"
        for n in nodes.values()
    )
    has_agent        = any(n.get("node_type") == "AGENT"              for n in nodes.values())
    has_model        = any(n.get("node_type") == "MODEL"              for n in nodes.values())
    has_mcp          = any(n.get("node_type") in ("DATASOURCE", "TOOL") for n in nodes.values())
    has_orchestrator = any(n.get("node_type") == "ORCHESTRATOR_AGENT" for n in nodes.values())
    needs_httpx = has_agent or has_model or has_mcp or has_orchestrator

    parts: list[str] = []

    # 1. header + imports
    parts.append(_gen_imports(workflow_name, has_jmespath, needs_httpx))

    # 2. safe builtins
    parts.append(_SAFE_GLOBALS)

    # 3. expression constants (long strings kept out of the executor body)
    expr_consts = _gen_expr_constants(nodes)
    if expr_consts:
        parts.append(expr_consts)

    # 4. ADK helpers — for AGENT and/or ORCHESTRATOR_AGENT nodes
    if has_agent or has_orchestrator:
        parts.append(_ORCHESTRATOR_HELPERS)

    # 5. workflow executor
    parts.append(_gen_executor(ir_dict))

    # 6. HTTP trigger endpoint
    parts.append(_gen_endpoint(trigger_path, trigger_method, workflow_name))

    # 7. health check
    parts.append(_HEALTH)

    return "\n\n".join(parts)


# ── Section generators ────────────────────────────────────────────────────────

def _gen_imports(name: str, jmespath: bool, httpx_: bool, _unused: bool = False) -> str:
    lines = [
        f'"""Standalone FastAPI workflow — {name} (auto-generated, do not edit)."""',
        "import asyncio, json, os",
        "from datetime import datetime",
        "from typing import Any",
        "from fastapi import FastAPI, Body",
        "from fastapi.responses import JSONResponse",
        "",
        f"app = FastAPI(title={name!r}, version='1.0.0')",
    ]
    if jmespath:
        lines.append("import jmespath")
    if httpx_:
        lines.append("import httpx")
    return "\n".join(lines)


def _gen_expr_constants(nodes: dict[str, dict]) -> str:
    lines: list[str] = []
    for nid, node in nodes.items():
        if node.get("node_type") != "TRANSFORM":
            continue
        expr = node.get("config", {}).get("expression", "")
        if not expr:
            continue
        var = _expr_var(nid)
        lines.append(f"{var} = {expr!r}")
    return "\n".join(lines)


def _gen_executor(ir_dict: dict) -> str:
    nodes: dict[str, dict] = ir_dict["nodes"]
    entrypoints = ir_dict.get("entrypoints", [])
    trigger_id = entrypoints[0] if entrypoints else next(iter(nodes))

    L: list[str] = [
        "async def run_workflow(trigger_payload: dict) -> dict:",
        "    state = trigger_payload",
        f"    _next: str | None = {trigger_id!r}",
        "",
        "    while _next:",
    ]

    first = True
    for nid, node in nodes.items():
        kw = "if" if first else "elif"
        first = False
        nt = node.get("node_type", "")
        cfg = node.get("config", {})
        nxt = node.get("next", [])
        branches = node.get("branches", {})
        var = _expr_var(nid)
        nxt0 = nxt[0] if nxt else None

        L.append(f"        {kw} _next == {nid!r}:")

        # ── Trigger nodes ──────────────────────────────────────
        if nt in ("HTTP_TRIGGER", "SCHEDULE_TRIGGER", "WEBHOOK_TRIGGER", "QUEUE_TRIGGER"):
            L += [
                "            if isinstance(state.get('body'), dict):",
                "                state = state['body']",
                f"            _next = {nxt0!r}",
            ]

        # ── Transform ──────────────────────────────────────────
        elif nt == "TRANSFORM":
            mode = cfg.get("mode", "jmespath")
            out_key = cfg.get("output_key")
            if mode == "jmespath":
                L.append(f"            _r = jmespath.search({var}, state)")
                if out_key:
                    L += [
                        "            if _r is not None:",
                        f"                state = {{**state, {out_key!r}: _r}}",
                    ]
                else:
                    L += [
                        "            if isinstance(_r, dict): state = _r",
                        "            elif _r is not None: state = {'result': _r}",
                    ]
            elif mode == "python":
                L += [
                    f"            _locs = {{'data': state, 'result': None}}",
                    f"            exec({var}, _SAFE_GLOBALS, _locs)",
                    "            _res = _locs.get('result')",
                    "            if isinstance(_res, dict): state = _res",
                    "            elif _res is not None: state = {'result': _res}",
                ]
            elif mode == "jinja2":
                L += [
                    "            from jinja2.sandbox import SandboxedEnvironment as _SBE",
                    f"            _rendered = _SBE().from_string({var}).render(**state)",
                    "            try: state = json.loads(_rendered)",
                    "            except: state = {'result': _rendered}",
                ]
            L.append(f"            _next = {nxt0!r}")

        # ── Condition ──────────────────────────────────────────
        elif nt == "CONDITION":
            branch_list: list[dict] = cfg.get("branches", [])
            L += [
                "            _branch = None",
                "            _cd = {'__builtins__': {}, 'data': state,"
                " 'True': True, 'False': False, 'None': None}",
            ]
            for b in branch_list:
                bexpr = b["expression"]
                bname = b["name"]
                L += [
                    "            if _branch is None:",
                    f"                try:",
                    f"                    if bool(eval({bexpr!r}, _cd)): _branch = {bname!r}",
                    "                except: pass",
                ]
            for bname, btargets in branches.items():
                bnxt = btargets[0] if btargets else None
                L.append(f"            if _branch == {bname!r}: _next = {bnxt!r}")
            L.append("            if _branch is None: _next = None")

        # ── Agent ──────────────────────────────────────────────
        elif nt == "AGENT":
            L += [
                f"            state = await _run_agent({cfg!r}, state)",
                f"            _next = {nxt0!r}",
            ]

        # ── Model ──────────────────────────────────────────────
        elif nt == "MODEL":
            provider = cfg.get("provider", "google")
            model = cfg.get("model", "gemini-2.0-flash")
            max_tokens = cfg.get("max_tokens", 1024)
            temperature = cfg.get("temperature", 0.7)
            sys_prompt = cfg.get("system_prompt", "")
            api_key_val = cfg.get("api_key", "")

            if provider == "vertex_ai":
                vp = cfg.get("vertex_project", "")
                vl = cfg.get("vertex_location", "us-central1")
                sa = cfg.get("service_account_json", "")
                si = f", system_instruction={sys_prompt!r}" if sys_prompt else ""
                L += [
                    f"            _vp = {vp!r} or os.environ.get('GOOGLE_CLOUD_PROJECT', '')",
                    f"            _vl = {vl!r} or os.environ.get('VERTEX_LOCATION', 'us-central1')",
                    # SA JSON auth — from node config or GOOGLE_SERVICE_ACCOUNT_JSON env var
                    f"            _sa = {sa!r} or os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')",
                    "            if _sa and 'GOOGLE_APPLICATION_CREDENTIALS' not in os.environ:",
                    "                import tempfile as _tf",
                    "                _sa_f = _tf.NamedTemporaryFile(mode='w', suffix='.json', delete=False)",
                    "                _sa_f.write(_sa); _sa_f.flush()",
                    "                os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = _sa_f.name",
                    "            if not _vp:",
                    "                state = {'error': 'GOOGLE_CLOUD_PROJECT not set — add it to .env', '_error': True}",
                    "            else:",
                    "                from google import genai as _genai",
                    "                from google.genai import types as _genai_types",
                    "                _vc = _genai.Client(vertexai=True, project=_vp, location=_vl)",
                    f"                _m_prompt = {sys_prompt!r} + ('\\n\\n' if {sys_prompt!r} else '') + json.dumps(state)",
                    "                try:",
                    f"                    _vpr = await _vc.aio.models.generate_content(model={model!r}, contents=_m_prompt, config=_genai_types.GenerateContentConfig(max_output_tokens={max_tokens}, temperature={temperature}{si}))",
                    "                    _mt = _vpr.text or ''",
                    "                    try: state = {'content': _mt, 'parsed': json.loads(_mt)}",
                    "                    except: state = {'content': _mt}",
                    "                except Exception as _ve:",
                    "                    state = {'error': str(_ve), '_error': True}",
                    f"            _next = {nxt0!r}",
                ]
            else:
                # Google AI Studio REST API
                L += [
                    f"            _m_key = {api_key_val!r} or os.environ.get('GOOGLE_API_KEY', '')",
                    "            if not _m_key:",
                    "                state = {'error': 'GOOGLE_API_KEY not set — add it to .env', '_error': True}",
                    "            else:",
                    f"                _m_url = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'",
                    f"                _m_prompt = {sys_prompt!r} + ('\\n\\n' if {sys_prompt!r} else '') + json.dumps(state)",
                    "                _m_body = {'contents': [{'role': 'user', 'parts': [{'text': _m_prompt}]}],"
                    f" 'generationConfig': {{'maxOutputTokens': {max_tokens}, 'temperature': {temperature}}}}}",
                    "                async with httpx.AsyncClient(timeout=60) as _mh:",
                    "                    _mr = await _mh.post(_m_url, params={'key': _m_key}, json=_m_body)",
                    "                _md = _mr.json()",
                    "                if 'error' in _md:",
                    "                    state = {'error': _md['error'].get('message', str(_md['error'])), '_error': True}",
                    "                else:",
                    "                    _mt = ''.join(p.get('text','') for p in _md.get('candidates',[{}])[0].get('content',{}).get('parts',[]))",
                    "                    try: state = {'content': _mt, 'parsed': json.loads(_mt)}",
                    "                    except: state = {'content': _mt}",
                    f"            _next = {nxt0!r}",
                ]

        # ── Orchestrator Agent ─────────────────────────────────
        elif nt == "ORCHESTRATOR_AGENT":
            output_field = cfg.get("output_field")
            L += [
                f"            _orch_result = await _run_orchestrator({cfg!r}, state)",
            ]
            if output_field:
                L += [
                    f"            if isinstance(_orch_result, dict) and {output_field!r} in _orch_result:",
                    f"                state = {{**_orch_result, {output_field!r}: _orch_result[{output_field!r}]}}",
                    "            else:",
                    "                state = _orch_result",
                ]
            else:
                L.append("            state = _orch_result")
            L.append(f"            _next = {nxt0!r}")

        # ── Datasource / Tool (MCP) ────────────────────────────
        elif nt in ("DATASOURCE", "TOOL"):
            mcp_url  = cfg.get("mcp_url", "")
            tool_name = cfg.get("tool_name", "")
            static_args = cfg.get("tool_args") or {}
            L += [
                f"            async with httpx.AsyncClient(timeout=30) as _hc:",
                f"                _args = {{**{static_args!r}, **state}}",
                f"                _mr = await _hc.post({mcp_url!r},",
                f"                    json={{'jsonrpc':'2.0','id':1,'method':'tools/call',",
                f"                          'params':{{'name':{tool_name!r},'arguments':_args}}}})",
                "                state = _mr.json().get('result', _mr.json())",
                f"            _next = {nxt0!r}",
            ]

        # ── End ────────────────────────────────────────────────
        elif nt == "END":
            out_map: dict = cfg.get("output_mapping", {})
            if out_map:
                L.append(f"            state = {{v: state.get(k) for k, v in {out_map!r}.items()}}")
            L.append("            _next = None")

        # ── Loop (single pass for codegen simplicity) ──────────
        elif nt == "LOOP":
            L.append(f"            _next = {nxt0!r}")

        # ── Parallel fork / merge — pass-through ──────────────
        elif nt in ("PARALLEL_FORK", "MERGE", "SUBWORKFLOW", "HUMAN_APPROVAL"):
            L.append(f"            _next = {nxt0!r}")

        else:
            L.append(f"            _next = {nxt0!r}")

    L += [
        "        else:",
        "            _next = None",
        "",
        "    return state",
    ]

    return "\n".join(L)


def _gen_endpoint(path: str, method: str, name: str) -> str:
    return f"""\
@app.{method}("{path}")
async def workflow_trigger(body: dict = Body(default={{}})):
    \"\"\"Trigger endpoint for workflow: {name}.\"\"\"
    result = await run_workflow(body)
    return JSONResponse(content=result)
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _expr_var(node_id: str) -> str:
    safe = node_id.replace("-", "_").replace("/", "_").replace(".", "_")
    return f"_EXPR_{safe}"
