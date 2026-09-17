"""Intermediate Representation (IR) types and the canvas -> IR compiler."""
from dataclasses import dataclass, field
from typing import Any
from app.schemas.canvas import CanvasPayload
from app.nodes.agent_io import fields_to_json_schema
from app.nodes.registry import (
    BRANCH_NODE_TYPES,
    TOOL_CONSUMER_TYPES,
    TOOL_GROUP_TYPES,
    TOOL_PROVIDER_TYPES,
    get_node_definition,
)


@dataclass
class IRNode:
    id: str
    kind: str                    # e.g. "trigger.a2a_start", "task.agent"
    node_type: str               # original type string
    config: dict[str, Any]
    policies: dict[str, Any]
    metadata: dict[str, Any]
    io: dict[str, Any]
    next: list[str] = field(default_factory=list)
    branches: dict[str, list[str]] = field(default_factory=dict)  # for CONDITION, PARALLEL_FORK


@dataclass
class IR:
    workflow_version_id: str
    entrypoints: list[str]
    nodes: dict[str, IRNode]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_version_id": self.workflow_version_id,
            "entrypoints": self.entrypoints,
            "nodes": {
                nid: {
                    "kind": n.kind,
                    "node_type": n.node_type,
                    "config": n.config,
                    "policies": n.policies,
                    "metadata": n.metadata,
                    "io": n.io,
                    "next": n.next,
                    "branches": n.branches,
                }
                for nid, n in self.nodes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IR":
        nodes = {
            nid: IRNode(
                id=nid,
                kind=n["kind"],
                node_type=n["node_type"],
                config=n.get("config", {}),
                policies=n.get("policies", {}),
                metadata=n.get("metadata", {}),
                io=n.get("io", {}),
                next=n.get("next", []),
                branches=n.get("branches", {}),
            )
            for nid, n in data.get("nodes", {}).items()
        }
        return cls(
            workflow_version_id=data["workflow_version_id"],
            entrypoints=data.get("entrypoints", []),
            nodes=nodes,
        )


_KIND_MAP = {
    # A2A_START is the only entry kind. The old trigger kinds (trigger.http,
    # trigger.schedule, trigger.webhook, trigger.queue) were removed when
    # packaging moved to ADK graph workflows served over A2A.
    "A2A_START": "trigger.a2a_start",
    "AGENT": "task.agent",
    "ORCHESTRATOR_AGENT": "task.orchestrator_agent",
    "REMOTE_AGENT": "task.remote_agent",
    "FUNCTION": "task.function",
    "LLM_AGENT": "agent.llm",
    "TOOL": "task.tool",
    "MCP_TOOL": "task.mcp_tool",
    "CONDITION": "task.condition",
    "LOOP": "task.loop",
    "TRANSFORM": "task.transform",
    "END": "task.end",
    "DATASOURCE": "task.datasource",
    "HUMAN_APPROVAL": "task.human_approval",
    "SUBWORKFLOW": "task.subworkflow",
    "PARALLEL_FORK": "task.parallel_fork",
    "MERGE": "task.merge",
    "SEQUENTIAL_AGENT": "tools.sequential",
    "PARALLEL_AGENT": "tools.parallel",
}

# Which node types can be wired into a "tools" handle, and which types have
# one, are derived from the node declarations in app/nodes/registry.py.


def compile_to_ir(canvas: CanvasPayload, version_id: str) -> IR:
    """Convert validated canvas JSON into a normalized IR."""
    nodes_by_id = {n.id: n for n in canvas.nodes}

    # Resolve tool-provision edges. A provider wired into a consumer's "tools"
    # handle becomes a tool that consumer may call, and the edge is excluded
    # from the execution flow. Consumers are the agent types plus the groups —
    # a group's children arrive on its own tools handle, so this nests.
    tool_provision: dict[str, list] = {}          # consumer_id -> [source nodes]
    tool_edge_pairs: set[tuple[str, str]] = set()  # (src_id, tgt_id)

    for edge in canvas.edges:
        src = nodes_by_id.get(edge.source)
        tgt = nodes_by_id.get(edge.target)
        if (src and tgt
                and src.type in TOOL_PROVIDER_TYPES
                and tgt.type in TOOL_CONSUMER_TYPES
                and edge.target_handle == "tools"):
            tool_provision.setdefault(edge.target, []).append(src)
            tool_edge_pairs.add((edge.source, edge.target))

    # Normal execution adjacency — excludes tool-provision edges
    adjacency: dict[str, list[tuple[str, str]]] = {n.id: [] for n in canvas.nodes}
    for edge in canvas.edges:
        if (edge.source, edge.target) not in tool_edge_pairs:
            adjacency[edge.source].append((edge.target, edge.source_handle))

    ir_nodes: dict[str, IRNode] = {}
    entrypoints: list[str] = []

    for node in canvas.nodes:
        defn = get_node_definition(node.type)
        kind = _KIND_MAP.get(node.type, f"unknown.{node.type.lower()}")

        # A branch node's outbound edges are keyed by handle; everything else
        # puts every outbound edge into next[].
        branches: dict[str, list[str]] = {}
        if node.type in BRANCH_NODE_TYPES:
            for (tgt, handle) in adjacency[node.id]:
                branches.setdefault(handle, []).append(tgt)
            next_nodes: list[str] = []
        else:
            # Include ALL handles (output, error, approved, rejected, …)
            next_nodes = [tgt for (tgt, _handle) in adjacency[node.id]]

        # Any tool consumer gets resolved_tools embedded in its config.
        if node.type in TOOL_CONSUMER_TYPES:
            resolved = _resolve_tools(node.id, tool_provision, set())
            config = {**node.config, "resolved_tools": resolved}
        else:
            config = node.config

        ir_node = IRNode(
            id=node.id,
            kind=kind,
            node_type=node.type,
            config=config,
            policies={
                "timeout_seconds": node.policies.timeout_seconds,
                "retry_max_attempts": node.policies.retry.get("max_attempts", 1),
                "on_error": node.policies.on_error,
            },
            metadata={"title": node.metadata.title, "description": node.metadata.description},
            io={"input_schema": node.io.input_schema, "output_schema": node.io.output_schema},
            next=next_nodes if node.type not in ("CONDITION", "PARALLEL_FORK") else [],
            branches=branches,
        )
        ir_nodes[node.id] = ir_node

        if defn and defn.is_trigger:
            entrypoints.append(node.id)

    return IR(workflow_version_id=version_id, entrypoints=entrypoints, nodes=ir_nodes)


def _tool_entry(
    src,
    tool_provision: dict[str, list] | None = None,
    seen: set[str] | None = None,
) -> dict | None:
    """One leaf provider, as the shape the package's tool builders consume.

    `tool_provision` and `seen` are only needed for LLM_AGENT, which is a leaf
    from its consumer's point of view but a consumer in its own right: a
    sub-agent may have its own MCP tools, remote agents and groups.
    """
    if src.type == "LLM_AGENT":
        return _agent_entry(src, tool_provision or {}, seen or set())

    if src.type in ("TOOL", "DATASOURCE"):
        entry = {
            "kind": "mcp",
            "name": src.metadata.title or src.id,
            "url": src.config.get("mcp_url", ""),
            "auth_token": src.config.get("auth_token", ""),
            "transport": src.config.get("transport", "http"),
            "tool_name": src.config.get("tool_name", ""),
            "node_id": src.id,
            "node_type": src.type,
        }
        # A tool an agent may call on its own initiative is exactly where a
        # human gate belongs: the model picks the moment, a person approves it.
        # ADK parks the whole A2A task at `input-required` until then. Only set
        # when on, so an ordinary tool entry is unchanged.
        if src.config.get("require_confirmation"):
            entry["require_confirmation"] = True
        return entry

    if src.type == "REMOTE_AGENT":
        return {
            "kind": "a2a",
            "name": src.config.get("name") or src.metadata.title or src.id,
            "endpoint": src.config.get("endpoint", ""),
            "description": src.config.get("description", ""),
            "auth_token": src.config.get("auth_token", ""),
            "node_id": src.id,
            "node_type": src.type,
        }
    if src.type == "FUNCTION":
        return {
            "kind": "function",
            "name": src.config.get("name") or src.metadata.title or src.id,
            "description": src.config.get("description", ""),
            "parameters": src.config.get("parameters") or {"type": "object", "properties": {}},
            "code": src.config.get("code", "result = data"),
            "node_id": src.id,
            "node_type": src.type,
        }
    return None


def _agent_entry(
    src,
    tool_provision: dict[str, list],
    seen: set[str],
) -> dict[str, Any]:
    """An LLM_AGENT wired into a consumer, as a sub-agent.

    Carries its own resolved tools, so a sub-agent with MCP tools of its own
    generates correctly. `input_schema` / `output_schema` are converted from the
    canvas field lists here, because the package needs JSON Schema, not the
    authoring shape.
    """
    config = src.config or {}
    return {
        "kind": "agent",
        "name": config.get("name") or src.metadata.title or src.id,
        "description": config.get("description", ""),
        "instruction": config.get("system_prompt", ""),
        "provider": config.get("provider", "google"),
        "model": config.get("model", ""),
        "temperature": config.get("temperature"),
        "max_tokens": config.get("max_tokens"),
        "input_schema": fields_to_json_schema(config.get("input_structure")),
        "output_schema": fields_to_json_schema(config.get("output_structure")),
        "output_key": config.get("output_key", ""),
        # A sub-agent is itself a tool consumer.
        "tools": _resolve_tools(src.id, tool_provision, seen),
        "node_id": src.id,
        "node_type": src.type,
    }


def _resolve_tools(
    consumer_id: str,
    tool_provision: dict[str, list],
    seen: set[str],
) -> dict[str, Any]:
    """The tools wired into one consumer, recursing through nested groups.

    Returns the three flat leaf buckets the package's tool builders already
    understand, plus `groups`. A group's `children` is one ordered list in which
    a nested group appears as an entry of its own, so order is preserved even
    when a group mixes leaf tools and sub-groups.

    `seen` guards against a cycle in the tool wiring, which the flow-graph cycle
    check cannot see because tool edges are excluded from that graph.
    """
    resolved: dict[str, Any] = {
        "mcp_servers": [],
        "a2a_agents": [],
        "functions": [],
        "groups": [],
        "agents": [],
    }
    if consumer_id in seen:
        return resolved
    seen = seen | {consumer_id}

    for src in tool_provision.get(consumer_id, []):
        if src.type in TOOL_GROUP_TYPES:
            resolved["groups"].append(_resolve_group(src, tool_provision, seen))
            continue

        entry = _tool_entry(src, tool_provision, seen)
        if entry is None:
            continue
        bucket = {
            "mcp": "mcp_servers",
            "a2a": "a2a_agents",
            "function": "functions",
            "agent": "agents",
        }[entry["kind"]]
        resolved[bucket].append(entry)

    return resolved


def _resolve_group(
    group,
    tool_provision: dict[str, list],
    seen: set[str],
) -> dict[str, Any]:
    """One tool group, with its children in execution order."""
    children_nodes = tool_provision.get(group.id, [])

    # `order` lists canvas node ids. Anything connected but unlisted runs last,
    # in canvas order, so a half-filled order still produces a usable group.
    requested = [str(x) for x in (group.config.get("order") or [])]
    position = {node_id: index for index, node_id in enumerate(requested)}
    ordered = sorted(
        children_nodes, key=lambda child: position.get(child.id, len(requested))
    )

    children: list[dict[str, Any]] = []
    for child in ordered:
        if child.type in TOOL_GROUP_TYPES:
            if child.id in seen:
                continue  # the validator reports the cycle
            children.append(_resolve_group(child, tool_provision, seen | {group.id}))
            continue
        entry = _tool_entry(child, tool_provision, seen | {group.id})
        if entry is not None:
            children.append(entry)

    return {
        "kind": "group",
        "name": group.config.get("name") or group.metadata.title or group.id,
        "mode": "sequential" if group.type == "SEQUENTIAL_AGENT" else "parallel",
        "description": group.config.get("description", ""),
        "stop_on_error": group.config.get(
            "stop_on_error", group.type == "SEQUENTIAL_AGENT"
        ),
        "max_concurrency": group.config.get("max_concurrency", 0),
        "children": children,
        "node_id": group.id,
        "node_type": group.type,
    }
