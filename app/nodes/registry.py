from app.nodes.base import NodeDefinition
from app.nodes.triggers import A2aStartNode
from app.nodes.tasks import (
    AgentNode, OrchestratorAgentNode, RemoteAgentNode, FunctionNode,
    LlmAgentNode, ToolNode, ConditionNode, LoopNode,
    TransformNode, EndNode, DataSourceNode, HumanApprovalNode,
    SubworkflowNode, ParallelForkNode, MergeNode, HumanInputNode,
    SequentialAgentNode, ParallelAgentNode,
)

NODE_REGISTRY: dict[str, NodeDefinition] = {}


def _register(node: NodeDefinition) -> None:
    NODE_REGISTRY[node.node_type] = node


# ── Entry ────────────────────────────────────────────────────────────────────
# A2A_START is the only entry node. The old HTTP / SCHEDULE / WEBHOOK / QUEUE
# triggers were removed when packaging moved to ADK graph workflows served over
# A2A: a packaged workflow is invoked by a message, not by a path or a cron.
# Saved canvases are rewritten by app/compiler/canvas_migrations.py.
_register(A2aStartNode())

# ── Wave 1 — tasks ───────────────────────────────────────────────────────────
_register(AgentNode())
_register(OrchestratorAgentNode())
_register(RemoteAgentNode())
_register(FunctionNode())
_register(LlmAgentNode())
_register(ToolNode())
_register(ConditionNode())
_register(LoopNode())
_register(TransformNode())
_register(EndNode())

# Tool groups — they collect tools and expose them to a consumer as one.
_register(SequentialAgentNode())
_register(ParallelAgentNode())

# ── Wave 2 ───────────────────────────────────────────────────────────────────
_register(DataSourceNode())
_register(HumanApprovalNode())
_register(HumanInputNode())
_register(SubworkflowNode())
_register(ParallelForkNode())
_register(MergeNode())


# ── Tool wiring ──────────────────────────────────────────────────────────────
# Derived from the node declarations so these sets cannot drift from them.

# Types with a "tools" target handle: things can be wired into them to become
# tools they may call.
TOOL_CONSUMER_TYPES: frozenset[str] = frozenset(
    t for t, n in NODE_REGISTRY.items() if n.accepts_tools
)

# Types that collect tools and expose them to their consumer as a single tool.
TOOL_GROUP_TYPES: frozenset[str] = frozenset(
    t for t, n in NODE_REGISTRY.items() if n.is_tool_group
)

# Types that can be wired *into* a "tools" handle. The leaf providers, plus the
# groups (a group can feed another group, or an agent).
TOOL_LEAF_TYPES: frozenset[str] = frozenset(
    t for t, n in NODE_REGISTRY.items() if n.provides_tool
)
TOOL_PROVIDER_TYPES: frozenset[str] = TOOL_LEAF_TYPES | TOOL_GROUP_TYPES

# Types generated as an ADK `LlmAgent`, so they may declare an input/output
# structure and can be attached to another agent as a sub-agent.
AGENT_TYPES: frozenset[str] = frozenset(
    t for t, n in NODE_REGISTRY.items() if n.is_agent
)

# Agent types that can be wired into a "tools" handle, i.e. become a sub-agent
# of another agent or a member of a tool group.
SUBAGENT_TYPES: frozenset[str] = AGENT_TYPES & TOOL_LEAF_TYPES


# Types whose outbound edges are named routes, so the compiler keys them by
# handle instead of treating every edge as plain flow.
BRANCH_NODE_TYPES: frozenset[str] = frozenset(
    t for t, n in NODE_REGISTRY.items() if n.uses_named_routes
)


# Node types that were removed. Kept as a named set so the canvas migration and
# the validator can give a specific error instead of "unknown node type".
RETIRED_TRIGGER_TYPES: frozenset[str] = frozenset(
    {"HTTP_TRIGGER", "SCHEDULE_TRIGGER", "WEBHOOK_TRIGGER", "QUEUE_TRIGGER"}
)


def get_node_definition(node_type: str) -> NodeDefinition | None:
    return NODE_REGISTRY.get(node_type)


def get_palette() -> list[dict]:
    """Return palette metadata for all registered node types, sorted by wave then category."""
    nodes = sorted(NODE_REGISTRY.values(), key=lambda n: (n.palette.wave, n.palette.category, n.palette.label))
    return [
        {
            "type": n.node_type,
            "version": n.version,
            "label": n.palette.label,
            "category": n.palette.category,
            "color": n.palette.color,
            "icon": n.palette.icon,
            "description": n.palette.description,
            "wave": n.palette.wave,
            "is_trigger": n.is_trigger,
            "is_terminal": n.is_terminal,
            "output_handles": n.output_handles,
            "config_schema": n.config_schema,
            "input_schema": n.input_schema,
            "output_schema": n.output_schema,
        }
        for n in nodes
    ]
