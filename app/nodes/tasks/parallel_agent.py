"""PARALLEL_AGENT — runs its connected tools at the same time.

A tool group, not a graph node: it collects the tools wired into its `tools`
handle and hands its consumer (an ORCHESTRATOR_AGENT, AGENT or LLM_AGENT) a single
tool that runs them all concurrently on the same input, returning every result
keyed by tool name.

Use it when several tools answer the same question independently — several
sources to compare, or several checks to run at once. Use SEQUENTIAL_AGENT when
one tool needs another's output.

ADK's own `ParallelAgent` is not used here: it is deprecated in favour of
`Workflow`, and `Workflow` cannot be attached to an `LlmAgent` as a tool or
sub-agent. See `packaging/template/tools/groups.py`.
"""

from dataclasses import dataclass

from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class ParallelAgentNode(NodeDefinition):
    node_type: str = "PARALLEL_AGENT"
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
            label="Parallel Tools",
            category="ai",
            color="#0d9488",
            icon="Rows3",
            description="Runs connected tools at the same time on the same input",
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
                        "What this group does. The model reads this to decide "
                        "when to call it; a generated default lists the tools."
                    ),
                },
                "max_concurrency": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": (
                        "Most tools to run at once. 0 means no limit. Use it to "
                        "stay inside a rate limit."
                    ),
                },
                "stop_on_error": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Report the whole group as failed if any tool fails. Off "
                        "by default, so one dead source does not lose the others."
                    ),
                },
            },
            "required": [],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {"type": "object"}
        self.output_handles = ["output"]
