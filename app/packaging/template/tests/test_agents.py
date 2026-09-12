"""LLM_AGENT used as a sub-agent, and the schema conversion behind it.

Part of the template's own suite: `tools/agents.py` and `tools/schema.py` are
copied verbatim into every package, so proving them once here proves them
everywhere.

Nothing here calls a model. The two things worth testing are what ADK is handed
and what a *calling* model would therefore see, and both are observable from the
constructed agent and its tool declaration.
"""

from __future__ import annotations

import asyncio

import pytest
from google.adk.agents import LlmAgent

from tools.agents import build_agent_tool, build_subagent
from tools.schema import (
    FALLBACK_FIELD,
    fallback_input_model,
    model_from_schema,
    safe_model_name,
)

WRITER = {
    "kind": "agent",
    "name": "writer",
    "description": "Writes a headline from notes.",
    "instruction": "Write a headline.",
    "model": "gemini-2.5-flash",
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {"type": "string", "description": "Raw notes"},
            "tone": {"type": "string", "description": "Tone of voice"},
        },
        "required": ["notes"],
    },
    "output_schema": {
        "type": "object",
        "properties": {"headline": {"type": "string"}},
        "required": ["headline"],
    },
    "output_key": "headline_out",
    "tools": {},
}


def _declaration(tool) -> dict:
    return tool._get_declaration().model_dump(exclude_none=True)


def _params(tool) -> dict:
    """The declared parameters a calling model sees.

    ADK puts these in `parameters_json_schema`, not `parameters`; reading the
    wrong key makes a correctly-declared tool look like it takes no arguments.
    """
    dumped = _declaration(tool)
    return dumped.get("parameters_json_schema") or dumped.get("parameters") or {}


def _exposed_by(parent: LlmAgent):
    """The tool ADK generates for a parent's first sub-agent."""
    return asyncio.run(parent.canonical_tools())[0]


# ── tools/schema.py ──────────────────────────────────────────────────────────


def test_model_from_schema_keeps_the_required_split():
    model = model_from_schema("writer_input", WRITER["input_schema"])
    schema = model.model_json_schema()

    assert set(schema["properties"]) == {"notes", "tone"}
    assert schema["required"] == ["notes"]
    assert schema["properties"]["notes"]["description"] == "Raw notes"


def test_model_from_schema_returns_none_for_an_empty_schema():
    """None, not an empty model.

    An empty schema would tell a calling model this agent takes no arguments,
    which is worse than leaving ADK's `input_schema` unset.
    """
    assert model_from_schema("x", None) is None
    assert model_from_schema("x", {}) is None
    assert model_from_schema("x", {"type": "object", "properties": {}}) is None


def test_model_from_schema_skips_unusable_field_names():
    model = model_from_schema(
        "x",
        {
            "type": "object",
            "properties": {"ok": {"type": "string"}, "not a name": {"type": "string"}},
            "required": ["ok"],
        },
    )
    assert set(model.model_json_schema()["properties"]) == {"ok"}


def test_model_from_schema_maps_json_types():
    model = model_from_schema(
        "x",
        {
            "type": "object",
            "properties": {
                "s": {"type": "string"},
                "i": {"type": "integer"},
                "f": {"type": "number"},
                "b": {"type": "boolean"},
                "o": {"type": "object"},
                "a": {"type": "array"},
            },
            "required": ["s", "i", "f", "b", "o", "a"],
        },
    )
    properties = model.model_json_schema()["properties"]
    assert properties["s"]["type"] == "string"
    assert properties["i"]["type"] == "integer"
    assert properties["f"]["type"] == "number"
    assert properties["b"]["type"] == "boolean"
    assert properties["o"]["type"] == "object"
    assert properties["a"]["type"] == "array"


def test_safe_model_name_always_yields_an_identifier():
    for raw in ("my writer", "2fast", "", "class", "a-b-c"):
        assert safe_model_name(raw).isidentifier(), raw


def test_fallback_model_has_one_required_text_field():
    schema = fallback_input_model("x", "do the thing").model_json_schema()
    assert schema["required"] == [FALLBACK_FIELD]
    assert schema["properties"][FALLBACK_FIELD]["type"] == "string"


# ── build_subagent: the sub_agents / single_turn path ────────────────────────


def test_subagent_is_single_turn():
    """`mode='single_turn'` is what makes ADK expose it to the parent as a tool.

    ADK's own docstring: "prefer setting mode='single_turn' on the sub-agent and
    attaching it via sub_agents=[...] ... Direct usage of AgentTool is
    discouraged."
    """
    agent = asyncio.run(build_subagent(WRITER))
    assert agent.mode == "single_turn"
    assert agent.name == "writer"


def test_subagent_carries_the_declared_structures():
    agent = asyncio.run(build_subagent(WRITER))
    assert agent.input_schema is not None
    assert set(agent.input_schema.model_json_schema()["properties"]) == {"notes", "tone"}
    # output_schema takes a plain dict, so it is passed through unchanged.
    assert agent.output_schema == WRITER["output_schema"]
    assert agent.output_key == "headline_out"


def test_the_parent_sees_the_input_structure_as_the_tool_parameters():
    """The whole point of declaring an input structure.

    Without it the parent's model can only send one blob of text.
    """
    agent = asyncio.run(build_subagent(WRITER))
    parent = LlmAgent(
        name="parent", model="gemini-2.5-flash", instruction="x", sub_agents=[agent]
    )
    tool = _exposed_by(parent)

    assert tool.name == "writer"
    params = _params(tool)
    assert set(params["properties"]) == {"notes", "tone"}
    assert params["required"] == ["notes"]
    assert _declaration(tool)["description"] == "Writes a headline from notes."


def test_a_subagent_without_a_structure_still_takes_one_argument():
    """A tool declaring no parameters can never be passed anything."""
    agent = asyncio.run(build_subagent({**WRITER, "input_schema": None}))
    parent = LlmAgent(
        name="parent", model="gemini-2.5-flash", instruction="x", sub_agents=[agent]
    )
    params = _params(_exposed_by(parent))

    assert params["required"] == [FALLBACK_FIELD]


def test_output_schema_and_tools_can_coexist():
    """ADK 2.8 supports both together, so the platform imposes no rule against it.

    From `LlmAgent.output_schema`: "The ADK supports using `output_schema` and
    `tools` together. It works by exposing tools during the thought loop and
    enforcing structure only on the final output."
    """
    entry = {
        **WRITER,
        "tools": {
            "functions": [
                {
                    "name": "helper",
                    "description": "A helper.",
                    "code": "result = {'ok': True}",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                }
            ]
        },
    }
    agent = asyncio.run(build_subagent(entry))
    assert agent.output_schema == WRITER["output_schema"]
    assert len(agent.tools) == 1


def test_a_subagent_keeps_its_own_tools():
    entry = {
        **WRITER,
        "tools": {
            "functions": [
                {
                    "name": "helper",
                    "description": "A helper.",
                    "code": "result = {'ok': True}",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        },
    }
    agent = asyncio.run(build_subagent(entry))
    assert [getattr(t, "name", "") for t in agent.tools] == ["helper"]


def test_temperature_and_max_tokens_reach_the_generate_config():
    agent = asyncio.run(build_subagent({**WRITER, "temperature": 0.2, "max_tokens": 64}))
    assert agent.generate_content_config.temperature == pytest.approx(0.2)
    assert agent.generate_content_config.max_output_tokens == 64


# ── build_agent_tool: the tool-group path ────────────────────────────────────


def test_agent_tool_is_not_single_turn():
    """A group drives its child through a Runner, and ADK refuses a single-turn root.

    "LlmAgent as root agent must have mode='chat' or 'task', but got
    mode='single_turn'."
    """
    tool = asyncio.run(build_agent_tool(WRITER))
    # The tool closes over the agent; assert via a fresh build of the same path.
    from tools.agents import _build

    agent = asyncio.run(_build(WRITER, single_turn=False))
    assert agent.mode is None
    assert tool.name == "writer"


def test_agent_tool_declares_the_input_structure():
    """So a group can merge it into its own parameters like any other tool."""
    tool = asyncio.run(build_agent_tool(WRITER))
    params = _params(tool)

    assert set(params["properties"]) == {"notes", "tone"}
    assert params["required"] == ["notes"]


def test_a_single_turn_agent_cannot_be_a_runner_root():
    """The constraint that forces two mechanisms rather than one.

    Pinned as a test because if ADK ever lifts it, the group path could use the
    same construction as the sub_agents path.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    agent = asyncio.run(build_subagent(WRITER))
    service = InMemorySessionService()
    runner = Runner(app_name="p", agent=agent, session_service=service)

    async def run():
        session = await service.create_session(app_name="p", user_id="u")
        async for _ in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
        ):
            pass

    with pytest.raises(ValueError, match="single_turn"):
        asyncio.run(run())


def test_agent_tool_reports_a_failure_as_data():
    """A group member must not raise: a failure is reported to the model."""
    tool = asyncio.run(build_agent_tool(WRITER))
    # No credentials configured, so the underlying run fails.
    result = asyncio.run(tool.func(notes="some notes"))
    assert isinstance(result, dict)
    assert "error" in result or "result" in result
