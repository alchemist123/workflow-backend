"""Render a generated package from the graph plan and the project template.

Replaces the previous `codegen.py`, which built one flat `main.py` by
concatenating Python source strings.  Here every file is either copied verbatim
from `template/` or rendered from a Jinja2 template in `render_templates/`, so:

  * node behaviour lives in real modules that can be imported, tested and read
    in a diff, one file per canvas node
  * the static half is a runnable project with its own test suite, so a bug in
    it is caught by CI rather than by a customer's container
  * everything rendered is driven by `graph.json` and nothing else

The renderer validates what it writes: every canvas code body is compiled before
it is emitted, and the whole tree is byte-compiled afterwards, so a package that
would fail on import fails the build instead.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import py_compile
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.compiler.graph_plan import GraphPlan, PlannedNode

PACKAGING_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGING_DIR / "template"
RENDER_TEMPLATE_DIR = PACKAGING_DIR / "render_templates"

# Copied verbatim into every package. These are the files the template's own
# test suite exercises, so they arrive already tested.
STATIC_FILES: tuple[str, ...] = (
    "core/__init__.py",
    "core/config.py",
    "core/a2a_app.py",
    "core/state.py",
    "core/variables.py",
    "core/logging.py",
    "core/mapping.py",
    "core/progress.py",
    "nodes/__init__.py",
    "nodes/base.py",
    "nodes/merge.py",
    "tools/__init__.py",
    "tools/signature.py",
    "tools/schema.py",
    "tools/mcp.py",
    "tools/a2a.py",
    "tools/functions.py",
    "tools/groups.py",
    "tools/agents.py",
    "tests/__init__.py",
    "tests/conftest.py",
    "Dockerfile",
    "docker-compose.yml",
    "pytest.ini",
    "ruff.toml",
    "run_once.py",
    "requirements-dev.txt",
    ".gitignore",
    ".dockerignore",
)

# canvas node type -> the Jinja2 template that generates its module.
NODE_TEMPLATES: dict[str, str] = {
    "A2A_START": "nodes/a2a_start.py.j2",
    "TRANSFORM": "nodes/transform.py.j2",
    "CONDITION": "nodes/condition.py.j2",
    "FUNCTION": "nodes/function.py.j2",
    "END": "nodes/end.py.j2",
    "LOOP": "nodes/loop.py.j2",
    "WAIT": "nodes/wait.py.j2",
    "PARALLEL_FORK": "nodes/parallel_fork.py.j2",
    "TOOL": "nodes/mcp_tool.py.j2",
    "MCP_TOOL": "nodes/mcp_call.py.j2",
    "DATASOURCE": "nodes/mcp_tool.py.j2",
    "REMOTE_AGENT": "nodes/remote_agent.py.j2",
    "ORCHESTRATOR_AGENT": "nodes/agent_node.py.j2",
    "AGENT": "nodes/agent_node.py.j2",
    "LLM_AGENT": "nodes/llm_agent.py.j2",
    # One template for both: the fragile parts (rerun_on_resume, the
    # deterministic interrupt id, reading ctx.resume_inputs) are shared, so
    # they cannot drift apart.
    "HUMAN_APPROVAL": "nodes/human_pause.py.j2",
    "HUMAN_INPUT": "nodes/human_pause.py.j2",
    # MERGE has no module: the registry instantiates a JoinNode for it.
}

_PASSTHROUGH_TEMPLATE = "nodes/passthrough.py.j2"


class RenderError(Exception):
    """A package could not be rendered. The message names the canvas node."""


@dataclass
class RenderedPackage:
    files: list[str]
    warnings: list[str]


# ── Jinja2 environment ───────────────────────────────────────────────────────


def _pyliteral(value: Any) -> str:
    """Render a value as a Python literal.

    `tojson` is right for strings and scalars but produces `true` / `null` for
    booleans and None, which are not Python. Anything embedded as a dict or list
    literal goes through here instead.
    """
    return repr(value)


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(RENDER_TEMPLATE_DIR),
        undefined=StrictUndefined,  # a missing variable is a bug, not a blank
        keep_trailing_newline=True,
        # A block tag alone on a line vanishes entirely, newline included --
        # the conventional setup for generating code rather than prose.
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pyliteral"] = _pyliteral
    env.filters["tojson"] = json.dumps
    return env


# ── Canvas code bodies ───────────────────────────────────────────────────────


def _mapping_fields(fields: list | None) -> list[dict]:
    """A canvas field mapping, cleaned for embedding.

    Shared by a TRANSFORM's output fields and an MCP_TOOL's arguments: they are
    the same shape, read by the same `core/mapping.py` at run time.
    """
    cleaned: list[dict] = []
    for field in fields or []:
        name = (field.get("name") or "").strip()
        if not name:
            continue
        entry: dict = {
            "name": name,
            "source": (field.get("source") or "").strip(),
            "type": field.get("type") or "string",
        }
        if field.get("required"):
            entry["required"] = True
        if "default" in field and field["default"] is not None:
            entry["default"] = field["default"]
        cleaned.append(entry)
    return cleaned


def _output_fields(config: dict, canvas_id: str) -> list[dict]:
    """The declared output shape. A transform that builds nothing is an error."""
    cleaned = _mapping_fields(config.get("output_fields"))
    if not cleaned:
        raise RenderError(
            f"Canvas node '{canvas_id}' (TRANSFORM) builds no fields. Add at "
            "least one output field, or switch to an expression mode."
        )
    return cleaned


def _indent_body(code: str, canvas_id: str, *, spaces: int = 4) -> str:
    """Validate a canvas code body and indent it for embedding in a function.

    Compiling here means a syntax error fails packaging with the canvas node
    named, rather than producing a package that raises on import.
    """
    source = (code or "").strip() or "result = data"
    try:
        compile(source, f"<canvas node {canvas_id}>", "exec")
    except SyntaxError as exc:
        raise RenderError(
            f"Canvas node '{canvas_id}' has invalid Python: {exc.msg} "
            f"(line {exc.lineno}). Fix the code on the canvas and save again."
        ) from exc

    pad = " " * spaces
    return "\n".join(f"{pad}{line}" if line.strip() else "" for line in source.splitlines())


def _validate_expression(expression: str, canvas_id: str, label: str) -> str:
    """Check a single-expression canvas field parses before embedding it."""
    source = (expression or "").strip()
    if not source:
        raise RenderError(
            f"Canvas node '{canvas_id}' has an empty {label}. Fill it in and save again."
        )
    try:
        ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise RenderError(
            f"Canvas node '{canvas_id}' has an invalid {label}: {exc.msg}. "
            "Fix the expression on the canvas and save again."
        ) from exc
    return source


# ── Per-node template context ────────────────────────────────────────────────


def _node_kwargs(node: PlannedNode, timeout: int | None = None) -> str:
    """The literal keyword arguments for this node's `@node(...)` decorator.

    `timeout` overrides the canvas policy. WAIT needs that: ADK kills a node
    that outlives its timeout, and the canvas default (120s in the seeds) is
    shorter than many useful waits.
    """
    parts = []
    effective = timeout if timeout is not None else node.timeout
    if effective:
        parts.append(f"timeout={int(effective)}")
    if node.retries and node.retries > 1:
        parts.append(f"retries={int(node.retries)}")
    return ", ".join(parts)


def _tool_entries(config: dict, bucket: str) -> list[dict]:
    """One bucket of a consumer's resolved tools, ready to embed.

    Canvas ids are dropped: the generated module has no use for them, and a
    group's children are cleaned recursively.
    """
    resolved = (config or {}).get("resolved_tools") or {}
    return [_clean_tool_entry(raw) for raw in resolved.get(bucket) or []]


def _tool_kinds(resolved: dict) -> set[str]:
    """Every tool kind in a resolved tool tree, including inside groups."""
    kinds: set[str] = set()

    def walk(entries: list, default: str) -> None:
        for entry in entries or []:
            kind = entry.get("kind") or default
            kinds.add(kind)
            if kind == "group":
                walk(entry.get("children") or [], "")
            elif kind == "agent":
                nested = entry.get("tools") or {}
                walk(nested.get("mcp_servers") or [], "mcp")
                walk(nested.get("a2a_agents") or [], "a2a")
                walk(nested.get("functions") or [], "function")
                walk(nested.get("groups") or [], "group")
                walk(nested.get("agents") or [], "agent")

    walk(resolved.get("mcp_servers") or [], "mcp")
    walk(resolved.get("a2a_agents") or [], "a2a")
    walk(resolved.get("functions") or [], "function")
    walk(resolved.get("groups") or [], "group")
    walk(resolved.get("agents") or [], "agent")
    return kinds


def _clean_tool_entry(raw: dict) -> dict:
    entry = {k: v for k, v in raw.items() if k not in ("node_id", "node_type")}
    if entry.get("kind") == "group":
        entry["children"] = [
            _clean_tool_entry(child) for child in entry.get("children") or []
        ]
    elif entry.get("kind") == "agent":
        nested = entry.get("tools") or {}
        entry["tools"] = {
            bucket: [_clean_tool_entry(child) for child in nested.get(bucket) or []]
            for bucket in ("mcp_servers", "a2a_agents", "functions", "groups", "agents")
        }
    return entry


def _io_structure(config: dict) -> dict[str, Any]:
    """An agent node's declared input/output structure, as JSON Schema.

    `input_schema` is only consulted by ADK when the agent is called as a tool,
    so a flow node carries it for the case where it is also wired into another
    agent. `output_schema` applies wherever the agent runs.
    """
    from app.nodes.agent_io import fields_to_json_schema

    return {
        "input_schema": fields_to_json_schema(config.get("input_structure")),
        "output_schema": fields_to_json_schema(config.get("output_structure")),
        "structure_output_key": config.get("output_key") or "",
    }


def _node_context(node: PlannedNode, plan: GraphPlan) -> dict[str, Any]:
    """Everything a node template needs, keyed by template variable."""
    config = node.config or {}
    env = config.get("_env") or {}
    base: dict[str, Any] = {
        "node": node,
        "node_kwargs": _node_kwargs(node),
        # Every node may name its result; `flow_node` saves it after the node
        # returns, so all of a node's return paths are covered at once.
        "output_variable": (config.get("output_variable") or "").strip(),
    }

    if node.node_type == "A2A_START":
        from app.nodes.triggers import example_payload, payload_json_schema

        return {
            **base,
            "payload_schema": payload_json_schema(config),
            "example_payload": example_payload(config),
        }

    if node.node_type == "TRANSFORM":
        mode = config.get("mode") or "fields"
        context = {
            **base,
            "mode": mode,
            "output_key": config.get("output_key") or "",
            "expression": config.get("expression") or "",
            "python_body": "",
        }
        if mode == "fields":
            context["output_fields"] = _output_fields(config, node.canvas_id)
        elif mode == "python":
            context["python_body"] = _indent_body(config.get("expression", ""), node.canvas_id)
        elif mode == "jmespath" and not context["expression"]:
            raise RenderError(
                f"Canvas node '{node.canvas_id}' (TRANSFORM) has no JMESPath expression."
            )
        return context

    if node.node_type == "CONDITION":
        branches = []
        for branch in config.get("branches") or []:
            branches.append(
                {
                    "name": branch["name"],
                    "expression": _validate_expression(
                        branch.get("expression", ""), node.canvas_id, "branch expression"
                    ),
                }
            )
        return {
            **base,
            "branches": branches,
            "branch_names": [b["name"] for b in branches],
            "default_route": node.default_route,
        }

    if node.node_type == "FUNCTION":
        return {
            **base,
            "python_body": _indent_body(config.get("code", ""), node.canvas_id),
        }

    if node.node_type == "END":
        return {**base, "output_mapping": config.get("output_mapping") or {}}

    if node.node_type in ("HUMAN_APPROVAL", "HUMAN_INPUT"):
        shared = {
            **base,
            "prompt": config.get("prompt") or "",
            "assignees": list(config.get("assignees") or []),
        }
        if node.node_type == "HUMAN_APPROVAL":
            from app.nodes.tasks.human_approval import (
                required_extra_fields,
                response_json_schema,
            )

            return {
                **shared,
                "is_decision": True,
                "interrupt_prefix": "approval",
                "waiting_for": "a human decision",
                "default_prompt": "Approve this step?",
                "response_schema": response_json_schema(config),
                "required_fields": required_extra_fields(config),
                "approved_route": "approved",
                "rejected_route": "rejected",
            }

        from app.nodes.tasks.human_input import request_json_schema, required_fields

        return {
            **shared,
            "is_decision": False,
            "interrupt_prefix": "input",
            "waiting_for": "input from a human",
            "default_prompt": "Please provide the following.",
            "response_schema": request_json_schema(config),
            "required_fields": required_fields(config),
        }

    if node.node_type == "WAIT":
        from app.nodes.tasks.wait import node_timeout_for, wait_seconds

        seconds = wait_seconds(config)
        return {
            **base,
            "wait_seconds": seconds,
            # Not the canvas policy: a node whose job is to take a long time
            # must not be killed for taking it.
            "node_kwargs": _node_kwargs(node, timeout=node_timeout_for(config)),
        }

    if node.node_type == "LOOP":
        mode = config.get("mode") or "for_each"
        context = {
            **base,
            "mode": mode,
            "max_iterations": int(config.get("max_iterations") or 100),
            "items_path": config.get("items_path") or "",
            "exit_condition": "True",
            "body_route": "loop_body",
            "done_route": "done",
        }
        if mode == "for_each":
            if not context["items_path"].strip():
                raise RenderError(
                    f"Canvas node '{node.canvas_id}' (LOOP) is in for_each mode "
                    "with no items path, so it would never iterate. Set the path "
                    "to the list and save again."
                )
        else:
            context["exit_condition"] = _validate_expression(
                config.get("exit_condition", ""), node.canvas_id, "exit condition"
            )
        return context

    if node.node_type == "PARALLEL_FORK":
        return {**base, "branch_names": list(config.get("branches") or [])}

    if node.node_type in ("TOOL", "DATASOURCE"):
        tool_name = config.get("tool_name") or ""
        if not tool_name:
            raise RenderError(
                f"Canvas node '{node.canvas_id}' ({node.node_type}) has no tool name."
            )
        # The compiler moved the URL and token to environment variables and
        # stripped them from the config, so only the names are available here.
        if "mcp_url" not in env:
            raise RenderError(
                f"Canvas node '{node.canvas_id}' has no MCP URL environment "
                "variable; the graph plan is stale, so re-save the workflow."
            )
        return {
            **base,
            "tool_name": tool_name,
            "static_args": config.get("tool_args") or {},
            "url_env_key": env["mcp_url"],
            "token_env_key": env.get("auth_token", "A2A_AUTH_TOKEN"),
        }

    if node.node_type == "MCP_TOOL":
        tool_name = config.get("tool_name") or ""
        if not tool_name:
            raise RenderError(
                f"Canvas node '{node.canvas_id}' (MCP_TOOL) has no tool name. "
                "Fetch the server's tools and pick one."
            )
        if "mcp_url" not in env:
            raise RenderError(
                f"Canvas node '{node.canvas_id}' has no MCP URL environment "
                "variable; the graph plan is stale, so re-save the workflow."
            )
        arg_mode = config.get("arg_mode") or "fields"
        return {
            **base,
            "tool_name": tool_name,
            "arg_mode": arg_mode,
            "arg_fields": _mapping_fields(config.get("arg_fields")),
            "result_key": (config.get("result_key") or "").strip(),
            "url_env_key": env["mcp_url"],
            "token_env_key": env.get("auth_token", "A2A_AUTH_TOKEN"),
        }

    if node.node_type == "REMOTE_AGENT":
        if "endpoint" not in env:
            raise RenderError(
                f"Canvas node '{node.canvas_id}' has no endpoint environment "
                "variable; the graph plan is stale, so re-save the workflow."
            )
        return {
            **base,
            "agent_label": config.get("name") or node.title or node.canvas_id,
            "endpoint_env_key": env["endpoint"],
            "token_env_key": env.get("auth_token", "A2A_AUTH_TOKEN"),
            "message_key": config.get("message_key") or "message",
            "output_key": config.get("output_key") or "result",
        }

    if node.node_type in ("ORCHESTRATOR_AGENT", "AGENT"):
        return {
            **base,
            "system_prompt": config.get("system_prompt") or "",
            "model": config.get("model") or "gemini-2.5-flash",
            "output_key": config.get("output_field") or "result",
            "mcp_servers": _tool_entries(config, "mcp_servers"),
            "a2a_agents": _tool_entries(config, "a2a_agents"),
            "functions": _tool_entries(config, "functions"),
            "groups": _tool_entries(config, "groups"),
            "agents": _tool_entries(config, "agents"),
            **_io_structure(config),
        }

    if node.node_type == "LLM_AGENT":
        buckets = {
            bucket: _tool_entries(config, bucket)
            for bucket in ("mcp_servers", "a2a_agents", "functions", "groups", "agents")
        }
        io = _io_structure(config)
        return {
            **base,
            "system_prompt": config.get("system_prompt") or "",
            "model": config.get("model") or "gemini-2.5-flash",
            "max_tokens": int(config.get("max_tokens") or 1024),
            "temperature": float(config.get("temperature") or 0.7),
            "prompt_template": config.get("prompt_template") or "",
            # With tools, sub-agents or an output structure the node needs a
            # real LlmAgent; a bare prompt is cheaper as one generate_content
            # call and stays fully deterministic.
            "has_tools": any(buckets.values()) or bool(io["output_schema"]),
            **buckets,
            **io,
        }

    return base


# ── External dependencies ────────────────────────────────────────────────────

# Node types whose behaviour depends on a service configured in `.env`. A
# workflow containing one cannot be expected to reach `completed` in an offline
# test run, so its generated tests assert task terminality instead.
_NETWORK_NODE_TYPES = {
    "REMOTE_AGENT": "remote A2A agents",
    "TOOL": "MCP tool servers",
    "MCP_TOOL": "MCP tool servers",
    "DATASOURCE": "MCP data sources",
    "ORCHESTRATOR_AGENT": "a language model",
    "AGENT": "a language model",
    "LLM_AGENT": "a language model",
}


def _network_dependencies(plan: GraphPlan) -> tuple[bool, str]:
    """Whether this workflow calls out, and a phrase naming what it calls."""
    reasons: list[str] = []
    for node_type, reason in _NETWORK_NODE_TYPES.items():
        if any(n.node_type == node_type for n in plan.nodes) and reason not in reasons:
            reasons.append(reason)
    return bool(reasons), " and ".join(reasons)


# ── Requirements ─────────────────────────────────────────────────────────────


def _requirements_context(plan: GraphPlan) -> dict[str, Any]:
    types = {n.node_type for n in plan.nodes}
    transform_modes = {
        (n.config or {}).get("mode") or "jmespath"
        for n in plan.nodes
        if n.node_type == "TRANSFORM"
    }
    has_agent = bool(types & {"ORCHESTRATOR_AGENT", "AGENT", "LLM_AGENT"})

    # A tool can be nested inside a group, so count what the resolved tool tree
    # actually contains rather than only the top-level buckets.
    tool_kinds: set[str] = set()
    for node in plan.nodes:
        tool_kinds |= _tool_kinds((node.config or {}).get("resolved_tools") or {})

    has_mcp = bool(types & {"TOOL", "DATASOURCE", "MCP_TOOL"}) or "mcp" in tool_kinds
    has_remote = "REMOTE_AGENT" in types or "a2a" in tool_kinds

    jinja_reasons = []
    if "jinja2" in transform_modes:
        jinja_reasons.append("TRANSFORM nodes in jinja2 mode")
    if any(
        n.node_type == "LLM_AGENT" and (n.config or {}).get("prompt_template")
        for n in plan.nodes
    ):
        jinja_reasons.append("LLM_AGENT prompt templates")

    return {
        "needs_httpx": has_remote,
        "httpx_reason": "REMOTE_AGENT nodes and A2A agent tools",
        "needs_mcp": has_mcp,
        "mcp_reason": "MCP_TOOL / TOOL / DATASOURCE nodes and MCP agent tools",
        "needs_jmespath": "jmespath" in transform_modes,
        "needs_jinja2": bool(jinja_reasons),
        "jinja_reason": " and ".join(jinja_reasons) or "template rendering",
        "needs_sqlalchemy": True,
        "has_agent": has_agent,
    }


# ── Environment file grouping ────────────────────────────────────────────────

_ENV_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Serving", ("PORT",)),
    ("Agent identity", ("AGENT_NAME", "AGENT_DESCRIPTION", "AGENT_VERSION", "CLOUD_RUN_URL")),
    ("Inbound auth", ("A2A_AUTH_TOKEN",)),
    ("Task persistence", ("TASK_STORE_DSN",)),
    (
        "Google / Vertex AI",
        (
            "GOOGLE_CLOUD_PROJECT",
            "VERTEX_AI_LOCATION",
            "LLM_MODEL",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "GOOGLE_API_KEY",
        ),
    ),
)


def _grouped_env(plan: GraphPlan) -> list[tuple[str, list]]:
    """Env keys in reading order, grouped under headings."""
    by_key = {k.key: k for k in plan.env_keys}
    grouped: list[tuple[str, list]] = []
    placed: set[str] = set()

    for heading, keys in _ENV_GROUPS:
        present = [by_key[k] for k in keys if k in by_key]
        if present:
            grouped.append((heading, present))
            placed.update(k.key for k in present)

    mcp = [k for k in plan.env_keys if k.key.startswith("MCP_") and k.key not in placed]
    if mcp:
        grouped.append(("MCP servers", mcp))
        placed.update(k.key for k in mcp)

    a2a = [k for k in plan.env_keys if k.key.startswith("A2A_") and k.key not in placed]
    if a2a:
        grouped.append(("Remote A2A agents", a2a))
        placed.update(k.key for k in a2a)

    rest = [k for k in plan.env_keys if k.key not in placed]
    if rest:
        grouped.append(("Other", rest))

    return grouped


# ── ASCII graph, for the README ──────────────────────────────────────────────


def ascii_graph(plan: GraphPlan) -> str:
    """A readable rendering of the topology for the generated README."""
    successors: dict[str, list] = {}
    for edge in plan.edges:
        successors.setdefault(edge.from_node, []).append(edge)

    lines: list[str] = []
    seen: set[str] = set()

    def walk(name: str, prefix: str) -> None:
        if name in seen:
            lines.append(f"{prefix}{name}  (loops back)")
            return
        seen.add(name)

        node = plan.by_name.get(name)
        label = f"{name}  [{node.node_type}]" if node else name
        lines.append(f"{prefix}{label}")

        outgoing = successors.get(name, [])
        for edge in outgoing:
            route = ""
            if edge.route is not None:
                values = edge.route if isinstance(edge.route, list) else [edge.route]
                route = f" --{'|'.join(str(v) for v in values)}-->"
            child_prefix = f"{prefix}  {route} " if route else f"{prefix}  -> "
            walk(edge.to_node, child_prefix)

    start = next((e for e in plan.edges if e.from_node == "START"), None)
    if start:
        lines.append("START")
        walk(start.to_node, "  -> ")
    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────


# Written into every package: the version of the template that produced it.
FINGERPRINT_FILE = ".template-fingerprint"

# Where a package keeps its A2A tasks and ADK sessions. Mirrors STATE_DIR in
# the template's run_once.py; a test pins the two together.
STATE_DIR_NAME = ".runs"

_fingerprint_cache: str | None = None


def template_fingerprint() -> str:
    """A hash of everything a package is rendered from.

    Cheap enough to compute once per process — the template is a few dozen
    small files — and it is the only honest answer to "was this package built
    by the code running now?".
    """
    global _fingerprint_cache
    if _fingerprint_cache is not None:
        return _fingerprint_cache

    digest = hashlib.sha256()
    for root in (TEMPLATE_DIR, RENDER_TEMPLATE_DIR):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    _fingerprint_cache = digest.hexdigest()
    return _fingerprint_cache


def render_package(plan: GraphPlan, destination: Path, *, port: int = 8080) -> RenderedPackage:
    """Write a complete package for `plan` into `destination`.

    The directory is created fresh.  Rendering happens in a temporary directory
    and is only moved into place once every file has been written and
    byte-compiled, so a failed build never leaves a half-written package behind.
    """
    if plan.unsupported:
        listed = ", ".join(f"'{n.canvas_id}' ({n.node_type})" for n in plan.unsupported)
        raise RenderError(
            f"These nodes cannot be packaged yet: {listed}. "
            "Remove them or replace them with supported nodes."
        )

    env = _environment()
    written: list[str] = []
    warnings: list[str] = list(plan.warnings)

    staging = Path(tempfile.mkdtemp(prefix="wf-render-"))
    try:
        # ── Static files, copied verbatim ────────────────────────────────────
        for relative in STATIC_FILES:
            source = TEMPLATE_DIR / relative
            if not source.exists():
                raise RenderError(f"template is missing {relative}")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            written.append(relative)

        # ── Rendered files ───────────────────────────────────────────────────
        example = {}
        entry = plan.by_name.get(plan.entry_node)
        if entry is not None:
            from app.nodes.triggers import example_payload

            example = example_payload(entry.config or {})
        example_json = json.dumps(example or {"text": "hello"}, indent=2)
        requires_network, network_reason = _network_dependencies(plan)

        simple = {
            "main.py": ("main.py.j2", {"plan": plan}),
            "agent.py": ("agent.py.j2", {"plan": plan}),
            "nodes/registry.py": ("nodes/registry.py.j2", {"plan": plan}),
            "core/agent_card.py": (
                "core/agent_card.py.j2",
                {
                    "plan": plan,
                    "skill_id": "run_workflow",
                    "skill_name": f"Run {plan.workflow_name}",
                    "skill_description": (
                        plan.workflow_description
                        or "Runs the workflow graph end to end and returns its "
                        "result as a JSON text artifact."
                    ),
                    "skill_tags": ["workflow", "a2a", "adk"],
                },
            ),
            "requirements.txt": (
                "requirements.txt.j2",
                {"plan": plan, **_requirements_context(plan)},
            ),
            ".env": ("env.j2", {"plan": plan}),
            ".env.example": (
                "env.example.j2",
                {
                    "plan": plan,
                    "grouped": _grouped_env(plan),
                    "has_llm_node": _requirements_context(plan)["has_agent"],
                },
            ),
            "README.md": (
                "README.md.j2",
                {
                    "plan": plan,
                    "port": port,
                    "dir_slug": destination.name,
                    "ascii_graph": ascii_graph(plan),
                    "example_payload_json": example_json,
                    "example_payload_escaped": json.dumps(
                        json.dumps(example or {"text": "hello"})
                    ),
                },
            ),
        }

        for relative, (template_name, context) in simple.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(env.get_template(template_name).render(**context))
            written.append(relative)

        # ── One module per node ──────────────────────────────────────────────
        for node in plan.nodes:
            if node.is_join:
                continue  # a MERGE is a JoinNode built in the registry
            template_name = NODE_TEMPLATES.get(node.node_type)
            if template_name is None:
                template_name = _PASSTHROUGH_TEMPLATE
                warnings.append(
                    f"canvas node '{node.canvas_id}' ({node.node_type}) has no "
                    "generated implementation and will pass its input through"
                )
            relative = f"nodes/{node.name}.py"
            target = staging / relative
            target.write_text(
                env.get_template(template_name).render(**_node_context(node, plan))
            )
            written.append(relative)

        # ── graph.json ───────────────────────────────────────────────────────
        (staging / "graph.json").write_text(json.dumps(plan.to_dict(), indent=2) + "\n")
        written.append("graph.json")

        # ── Generated tests ─────────────────────────────────────────────────
        # Both are per-workflow: the graph test asserts this graph's structure,
        # and the A2A test drives this workflow's own example payload.
        (staging / "tests" / "test_graph.py").write_text(_render_graph_test(plan))
        written.append("tests/test_graph.py")

        (staging / "tests" / "test_nodes.py").write_text(
            env.get_template("tests/test_nodes.py.j2").render(
                plan=plan,
                module_nodes=[n.name for n in plan.nodes if not n.is_join],
            )
        )
        written.append("tests/test_nodes.py")

        (staging / "tests" / "test_a2a.py").write_text(
            env.get_template("tests/test_a2a.py.j2").render(
                plan=plan,
                example_payload=example or {"text": "hello"},
                requires_network=requires_network,
                network_reason=network_reason,
            )
        )
        written.append("tests/test_a2a.py")

        # ── Validate before publishing ──────────────────────────────────────
        # Byte-compiling catches syntax errors, but not an undefined name: a
        # template bug that emitted `null` instead of `None` compiled happily
        # and only failed on import. Importing the graph is the real gate -- it
        # resolves every name and runs ADK's own graph validation.
        _byte_compile(staging)
        _verify_imports(staging)

        # Stamp what rendered this, so a package built by an older platform is
        # not silently reused. Reuse is otherwise keyed on the plan alone, and
        # a plan is unchanged by an edit to the template — so a template fix
        # would reach new workflows and quietly skip every existing one.
        (staging / FINGERPRINT_FILE).write_text(template_fingerprint())

        # The package's task store is data, not generated code: `.runs/`
        # holds every A2A task and ADK session the package has created, which
        # is what lets a task be looked up, or a run parked on a human node be
        # answered, by a later process. Deleting the directory wholesale took
        # those with it — so a re-render silently made every outstanding
        # approval unanswerable and every task id a dead link.
        #
        # Carried across rather than regenerated. A session written by an older
        # graph may not resume cleanly into a new one, but that reports an
        # error the user can act on, where a wipe reports nothing at all.
        preserved = None
        if (destination / STATE_DIR_NAME).is_dir():
            preserved = Path(tempfile.mkdtemp(prefix="wf-state-"))
            shutil.move(str(destination / STATE_DIR_NAME), str(preserved / STATE_DIR_NAME))

        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(destination))

        if preserved is not None:
            shutil.move(str(preserved / STATE_DIR_NAME), str(destination / STATE_DIR_NAME))
            shutil.rmtree(preserved, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return RenderedPackage(files=sorted(written), warnings=warnings)


def _verify_imports(root: Path) -> None:
    """Import the generated graph in a subprocess.

    A subprocess rather than an in-process import: the package's modules are
    named `agent`, `core.*`, `nodes.*`, which would collide with the platform's
    own imports and would be cached across builds. Running `python -c` with the
    package as the working directory is exactly how the container imports it.
    """
    import subprocess
    import sys

    probe = (
        "import agent, json;"
        "from nodes.registry import NODES, ENTRY_NODE, TERMINAL_NODE;"
        "assert agent.root_agent.edges;"
        "assert ENTRY_NODE in NODES and TERMINAL_NODE in NODES;"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            # Import must not depend on a populated .env.
            "PYTHONWARNINGS": "ignore",
        },
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {result.returncode}"
        raise RenderError(
            f"The generated package does not import: {message}\n"
            "This is a bug in the package generator, not in the canvas."
        )


def _byte_compile(root: Path) -> None:
    """Byte-compile every module so a broken package fails the build."""
    for path in sorted(root.rglob("*.py")):
        try:
            with tempfile.NamedTemporaryFile(suffix=".pyc", delete=True) as sink:
                py_compile.compile(str(path), cfile=sink.name, doraise=True)
        except py_compile.PyCompileError as exc:
            raise RenderError(
                f"Generated file {path.relative_to(root)} does not compile: {exc.msg.strip()}"
            ) from exc


def _render_graph_test(plan: GraphPlan) -> str:
    """A per-workflow structural test, shipped in the package.

    Asserts the invariants the graph relies on against this specific graph, so
    a hand-edit that breaks one is caught by `pytest` rather than at deploy.
    """
    return f'''"""The graph builds and satisfies ADK's structural rules.

Generated for {plan.workflow_name}. Runs offline: no model, no network.
"""

from __future__ import annotations

EXPECTED_NODES = {sorted(n.name for n in plan.nodes)!r}
# Nodes with a module of their own. A MERGE is a JoinNode built in the registry,
# so it has none.
MODULE_NODES = {sorted(n.name for n in plan.nodes if not n.is_join)!r}
ENTRY_NODE = {plan.entry_node!r}
TERMINAL_NODE = {plan.terminal_node!r}


def test_graph_builds():
    """Constructing the Workflow runs ADK's own graph validation."""
    from agent import root_agent

    assert root_agent.name
    assert root_agent.edges


def test_registry_matches_the_graph():
    from nodes.registry import CANVAS_IDS, ENTRY_NODE as entry, NODES, TERMINAL_NODE as terminal

    assert sorted(NODES) == EXPECTED_NODES
    assert entry == ENTRY_NODE
    assert terminal == TERMINAL_NODE
    assert set(CANVAS_IDS) == set(NODES)


def test_every_node_module_imports():
    """One file per canvas node, all importable.

    Except a MERGE: the registry builds it as a JoinNode, so it has no module
    of its own. Importing one raised ModuleNotFoundError for any workflow that
    joined parallel branches.
    """
    import importlib

    for name in MODULE_NODES:
        module = importlib.import_module(f"nodes.{{name}}")
        assert module is not None


def test_node_names_are_valid_identifiers():
    from nodes.registry import NODES

    for name in NODES:
        assert name.isidentifier(), name


def test_exactly_one_terminal_node():
    """ADK fails a run with 'multiple terminal nodes produced output'."""
    from agent import root_agent
    from nodes.registry import NODES

    has_outgoing = set()
    for edge in root_agent.edges:
        if hasattr(edge, "from_node"):
            has_outgoing.add(edge.from_node.name)
            continue
        source, *rest = edge
        current = source
        for item in rest:
            has_outgoing.add(getattr(current, "name", str(current)))
            current = item

    terminals = set(NODES) - has_outgoing
    assert terminals == {{TERMINAL_NODE}}, terminals


def test_no_duplicate_edges():
    """Two edges sharing a (from, to) pair are rejected by ADK as duplicates."""
    from agent import root_agent

    pairs = []
    for edge in root_agent.edges:
        if hasattr(edge, "from_node"):
            pairs.append((edge.from_node.name, edge.to_node.name))
            continue
        source, *rest = edge
        current = source
        for item in rest:
            pairs.append(
                (getattr(current, "name", str(current)), getattr(item, "name", str(item)))
            )
            current = item

    assert len(pairs) == len(set(pairs)), f"duplicate edges: {{pairs}}"


def test_graph_json_matches_the_registry():
    import json
    from pathlib import Path

    from nodes.registry import CANVAS_IDS

    graph = json.loads((Path(__file__).resolve().parent.parent / "graph.json").read_text())

    assert {{n["name"] for n in graph["nodes"]}} == set(CANVAS_IDS)
    assert graph["entry_node"] == ENTRY_NODE
    assert graph["terminal_node"] == TERMINAL_NODE
'''
