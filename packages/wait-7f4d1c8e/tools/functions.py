"""Inline canvas functions exposed to agent nodes as ADK tools.

A FUNCTION node wired into an agent's `tools` handle becomes a `FunctionTool`
here.  A FUNCTION node wired into the flow instead is generated as a real node
module with its body emitted as module-level Python -- which is the better path,
and the reason this module is only for the tool case.

Trust boundary: the code in these definitions comes from whoever built the
canvas, and it runs with the package's own privileges.  The container is the
boundary, not `SAFE_BUILTINS` below -- that is a guardrail against accidents
(a stray `open`, an `import os`), not a sandbox against a determined author.
Treat canvas authorship as equivalent to commit access to this package.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import re
from typing import Any

from tools.signature import apply_schema_signature, safe_tool_name

logger = logging.getLogger("workflow.tools.functions")

# A deliberately small builtin surface. Enough for data shaping; no file, process
# or import access, so an ordinary transform cannot reach the host by accident.
SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "getattr": getattr,
    "hasattr": hasattr,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "print": print,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    "True": True,
    "False": False,
    "None": None,
}

SAFE_GLOBALS: dict[str, Any] = {
    "__builtins__": SAFE_BUILTINS,
    "datetime": datetime,
    "json": json,
    "math": math,
    "re": re,
}


def run_code(code: str, data: dict[str, Any]) -> Any:
    """Execute a canvas code body and return whatever it assigned to `result`.

    The body sees `data` (and `input_data` as an alias) and is expected to set
    `result`.  Falling back to `output` keeps older canvases working.
    """
    scope: dict[str, Any] = {
        "data": data,
        "input_data": data,
        "result": None,
        "output": None,
    }
    exec(compile(code, "<canvas-function>", "exec"), dict(SAFE_GLOBALS), scope)  # noqa: S102
    result = scope.get("result")
    return result if result is not None else scope.get("output")


def build_tools(functions: list[dict[str, Any]]) -> list:
    """Wrap each inline function definition as an ADK FunctionTool."""
    from google.adk.tools import FunctionTool

    tools: list = []
    for definition in functions or []:
        name = definition.get("name")
        code = definition.get("code")
        if not name or not code:
            logger.warning("Skipping inline function with no name or body.")
            continue

        def _make(name=name, code=code, definition=definition):
            async def call_inline_function(**kwargs) -> dict:
                try:
                    result = run_code(code, dict(kwargs))
                except Exception as exc:  # noqa: BLE001 - reported to the model
                    logger.warning("Inline function %s failed: %s", name, exc)
                    return {"error": str(exc)}
                return result if isinstance(result, dict) else {"result": result}

            call_inline_function.__name__ = safe_tool_name(name)
            call_inline_function.__doc__ = definition.get(
                "description", f"Inline function {name}."
            )
            # The canvas node declares this function's parameters; without them
            # the model could not pass it anything.
            apply_schema_signature(
                call_inline_function,
                definition.get("parameters"),
                fallback_param="request",
            )
            return FunctionTool(call_inline_function)

        tools.append(_make())
    return tools


async def collect_tools(
    mcp_servers: list[dict[str, Any]] | None = None,
    a2a_agents: list[dict[str, Any]] | None = None,
    functions: list[dict[str, Any]] | None = None,
    groups: list[dict[str, Any]] | None = None,
    agents: list[dict[str, Any]] | None = None,
) -> list:
    """Every tool a consumer offers, from all five sources.

    Called once when the agent node is constructed.  A group becomes a single
    tool that runs its own children, so the model sees one entry per group
    rather than each child individually.

    `agents` is different in kind from the rest and is returned separately by
    `collect_agent_tools`: an LLM_AGENT attached to another agent is not wrapped
    as a tool here at all, because ADK exposes a `single_turn` sub-agent as a
    tool by itself. Passing them here would mean hand-wrapping what the
    framework already does.
    """
    from tools import a2a as a2a_tools
    from tools import mcp as mcp_tools

    tools: list = []
    tools.extend(await mcp_tools.discover_tools(mcp_servers or []))
    tools.extend(a2a_tools.build_tools(a2a_agents or []))
    tools.extend(build_tools(functions or []))
    for group in groups or []:
        tools.append(await build_group(group))
    if agents:
        # Reached only when a caller has nowhere to put sub-agents -- a tool
        # group, whose children must all be tools.
        from tools.agents import build_agent_tool

        for agent in agents:
            tools.append(await build_agent_tool(agent))
    return tools


async def collect_subagents(agents: list[dict[str, Any]] | None = None) -> list:
    """LLM_AGENT nodes to hand to a parent agent's `sub_agents` list.

    Kept apart from `collect_tools` because these are agents, not tools: ADK
    turns each into a `_SingleTurnAgentTool` itself, deriving the parameters the
    parent's model sees from the sub-agent's `input_schema`.
    """
    from tools.agents import build_subagent

    return [await build_subagent(agent) for agent in agents or []]


async def build_group(group: dict[str, Any]) -> Any:
    """One tool group as a single tool, building its children first.

    Recursive: a child that is itself a group becomes one nested tool.
    """
    from tools import a2a as a2a_tools
    from tools import mcp as mcp_tools
    from tools.groups import build_group_tool

    children: list = []
    for child in group.get("children") or []:
        kind = child.get("kind")
        if kind == "group":
            children.append(await build_group(child))
        elif kind == "mcp":
            # A group child names one MCP tool, so only that tool is wrapped --
            # not every tool the server happens to expose.
            children.extend(
                await mcp_tools.discover_tools([child], only=child.get("tool_name"))
            )
        elif kind == "a2a":
            children.extend(a2a_tools.build_tools([child]))
        elif kind == "function":
            children.extend(build_tools([child]))
        elif kind == "agent":
            # A group's members are tools, so the agent is driven through its
            # own Runner rather than attached as a sub-agent.
            from tools.agents import build_agent_tool

            children.append(await build_agent_tool(child))
    return build_group_tool(group, children)
