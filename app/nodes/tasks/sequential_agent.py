"""SEQUENTIAL_AGENT — runs its connected tools in a fixed order.

A tool group, not a graph node: it collects the tools wired into its `tools`
handle and hands its consumer (an ORCHESTRATOR_AGENT, AGENT or LLM_AGENT) a single
tool that runs them one after another. Each tool's output is merged into the
payload passed to the next, so a later tool can consume an earlier one's result.

This replaces the old `tool_execution_mode` setting on ORCHESTRATOR_AGENT — the
ordering is now something you draw on the canvas rather than a dropdown.

ADK's own `SequentialAgent` is not used here, for two reasons: it is deprecated
in favour of `Workflow`, and `Workflow` cannot be attached to an `LlmAgent` as a
tool or sub-agent. See `packaging/template/tools/groups.py`.
"""

from dataclasses import dataclass

from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class SequentialAgentNode(NodeDefinition):
    node_type: str = "SEQUENTIAL_AGENT"
    version: str = "1"
    palette: PaletteMetadata = None
    accepts_tools: bool = True
    is_tool_group: bool = True

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Sequential Tools",
            category="ai",
            color="#0891b2",
            icon="ListOrdered",
            description="Runs connected tools in a set order, one after another",
            wave=1,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "The tool name the model sees. Defaults to the node title."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "What this pipeline does. The model reads this to decide "
                        "when to call it; a generated default lists the steps."
                    ),
                },
                "order": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Canvas node ids of the connected tools, in execution "
                        "order. Anything connected but not listed runs last, in "
                        "canvas order."
                    ),
                },
                "stop_on_error": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Stop the pipeline at the first failing step. Turn off to "
                        "run every step and report which failed."
                    ),
                },
            },
            "required": [],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        # Feeds a consumer's "tools" handle.
        self.output_handles = ["output"]
