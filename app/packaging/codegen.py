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

_ANTHROPIC_HELPER = r'''
async def _run_agent(config: dict, state: dict) -> dict:
    """Inline Anthropic agentic loop with MCP tool discovery and A2A delegation."""
    try:
        import anthropic as _ant
    except ImportError:
        return {"error": "anthropic package not installed", "result": None}

    model = config.get("model", "claude-opus-4-7")
    system = config.get("system_prompt", "You are a helpful assistant.")
    mcp_servers: list = config.get("mcp_servers") or []
    a2a_agents: list = config.get("a2a_agents") or []
    max_iter: int = config.get("max_iterations", 10)

    client = _ant.Anthropic()
    tools, mcp_map = [], {}

    async with httpx.AsyncClient(timeout=15) as _h:
        for srv in mcp_servers:
            try:
                r = await _h.post(srv["url"],
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers=srv.get("auth") or {})
                for t in r.json().get("result", {}).get("tools", []):
                    full = f"{srv['name']}__{t['name']}"
                    tools.append({"name": full, "description": t.get("description", ""),
                                  "input_schema": t.get("inputSchema", {"type": "object"})})
                    mcp_map[full] = (srv, t["name"])
            except Exception:
                pass

    for ag in a2a_agents:
        tools.append({"name": f"a2a__{ag['name']}",
                      "description": ag.get("description", f"Call agent {ag['name']}"),
                      "input_schema": {"type": "object",
                                       "properties": {"message": {"type": "string"}},
                                       "required": ["message"]}})

    messages = [{"role": "user", "content": json.dumps(state)}]
    in_tok = out_tok = 0

    for _ in range(max_iter):
        kw: dict = {"model": model, "max_tokens": 4096, "system": system, "messages": messages}
        if tools:
            kw["tools"] = tools
        resp = client.messages.create(**kw)
        in_tok += resp.usage.input_tokens
        out_tok += resp.usage.output_tokens

        if resp.stop_reason == "end_turn":
            texts = [b.text for b in resp.content if hasattr(b, "text")]
            text = texts[-1] if texts else ""
            try:
                data = json.loads(text)
            except Exception:
                data = {"result": text}
            return {**data, "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}

        if resp.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": resp.content})
        trs = []
        async with httpx.AsyncClient(timeout=30) as _h:
            for tu in (b for b in resp.content if b.type == "tool_use"):
                if tu.name in mcp_map:
                    srv, rn = mcp_map[tu.name]
                    try:
                        r = await _h.post(srv["url"],
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": rn, "arguments": tu.input}},
                            headers=srv.get("auth") or {})
                        content = json.dumps(r.json().get("result", r.json()))
                    except Exception as exc:
                        content = json.dumps({"error": str(exc)})
                elif tu.name.startswith("a2a__"):
                    ag_name = tu.name[5:]
                    ag = next((a for a in a2a_agents if a["name"] == ag_name), None)
                    try:
                        assert ag
                        r = await _h.post(ag["endpoint"],
                            json={"message": tu.input.get("message", json.dumps(tu.input))})
                        content = json.dumps(r.json())
                    except Exception as exc:
                        content = json.dumps({"error": str(exc)})
                else:
                    content = json.dumps({"error": f"unknown tool {tu.name}"})
                trs.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
        messages.append({"role": "user", "content": trs})

    return {"result": None, "error": "max_iterations_reached",
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}
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
    has_agent  = any(n.get("node_type") == "AGENT"  for n in nodes.values())
    has_model  = any(n.get("node_type") == "MODEL"  for n in nodes.values())
    has_mcp    = any(n.get("node_type") in ("DATASOURCE", "TOOL") for n in nodes.values())
    needs_httpx = has_agent or has_model or has_mcp

    parts: list[str] = []

    # 1. header + imports
    parts.append(_gen_imports(workflow_name, has_jmespath, needs_httpx, has_agent or has_model))

    # 2. safe builtins
    parts.append(_SAFE_GLOBALS)

    # 3. expression constants (long strings kept out of the executor body)
    expr_consts = _gen_expr_constants(nodes)
    if expr_consts:
        parts.append(expr_consts)

    # 4. agent helper
    if has_agent or has_model:
        parts.append(_ANTHROPIC_HELPER)

    # 5. workflow executor
    parts.append(_gen_executor(ir_dict))

    # 6. HTTP trigger endpoint
    parts.append(_gen_endpoint(trigger_path, trigger_method, workflow_name))

    # 7. health check
    parts.append(_HEALTH)

    return "\n\n".join(parts)


# ── Section generators ────────────────────────────────────────────────────────

def _gen_imports(name: str, jmespath: bool, httpx_: bool, anthropic: bool) -> str:
    lines = [
        f'"""Standalone FastAPI workflow — {name} (auto-generated, do not edit)."""',
        "import asyncio, json, os",
        "from datetime import datetime",
        "from typing import Any",
        "from fastapi import FastAPI, Request",
        "from fastapi.responses import JSONResponse",
        "",
        f"app = FastAPI(title={name!r}, version='1.0.0')",
    ]
    if jmespath:
        lines.append("import jmespath")
    if httpx_:
        lines.append("import httpx")
    if anthropic:
        lines.append("import anthropic as _anthropic_pkg")
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
            L += [
                "            _mc = _anthropic_pkg.Anthropic()",
                f"            _mr = _mc.messages.create(",
                f"                model={cfg.get('model', 'claude-opus-4-7')!r},",
                "                max_tokens=4096,",
                f"                system={cfg.get('system_prompt', 'You are helpful.')!r},",
                "                messages=[{'role':'user','content':json.dumps(state)}],",
                "            )",
                "            _mt = next((b.text for b in _mr.content if hasattr(b,'text')), '')",
                "            try: state = json.loads(_mt)",
                "            except: state = {'result': _mt}",
                f"            _next = {nxt0!r}",
            ]

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
async def workflow_trigger(request: Request):
    \"\"\"Trigger endpoint for workflow: {name}.\"\"\"
    try:
        body = await request.json()
    except Exception:
        body = {{}}
    result = await run_workflow(body)
    return JSONResponse(content=result)
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _expr_var(node_id: str) -> str:
    safe = node_id.replace("-", "_").replace("/", "_").replace(".", "_")
    return f"_EXPR_{safe}"
