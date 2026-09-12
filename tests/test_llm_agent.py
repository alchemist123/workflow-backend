"""LLM_AGENT on the canvas: the renamed MODEL, now usable as a sub-agent.

An LLM_AGENT is both a tool consumer and a tool provider. It runs in the flow
like any node, and it can be wired into another agent's `tools` handle or into a
Sequential/Parallel Tools group, where ADK exposes it as a callable tool.

The runtime side — how the agent is constructed, and what declaration a calling
model ends up seeing — lives in the package template and is covered by
`app/packaging/template/tests/test_agents.py`. This file covers the canvas side:
the rename, declaration, validation, resolution, env handling and rendering.
"""

from __future__ import annotations

import json
import warnings

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.compiler.canvas_migrations import migrate_canvas
from app.compiler.semantic_validator import validate_semantics
from app.nodes.agent_io import fields_to_json_schema, structure_errors
from app.nodes.registry import (
    AGENT_TYPES,
    NODE_REGISTRY,
    SUBAGENT_TYPES,
    TOOL_CONSUMER_TYPES,
    TOOL_PROVIDER_TYPES,
)
from app.packaging.render import render_package
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)


def _node(node_id: str, node_type: str, config: dict | None = None, **meta) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": meta.get("title", ""), "description": ""},
        "config": config if config is not None else {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1}, "on_error": "fail"},
    }


def _edge(source, target, handle="output", target_handle="input") -> dict:
    return {
        "id": f"e_{source}_{target}_{target_handle}_{handle}",
        "source": source,
        "source_handle": handle,
        "target": target,
        "target_handle": target_handle,
        "condition": None,
    }


def _tools_edge(source, target) -> dict:
    return _edge(source, target, "output", "tools")


def _canvas(nodes, edges) -> CanvasPayload:
    return CanvasPayload.model_validate({"nodes": nodes, "edges": edges})


_START = {
    "payload_schema": {
        "fields": [{"name": "q", "type": "string", "description": "", "required": True}]
    }
}

_WRITER = {
    "name": "writer",
    "description": "Writes a headline from notes.",
    "system_prompt": "Write a headline.",
    "model": "gemini-2.5-flash",
    "input_structure": {
        "fields": [
            {"name": "notes", "type": "text", "description": "Raw notes", "required": True},
            {"name": "tone", "type": "string", "description": "Tone", "required": False},
        ]
    },
    "output_structure": {
        "fields": [{"name": "headline", "type": "string", "description": "", "required": True}]
    },
    "output_key": "headline_out",
}


def _plan(canvas: CanvasPayload, version_id: str):
    errors, warnings_out, ir = run_compiler(canvas, version_id)
    assert errors == [], errors
    return build_graph_plan(ir, workflow_name=version_id), warnings_out


def _subagent_canvas():
    """A2A_START -> ORCHESTRATOR_AGENT -> END, with an LLM_AGENT as a sub-agent."""
    return _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}, title="Brain"),
            _node("writer", "LLM_AGENT", dict(_WRITER), title="Writer"),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("writer", "orch"),
            _edge("orch", "end"),
        ],
    )


# ── The rename ───────────────────────────────────────────────────────────────


def test_model_node_type_is_gone():
    assert "MODEL" not in NODE_REGISTRY
    assert "LLM_AGENT" in NODE_REGISTRY


def test_a_saved_model_node_migrates_to_llm_agent():
    """A pure rename: every MODEL config key is still a valid LLM_AGENT key."""
    canvas = {
        "schema_version": 3,
        "nodes": [
            _node("m", "MODEL", {"model": "gemini-2.0-flash", "system_prompt": "hi"})
        ],
        "edges": [],
    }
    migrated, notes = migrate_canvas(canvas)

    assert migrated["nodes"][0]["type"] == "LLM_AGENT"
    assert migrated["nodes"][0]["config"] == {
        "model": "gemini-2.0-flash",
        "system_prompt": "hi",
    }
    assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
    assert any("MODEL -> LLM_AGENT" in n for n in notes)


def test_response_format_json_becomes_a_note_not_a_guess():
    """`response_format: json` asked for JSON without saying what shape.

    `output_schema` supersedes it, but nothing in the old value says what the
    structure should be, so the migration explains rather than inventing one.
    """
    canvas = {
        "schema_version": 3,
        "nodes": [_node("m", "MODEL", {"response_format": "json"})],
        "edges": [],
    }
    migrated, notes = migrate_canvas(canvas)

    assert "response_format" not in migrated["nodes"][0]["config"]
    assert "output structure" in migrated["nodes"][0]["metadata"]["description"]
    assert any("response_format" in n for n in notes)


def test_migration_is_idempotent():
    canvas = {
        "schema_version": 3,
        "nodes": [_node("m", "MODEL", {"response_format": "json"})],
        "edges": [],
    }
    once, _ = migrate_canvas(canvas)
    twice, notes = migrate_canvas(once)
    assert twice == once
    assert notes == []


# ── Declaration ──────────────────────────────────────────────────────────────


def test_llm_agent_is_both_a_consumer_and_a_provider():
    """That duality is the whole feature: it takes tools and can *be* one."""
    assert "LLM_AGENT" in TOOL_CONSUMER_TYPES
    assert "LLM_AGENT" in TOOL_PROVIDER_TYPES


def test_llm_agent_is_the_only_sub_agent_type():
    """AGENT and ORCHESTRATOR_AGENT stay flow-level.

    LLM_AGENT is the composable unit; making every agent type wirable as a tool
    would mean an ORCHESTRATOR_AGENT could be a member of a tool group, which is
    not a shape the canvas should invite.
    """
    assert SUBAGENT_TYPES == {"LLM_AGENT"}
    assert AGENT_TYPES == {"AGENT", "LLM_AGENT", "ORCHESTRATOR_AGENT"}


def test_only_llm_agent_offers_an_input_structure():
    """ADK consults `input_schema` only when an agent is called as a tool.

    On a type that can never be called as a tool the setting would be a control
    that does nothing, so it is not offered at all.
    """
    assert "input_structure" in NODE_REGISTRY["LLM_AGENT"].config_schema["properties"]
    for node_type in ("AGENT", "ORCHESTRATOR_AGENT"):
        properties = NODE_REGISTRY[node_type].config_schema["properties"]
        assert "input_structure" not in properties, node_type
        # ...but the output structure applies wherever an agent runs.
        assert "output_structure" in properties, node_type


def test_response_format_is_no_longer_a_config_key():
    assert "response_format" not in NODE_REGISTRY["LLM_AGENT"].config_schema["properties"]


# ── Structure conversion ─────────────────────────────────────────────────────


def test_fields_convert_to_json_schema_with_the_required_split():
    schema = fields_to_json_schema(_WRITER["input_structure"])
    assert schema == {
        "type": "object",
        "properties": {
            "notes": {"type": "string", "description": "Raw notes"},
            "tone": {"type": "string", "description": "Tone"},
        },
        "required": ["notes"],
    }


def test_an_empty_structure_converts_to_none_not_an_empty_schema():
    """An empty schema would constrain the model to reply with `{}`."""
    assert fields_to_json_schema(None) is None
    assert fields_to_json_schema({}) is None
    assert fields_to_json_schema({"fields": []}) is None


def test_structure_errors_catch_what_json_schema_cannot():
    errors = structure_errors(
        {"fields": [{"name": "a"}, {"name": "a"}, {"name": "2bad"}, {"name": ""}]},
        "input structure",
    )
    joined = " ".join(errors)
    assert "more than once" in joined
    assert "2bad" in joined
    assert "has no name" in joined


# ── Resolution ───────────────────────────────────────────────────────────────


def test_a_sub_agent_lands_in_the_agents_bucket_not_the_flow():
    """It is attached via `sub_agents`, so it must not become a graph node."""
    canvas = _subagent_canvas()
    _errors, _warnings, ir = run_compiler(canvas, "sub-0001")

    resolved = ir.nodes["orch"].config["resolved_tools"]
    assert [a["name"] for a in resolved["agents"]] == ["writer"]
    assert resolved["mcp_servers"] == []

    plan, _ = _plan(canvas, "sub-0001")
    assert "LLM_AGENT" not in {n.node_type for n in plan.nodes}


def test_a_sub_agent_carries_its_converted_schemas():
    _errors, _warnings, ir = run_compiler(_subagent_canvas(), "sub-0002")
    entry = ir.nodes["orch"].config["resolved_tools"]["agents"][0]

    assert entry["kind"] == "agent"
    assert entry["input_schema"]["required"] == ["notes"]
    assert entry["output_schema"]["required"] == ["headline"]
    assert entry["output_key"] == "headline_out"
    assert entry["instruction"] == "Write a headline."


def test_a_sub_agent_keeps_its_own_tools():
    """A sub-agent is a consumer in its own right, so its tools must nest."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node("writer", "LLM_AGENT", dict(_WRITER), title="Writer"),
            _node("look", "TOOL", {"mcp_url": "http://mcp:9000", "tool_name": "lookup"}, title="Look"),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("writer", "orch"),
            _tools_edge("look", "writer"),
            _edge("orch", "end"),
        ],
    )
    _errors, _warnings, ir = run_compiler(canvas, "sub-0003")
    entry = ir.nodes["orch"].config["resolved_tools"]["agents"][0]

    assert [t["tool_name"] for t in entry["tools"]["mcp_servers"]] == ["lookup"]


def test_a_sub_agent_can_be_a_member_of_a_group():
    """The wiring the request asked for: group -> agent -> tools."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node("seq", "SEQUENTIAL_AGENT", {"name": "pipe", "order": ["writer", "rem"]}),
            _node("writer", "LLM_AGENT", dict(_WRITER), title="Writer"),
            _node("rem", "REMOTE_AGENT", {"name": "calc", "endpoint": "http://a", "description": "d"}),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("writer", "seq"),
            _tools_edge("rem", "seq"),
            _tools_edge("seq", "orch"),
            _edge("orch", "end"),
        ],
    )
    _errors, _warnings, ir = run_compiler(canvas, "sub-0004")
    group = ir.nodes["orch"].config["resolved_tools"]["groups"][0]

    assert [(c["kind"], c["name"]) for c in group["children"]] == [
        ("agent", "writer"),
        ("a2a", "calc"),
    ]


def test_a_sub_agents_nested_tools_get_their_own_env_vars():
    """Credentials inside a sub-agent must be env-ified like any other."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node("writer", "LLM_AGENT", dict(_WRITER), title="Writer"),
            _node("look", "TOOL", {"mcp_url": "http://mcp:9000", "tool_name": "lookup"}, title="Look"),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("writer", "orch"),
            _tools_edge("look", "writer"),
            _edge("orch", "end"),
        ],
    )
    plan, _ = _plan(canvas, "sub-0005")
    keys = {k.key for k in plan.env_keys}
    assert "MCP_LOOK_URL" in keys

    orch = next(n for n in plan.nodes if n.node_type == "ORCHESTRATOR_AGENT")
    nested = orch.config["resolved_tools"]["agents"][0]["tools"]["mcp_servers"][0]
    assert nested["url_env"] == "MCP_LOOK_URL"
    assert "url" not in nested, "the literal URL must not survive into the plan"


# ── Validation ───────────────────────────────────────────────────────────────


def test_a_sub_agent_without_a_description_warns():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node("writer", "LLM_AGENT", {"system_prompt": "x"}),
            _node("end", "END", {}),
        ],
        [_edge("start", "orch"), _tools_edge("writer", "orch"), _edge("orch", "end")],
    )
    errors, warnings_out = validate_semantics(canvas)
    assert errors == []
    assert any("no description" in w for w in warnings_out)
    assert any("no input structure" in w for w in warnings_out)


def test_an_input_structure_on_a_flow_only_agent_warns():
    """It would be silently ignored, which is worse than being told."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("writer", "LLM_AGENT", dict(_WRITER)),
            _node("end", "END", {}),
        ],
        [_edge("start", "writer"), _edge("writer", "end")],
    )
    errors, warnings_out = validate_semantics(canvas)
    assert errors == []
    assert any("only applies when the agent is called as a tool" in w for w in warnings_out)


def test_bad_structure_field_names_are_errors():
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node(
                "writer",
                "LLM_AGENT",
                {
                    "description": "d",
                    "input_structure": {"fields": [{"name": "a"}, {"name": "a"}]},
                },
            ),
            _node("end", "END", {}),
        ],
        [_edge("start", "orch"), _tools_edge("writer", "orch"), _edge("orch", "end")],
    )
    errors, _warnings = validate_semantics(canvas)
    assert any("more than once" in e for e in errors)


def test_an_agent_wired_into_itself_is_one_error_not_two():
    """Tool edges are excluded from the flow graph, so only the tool rule fires.

    Before that split the same loop was reported twice, once as a flow cycle,
    which pointed at the wrong thing.
    """
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node("writer", "LLM_AGENT", dict(_WRITER)),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("writer", "orch"),
            _tools_edge("writer", "writer"),
            _edge("orch", "end"),
        ],
    )
    errors, _warnings = validate_semantics(canvas)
    assert len(errors) == 1, errors
    assert "wired in a loop" in errors[0]


def test_a_flow_positioned_llm_agent_is_still_checked_for_connectivity():
    """It is a provider type, but here it sits in the flow.

    Exempting providers by type would have let this through silently.
    """
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node("writer", "LLM_AGENT", {"description": "d"}),
            _node("end", "END", {}),
        ],
        [_edge("start", "orch"), _edge("orch", "writer"), _edge("orch", "end")],
    )
    errors, _warnings = validate_semantics(canvas)
    assert any("no outbound edge" in e for e in errors)


# ── Rendering ────────────────────────────────────────────────────────────────


def test_the_consumer_module_attaches_sub_agents(tmp_path):
    plan, _ = _plan(_subagent_canvas(), "sub-0006")
    render_package(plan, tmp_path / "pkg")

    orch = next(n for n in plan.nodes if n.node_type == "ORCHESTRATOR_AGENT")
    source = (tmp_path / "pkg" / f"nodes/{orch.name}.py").read_text()

    assert "SUB_AGENTS" in source
    assert "collect_subagents" in source
    # Attached as sub-agents, not hand-wrapped: ADK exposes them itself.
    assert "sub_agents" in source
    assert "'name': 'writer'" in source or '"name": "writer"' in source


def test_an_output_structure_alone_forces_an_agent_loop(tmp_path):
    """`output_schema` is an LlmAgent feature, so a bare generate_content call
    cannot honour it."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node(
                "writer",
                "LLM_AGENT",
                {
                    "model": "gemini-2.5-flash",
                    "output_structure": {
                        "fields": [{"name": "headline", "type": "string", "required": True}]
                    },
                },
            ),
            _node("end", "END", {}),
        ],
        [_edge("start", "writer"), _edge("writer", "end")],
    )
    plan, _ = _plan(canvas, "sub-0007")
    render_package(plan, tmp_path / "pkg")

    writer = next(n for n in plan.nodes if n.node_type == "LLM_AGENT")
    source = (tmp_path / "pkg" / f"nodes/{writer.name}.py").read_text()

    assert "LlmAgent" in source
    assert "OUTPUT_SCHEMA" in source
    assert "output_schema" in source


def test_a_bare_llm_agent_is_still_one_direct_call(tmp_path):
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("writer", "LLM_AGENT", {"model": "gemini-2.5-flash"}),
            _node("end", "END", {}),
        ],
        [_edge("start", "writer"), _edge("writer", "end")],
    )
    plan, _ = _plan(canvas, "sub-0008")
    render_package(plan, tmp_path / "pkg")

    writer = next(n for n in plan.nodes if n.node_type == "LLM_AGENT")
    source = (tmp_path / "pkg" / f"nodes/{writer.name}.py").read_text()

    assert "generate_content" in source
    assert "LlmAgent" not in source


def test_a_nested_sub_agent_tool_pulls_in_the_mcp_dependency(tmp_path):
    """Requirements must see a tool that only exists inside a sub-agent."""
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node("writer", "LLM_AGENT", dict(_WRITER), title="Writer"),
            _node("look", "TOOL", {"mcp_url": "http://mcp:9000", "tool_name": "lookup"}, title="Look"),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("writer", "orch"),
            _tools_edge("look", "writer"),
            _edge("orch", "end"),
        ],
    )
    plan, _ = _plan(canvas, "sub-0009")
    render_package(plan, tmp_path / "pkg")

    requirements = (tmp_path / "pkg" / "requirements.txt").read_text()
    assert "mcp" in requirements


def test_no_sub_agent_credential_reaches_the_package(tmp_path):
    canvas = _canvas(
        [
            _node("start", "A2A_START", _START),
            _node("orch", "ORCHESTRATOR_AGENT", {"model": "gemini-2.5-flash"}),
            _node(
                "writer",
                "LLM_AGENT",
                {**_WRITER, "api_key": "CANARY-AGENT-KEY"},
                title="Writer",
            ),
            _node(
                "look",
                "TOOL",
                {
                    "mcp_url": "http://mcp:9000",
                    "tool_name": "lookup",
                    "auth_token": "CANARY-MCP-TOKEN",
                },
                title="Look",
            ),
            _node("end", "END", {}),
        ],
        [
            _edge("start", "orch"),
            _tools_edge("writer", "orch"),
            _tools_edge("look", "writer"),
            _edge("orch", "end"),
        ],
    )
    plan, _ = _plan(canvas, "sub-0010")
    destination = tmp_path / "pkg"
    render_package(plan, destination)

    leaked = []
    for path in destination.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.name == ".env":
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for canary in ("CANARY-AGENT-KEY", "CANARY-MCP-TOKEN"):
            if canary in text:
                leaked.append(f"{path.relative_to(destination)}: {canary}")

    assert leaked == [], leaked
    assert "CANARY-MCP-TOKEN" in (destination / ".env").read_text()
    assert "CANARY" not in json.dumps(plan.to_dict())
