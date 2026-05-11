"""Intermediate Representation (IR) types and the canvas -> IR compiler."""
from dataclasses import dataclass, field
from typing import Any
from app.schemas.canvas import CanvasPayload
from app.nodes.registry import get_node_definition


@dataclass
class IRNode:
    id: str
    kind: str                    # e.g. "trigger.http", "task.agent"
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
    "HTTP_TRIGGER": "trigger.http",
    "SCHEDULE_TRIGGER": "trigger.schedule",
    "WEBHOOK_TRIGGER": "trigger.webhook",
    "QUEUE_TRIGGER": "trigger.queue",
    "AGENT": "task.agent",
    "ORCHESTRATOR_AGENT": "task.orchestrator_agent",
    "REMOTE_AGENT": "task.remote_agent",
    "FUNCTION": "task.function",
    "MODEL": "task.model",
    "TOOL": "task.tool",
    "CONDITION": "task.condition",
    "LOOP": "task.loop",
    "TRANSFORM": "task.transform",
    "END": "task.end",
    "DATASOURCE": "task.datasource",
    "HUMAN_APPROVAL": "task.human_approval",
    "SUBWORKFLOW": "task.subworkflow",
    "PARALLEL_FORK": "task.parallel_fork",
    "MERGE": "task.merge",
}

# Node types that can be wired as tools into ORCHESTRATOR_AGENT
_TOOL_PROVIDER_TYPES = {"TOOL", "DATASOURCE", "REMOTE_AGENT", "FUNCTION"}


def compile_to_ir(canvas: CanvasPayload, version_id: str) -> IR:
    """Convert validated canvas JSON into a normalized IR."""
    nodes_by_id = {n.id: n for n in canvas.nodes}

    # Resolve tool-provision edges: TOOL/DATASOURCE/REMOTE_AGENT/FUNCTION → ORCHESTRATOR_AGENT
    # These use target_handle "tools" and are excluded from the normal execution flow.
    tool_provision: dict[str, list] = {}   # orchestrator_id -> [source_canvas_nodes]
    tool_edge_pairs: set[tuple[str, str]] = set()  # (src_id, tgt_id)

    for edge in canvas.edges:
        src = nodes_by_id.get(edge.source)
        tgt = nodes_by_id.get(edge.target)
        if (src and tgt
                and src.type in _TOOL_PROVIDER_TYPES
                and tgt.type == "ORCHESTRATOR_AGENT"
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

        # CONDITION, PARALLEL_FORK, and LOOP use named branches — engine reads branches dict.
        # All other nodes: every outbound edge goes into next[].
        _BRANCH_NODES = {"CONDITION", "PARALLEL_FORK", "LOOP"}
        branches: dict[str, list[str]] = {}
        if node.type in _BRANCH_NODES:
            for (tgt, handle) in adjacency[node.id]:
                branches.setdefault(handle, []).append(tgt)
            next_nodes: list[str] = []
        else:
            # Include ALL handles (output, error, approved, rejected, …)
            next_nodes = [tgt for (tgt, _handle) in adjacency[node.id]]

        # For ORCHESTRATOR_AGENT: embed resolved_tools from connected tool-provider nodes
        if node.type == "ORCHESTRATOR_AGENT":
            resolved: dict = {"mcp_servers": [], "a2a_agents": [], "functions": []}
            for src in tool_provision.get(node.id, []):
                if src.type in ("TOOL", "DATASOURCE"):
                    resolved["mcp_servers"].append({
                        "name": src.metadata.title or src.id,
                        "url": src.config.get("mcp_url", ""),
                        "transport": src.config.get("transport", "http"),
                        "node_id": src.id,
                        "node_type": src.type,
                    })
                elif src.type == "REMOTE_AGENT":
                    resolved["a2a_agents"].append({
                        "name": src.config.get("name") or src.metadata.title or src.id,
                        "endpoint": src.config.get("endpoint", ""),
                        "description": src.config.get("description", ""),
                        "auth_token": src.config.get("auth_token", ""),
                        "node_id": src.id,
                        "node_type": src.type,
                    })
                elif src.type == "FUNCTION":
                    resolved["functions"].append({
                        "name": src.config.get("name") or src.metadata.title or src.id,
                        "description": src.config.get("description", ""),
                        "parameters": src.config.get("parameters") or {"type": "object", "properties": {}},
                        "code": src.config.get("code", "result = data"),
                        "node_id": src.id,
                        "node_type": src.type,
                    })
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
