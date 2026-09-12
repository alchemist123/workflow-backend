"""ORCHESTRATOR_AGENT — Orchestrator.

Generated from canvas node 'orchestrator'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

import json

from google.adk import Event
from google.adk.agents.context import Context
from google.adk.workflow import node

from core.config import settings
from core.logging import node_logger
from core.state import as_text, merge_state
from nodes.base import NODE_KWARGS, node_error

NODE_ID = "n_orchestrator_agent_2"
ON_ERROR = "fail"

SYSTEM_PROMPT = "Answer the user's request. You can call a trip planner and a polish pipeline. Pass them proper arguments \u2014 they declare what they need."
MODEL_FALLBACK = "gemini-2.5-flash"

# Where the agent's answer is written into the payload.
OUTPUT_KEY = "result"

# Tools wired to this agent on the canvas. Each entry names the environment
# variable holding its URL or token rather than the value: a credential in this
# file would be committed to git. The canvas values are written into `.env`
# (gitignored) as those variables' defaults.
MCP_SERVERS: list[dict] = []
A2A_AGENTS: list[dict] = []
INLINE_FUNCTIONS: list[dict] = []

# Tool groups. Each becomes ONE tool the model can call, which runs its own
# children in order (sequential) or all at once (parallel). Drawn on the canvas
# as a SEQUENTIAL_AGENT or PARALLEL_AGENT node.
TOOL_GROUPS: list[dict] = [{'kind': 'group', 'name': 'polish_text', 'mode': 'sequential', 'description': 'Check grammar, then rewrite the text in a house style.', 'stop_on_error': True, 'max_concurrency': 0, 'children': [{'kind': 'mcp', 'name': 'Grammar MCP', 'transport': 'http', 'tool_name': 'check_grammar', 'url_env': 'MCP_GRAMMAR_MCP_URL', 'auth_token_env': 'MCP_GRAMMAR_MCP_TOKEN'}, {'kind': 'agent', 'name': 'editor', 'description': 'Rewrites text in a consistent house style.', 'instruction': 'Rewrite the text clearly and concisely.', 'provider': 'vertex_ai', 'model': 'gemini-2.5-flash', 'temperature': None, 'max_tokens': None, 'input_schema': {'type': 'object', 'properties': {'text': {'type': 'string', 'description': 'The text to rewrite'}, 'style': {'type': 'string', 'description': 'House style to apply'}}, 'required': ['text']}, 'output_schema': {'type': 'object', 'properties': {'rewritten': {'type': 'string', 'description': 'The rewritten text'}}, 'required': ['rewritten']}, 'output_key': '', 'tools': {'mcp_servers': [], 'a2a_agents': [], 'functions': [], 'groups': [], 'agents': []}}]}]

# LLM_AGENT nodes wired into this agent's tools handle. They are attached via
# `sub_agents`, not wrapped as tools: ADK exposes a `single_turn` sub-agent to
# its parent as a tool by itself, taking the parameters the parent's model sees
# from the sub-agent's declared input structure. (ADK's own docstring calls
# direct use of `AgentTool` discouraged.)
SUB_AGENTS: list[dict] = [{'kind': 'agent', 'name': 'trip_planner', 'description': 'Plans a short trip for a city and a number of days.', 'instruction': 'Plan a trip. Use the weather tool before recommending outdoor activities.', 'provider': 'vertex_ai', 'model': 'gemini-2.5-flash', 'temperature': None, 'max_tokens': None, 'input_schema': {'type': 'object', 'properties': {'city': {'type': 'string', 'description': 'Destination city'}, 'days': {'type': 'integer', 'description': 'Trip length in days'}, 'interests': {'type': 'string', 'description': 'What the traveller enjoys'}}, 'required': ['city', 'days']}, 'output_schema': {'type': 'object', 'properties': {'itinerary': {'type': 'string', 'description': 'Day-by-day plan'}, 'packing_list': {'type': 'array', 'description': 'What to bring'}}, 'required': ['itinerary']}, 'output_key': 'trip', 'tools': {'mcp_servers': [{'kind': 'mcp', 'name': 'Weather MCP', 'transport': 'http', 'tool_name': 'get_forecast', 'url_env': 'MCP_WEATHER_MCP_URL', 'auth_token_env': 'MCP_WEATHER_MCP_TOKEN'}], 'a2a_agents': [], 'functions': [], 'groups': [], 'agents': []}}]
OUTPUT_SCHEMA: dict = {}

log = node_logger(NODE_ID)

# Built once on first use: tool discovery talks to every MCP server, which is
# too slow to repeat per invocation.
_agent = None


# Which fields each kind of tool entry reads from the environment.
_ENV_FIELDS = {
    "mcp": ("url", "auth_token"),
    "a2a": ("endpoint", "auth_token"),
}


def _resolve_env(entries: list[dict]) -> list[dict]:
    """Swap each entry's env-key references for the configured values.

    Recursive, because a group's children are tool entries too.
    """
    resolved: list[dict] = []
    for entry in entries:
        copy = dict(entry)
        kind = copy.get("kind")
        if kind == "group":
            copy["children"] = _resolve_env(copy.get("children") or [])
            resolved.append(copy)
            continue
        if kind == "agent":
            # A sub-agent holds no credential itself, but its own tools do.
            nested = copy.get("tools") or {}
            copy["tools"] = {
                bucket: _resolve_env(nested.get(bucket) or [])
                for bucket in (
                    "mcp_servers",
                    "a2a_agents",
                    "functions",
                    "groups",
                    "agents",
                )
            }
            resolved.append(copy)
            continue
        for field in _ENV_FIELDS.get(kind, ()):
            env_key = entry.get(f"{field}_env")
            if env_key:
                copy[field] = settings.env_value(env_key)
        resolved.append(copy)
    return resolved


async def _build_agent():
    """The LlmAgent for this node, with its tools attached."""
    from google.adk.agents import LlmAgent

    from tools.functions import collect_subagents, collect_tools

    tools = await collect_tools(
        mcp_servers=_resolve_env(MCP_SERVERS),
        a2a_agents=_resolve_env(A2A_AGENTS),
        functions=INLINE_FUNCTIONS,
        groups=_resolve_env(TOOL_GROUPS),
    )
    sub_agents = await collect_subagents(_resolve_env(SUB_AGENTS))

    instruction = SYSTEM_PROMPT or "You are a helpful assistant."
    callable_names = [
        getattr(getattr(t, "func", None), "__name__", None) or getattr(t, "name", "")
        for t in tools
    ] + [a.name for a in sub_agents]
    callable_names = [n for n in callable_names if n]
    if callable_names:
        instruction = (
            f"{instruction}\n\n"
            f"You can call: {', '.join(callable_names)}. "
            "Use them to answer the request rather than relying on your own "
            "knowledge, and call one whenever it is relevant."
        )
    log.info(
        "agent built with %d tool(s) and %d sub-agent(s)", len(tools), len(sub_agents)
    )

    kwargs: dict = {
        "name": f"{NODE_ID}_llm",
        "model": settings.LLM_MODEL or MODEL_FALLBACK,
        "description": SYSTEM_PROMPT,
        "instruction": instruction,
    }
    if tools:
        kwargs["tools"] = tools
    if sub_agents:
        kwargs["sub_agents"] = sub_agents
    if OUTPUT_SCHEMA:
        kwargs["output_schema"] = OUTPUT_SCHEMA
    return LlmAgent(**kwargs)


async def _run(payload: dict) -> str:
    """Run the agent over the payload and return its final text."""
    import uuid

    from google.adk import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    global _agent
    if _agent is None:
        _agent = await _build_agent()

    sessions = InMemorySessionService()
    session_id = str(uuid.uuid4())
    app_name = f"{settings.AGENT_NAME}_{NODE_ID}"
    await sessions.create_session(app_name=app_name, user_id="workflow", session_id=session_id)
    runner = Runner(app_name=app_name, agent=_agent, session_service=sessions)

    message = types.Content(role="user", parts=[types.Part(text=as_text(payload))])
    answer = ""
    async for event in runner.run_async(
        user_id="workflow", session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    answer += part.text
    return answer


@node(name=NODE_ID, **NODE_KWARGS(timeout=120))
async def n_orchestrator_agent_2(ctx: Context, node_input=None):
    data = node_input or {}
    try:
        answer = await _run(data)
    except Exception as exc:  # noqa: BLE001
        if ON_ERROR != "continue":
            raise
        return node_error(NODE_ID, exc)

    # An agent asked for JSON usually returns JSON; keep both forms so a
    # downstream node can use whichever it needs.
    out: dict = {OUTPUT_KEY: answer}
    try:
        parsed = json.loads(answer)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        out["parsed"] = parsed

    merge_state(ctx, out)
    return Event(output={**data, **out})
