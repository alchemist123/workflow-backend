from app.nodes.base import NodeDefinition
from app.nodes.triggers import HttpTriggerNode, ScheduleTriggerNode, WebhookTriggerNode
from app.nodes.tasks import (
    AgentNode, OrchestratorAgentNode, RemoteAgentNode, FunctionNode,
    ModelNode, ToolNode, ConditionNode, LoopNode,
    TransformNode, EndNode, DataSourceNode, HumanApprovalNode,
    QueueTriggerNode, SubworkflowNode, ParallelForkNode, MergeNode,
)

NODE_REGISTRY: dict[str, NodeDefinition] = {}


def _register(node: NodeDefinition) -> None:
    NODE_REGISTRY[node.node_type] = node


# Wave 1 — triggers
_register(HttpTriggerNode())
_register(ScheduleTriggerNode())
_register(WebhookTriggerNode())

# Wave 1 — tasks
_register(AgentNode())
_register(OrchestratorAgentNode())
_register(RemoteAgentNode())
_register(FunctionNode())
_register(ModelNode())
_register(ToolNode())
_register(ConditionNode())
_register(LoopNode())
_register(TransformNode())
_register(EndNode())

# Wave 2
_register(DataSourceNode())
_register(HumanApprovalNode())
_register(QueueTriggerNode())
_register(SubworkflowNode())
_register(ParallelForkNode())
_register(MergeNode())


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
