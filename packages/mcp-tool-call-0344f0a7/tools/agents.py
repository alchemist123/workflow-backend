"""An LLM_AGENT node used as a tool by something else.

A canvas LLM_AGENT can be wired into another agent's `tools` handle, or into a
Sequential/Parallel Tools group. Those two positions need two different
mechanisms, and the difference is forced by ADK rather than chosen here:

## Attached to another agent -> `sub_agents` with `mode='single_turn'`

This is ADK's own recommendation. `AgentTool`'s docstring says:

    To expose an agent as an inline tool of a parent LlmAgent, prefer setting
    mode='single_turn' on the sub-agent and attaching it via sub_agents=[...].
    The framework then exposes the sub-agent as a tool automatically and runs
    it inline in the parent's session. Direct usage of AgentTool is
    discouraged.

So `build_subagent` returns a plain `LlmAgent` for the parent's `sub_agents`
list; ADK wraps it in a `_SingleTurnAgentTool` whose declared parameters come
from the agent's `input_schema`. Running inline means it shares the parent's
session and costs no extra Runner.

## Inside a tool group -> a FunctionTool over its own Runner

A group is a deterministic composite `FunctionTool` (see `tools/groups.py`), not
an agent, so there is no `sub_agents` list to attach to and nothing to expose
the child automatically. `build_agent_tool` therefore drives the agent itself
through a private `Runner`, and presents it to the group as an ordinary tool
with a real signature — which is what lets the existing group machinery treat
an agent child exactly like an MCP tool.

That path must leave `mode` unset. ADK refuses to run a single-turn agent as a
Runner's root:

    LlmAgent as root agent must have mode='chat' or 'task', but got
    mode='single_turn'.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from tools.schema import fallback_input_model, model_from_schema
from tools.signature import apply_schema_signature, safe_tool_name

logger = logging.getLogger("workflow.tools.agents")


def _instruction(entry: dict[str, Any]) -> str:
    return (
        entry.get("instruction")
        or entry.get("description")
        or "You are a helpful assistant."
    )


def _description(entry: dict[str, Any], name: str) -> str:
    """What the calling model reads to decide whether to use this agent."""
    return entry.get("description") or f"Delegate a request to the '{name}' agent."


async def _own_tools(entry: dict[str, Any]) -> list:
    """The tools wired into this agent's own tools handle."""
    tools = entry.get("tools") or {}
    if not any(tools.get(k) for k in ("mcp_servers", "a2a_agents", "functions", "groups", "agents")):
        return []

    from tools.functions import collect_tools

    return await collect_tools(
        mcp_servers=tools.get("mcp_servers") or [],
        a2a_agents=tools.get("a2a_agents") or [],
        functions=tools.get("functions") or [],
        groups=tools.get("groups") or [],
        agents=tools.get("agents") or [],
    )


def _model_for(entry: dict[str, Any]):
    """The model id this agent should run on."""
    from core.config import settings

    return entry.get("model") or settings.LLM_MODEL or "gemini-2.5-flash"


def _generate_config(entry: dict[str, Any]):
    """Temperature / token limits, when the node set them."""
    from google.genai import types

    kwargs: dict[str, Any] = {}
    if entry.get("temperature") is not None:
        kwargs["temperature"] = entry["temperature"]
    if entry.get("max_tokens") is not None:
        kwargs["max_output_tokens"] = entry["max_tokens"]
    return types.GenerateContentConfig(**kwargs) if kwargs else None


async def _build(entry: dict[str, Any], *, single_turn: bool):
    """The shared LlmAgent construction for both call styles."""
    from google.adk.agents import LlmAgent

    name = safe_tool_name(entry.get("name") or "agent")
    tools = await _own_tools(entry)

    kwargs: dict[str, Any] = {
        "name": name,
        "model": _model_for(entry),
        "description": _description(entry, name),
        "instruction": _instruction(entry),
    }
    if tools:
        kwargs["tools"] = tools

    # `input_schema` must be a pydantic class. With no declared structure the
    # agent still needs one parameter, or a calling model cannot pass anything.
    input_model = model_from_schema(f"{name}_input", entry.get("input_schema"))
    if input_model is None and single_turn:
        input_model = fallback_input_model(f"{name}_input", _description(entry, name))
    if input_model is not None:
        kwargs["input_schema"] = input_model

    # `output_schema` takes a dict as-is. ADK supports pairing it with tools:
    # tools stay available during the thought loop and the structure is enforced
    # only on the final answer.
    if entry.get("output_schema"):
        kwargs["output_schema"] = entry["output_schema"]
    if entry.get("output_key"):
        kwargs["output_key"] = entry["output_key"]

    config = _generate_config(entry)
    if config is not None:
        kwargs["generate_content_config"] = config

    if single_turn:
        # Exposes it to the parent as a tool, run inline in the parent session.
        kwargs["mode"] = "single_turn"

    agent = LlmAgent(**kwargs)
    logger.info(
        "built sub-agent %s (%s) with %d tool(s), input_schema=%s output_schema=%s",
        name,
        "single_turn" if single_turn else "runner",
        len(tools),
        bool(input_model),
        bool(entry.get("output_schema")),
    )
    return agent


async def build_subagent(entry: dict[str, Any]):
    """An LlmAgent for a parent's `sub_agents` list.

    ADK exposes it to the parent's model as a tool by itself, so nothing here
    wraps it.
    """
    return await _build(entry, single_turn=True)


async def run_once(agent, payload: dict[str, Any]) -> Any:
    """Run an agent for one turn and return its answer.

    Used by the group path. The session is private to this call: a group member
    is a tool, and a tool should not accumulate conversation state between
    invocations.
    """
    from google.adk import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from core.config import settings

    sessions = InMemorySessionService()
    app_name = f"{settings.AGENT_NAME}_{agent.name}"
    session_id = str(uuid.uuid4())
    await sessions.create_session(
        app_name=app_name, user_id="workflow", session_id=session_id
    )
    runner = Runner(app_name=app_name, agent=agent, session_service=sessions)

    text = json.dumps(payload, default=str) if payload else ""
    message = types.Content(role="user", parts=[types.Part(text=text)])

    answer = ""
    async for event in runner.run_async(
        user_id="workflow", session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    answer += part.text

    # With an output_schema set the reply is JSON, so hand back the object
    # rather than the string a later tool would have to re-parse.
    try:
        return json.loads(answer)
    except (ValueError, TypeError):
        return answer


async def build_agent_tool(entry: dict[str, Any]):
    """One LLM_AGENT as a FunctionTool, for use inside a tool group.

    The declared signature comes from the agent's input structure, so the group
    can merge it into its own parameters exactly as it does for an MCP tool.
    """
    from google.adk.tools import FunctionTool

    agent = await _build(entry, single_turn=False)
    name = safe_tool_name(entry.get("name") or "agent")
    description = _description(entry, name)

    async def call_agent(**kwargs) -> dict:
        try:
            result = await run_once(agent, dict(kwargs))
        except Exception as exc:  # noqa: BLE001 - reported to the model as data
            logger.warning("sub-agent %s failed: %s", name, exc)
            return {"error": f"{type(exc).__name__}: {exc}"}
        return result if isinstance(result, dict) else {"result": result}

    call_agent.__name__ = name
    call_agent.__doc__ = description
    apply_schema_signature(
        call_agent, entry.get("input_schema"), fallback_param="request"
    )
    return FunctionTool(call_agent)
