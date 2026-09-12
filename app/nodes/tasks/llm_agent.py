"""LLM_AGENT — one ADK `LlmAgent`, usable in the flow or as a sub-agent.

This is the composable agent unit. It plays three roles depending on how it is
wired, and the wiring is the only thing that decides:

1. **In the flow** — a graph node. Reads state, calls the model, writes its
   reply on. With nothing on its `tools` handle this is a single
   `generate_content` round trip; wire tools in and it becomes an agent loop,
   because a model that can call tools needs a loop to call them in.

2. **Attached to another agent's `tools` handle** — a sub-agent. ADK's
   recommended mechanism is `mode='single_turn'` plus `sub_agents=[...]`, which
   makes the framework expose it to the parent as a tool automatically and run
   it inline in the parent's session. (ADK's own docstring calls direct use of
   `AgentTool` discouraged.) Its `input_structure` becomes the parameters the
   parent's model sees, so declaring one is what makes it callable with real
   arguments rather than a blob of text.

3. **Inside a Sequential Tools or Parallel Tools group** — a group member. A
   group is a deterministic composite tool rather than an agent, so it drives
   the child itself through the child's own `Runner`. That path leaves `mode`
   unset, because ADK refuses to run a `single_turn` agent as a Runner's root.

Naming: this node used to be called MODEL. It was renamed once it could take
tools and be attached to another agent, because "model" undersold it — a
canvas node that owns an instruction, a tool list and an output contract is an
agent, not a model call. `canvas_migrations` rewrites saved MODEL nodes.
"""

from dataclasses import dataclass

from app.nodes.agent_io import io_config_properties
from app.nodes.base import NodeDefinition, PaletteMetadata

_GOOGLE_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]

_VERTEX_MODELS = [
    "gemini-2.0-flash-001",
    "gemini-2.0-flash-lite-001",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-1.5-pro-001",
    "gemini-1.5-flash-001",
]


@dataclass
class LlmAgentNode(NodeDefinition):
    node_type: str = "LLM_AGENT"
    accepts_tools: bool = True
    is_agent: bool = True
    provides_tool: bool = True
    secret_config_keys: frozenset[str] = frozenset({"api_key", "service_account_json"})
    supports_on_error_continue: bool = True
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="LLM Agent",
            category="ai",
            color="#10b981",
            icon="Bot",
            description=(
                "An LLM agent — run it in the flow, or connect it to another "
                "agent or a tool group as a sub-agent"
            ),
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Name the calling model sees when this agent is used as "
                        "a tool. Defaults to the node title."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "What this agent is for. Used as the tool description "
                        "when another agent calls it, so it decides whether the "
                        "model picks it at all."
                    ),
                },
                "provider": {
                    "type": "string",
                    "enum": ["google", "vertex_ai"],
                    "default": "google",
                    "description": "LLM provider",
                },
                "model": {
                    "type": "string",
                    "default": "gemini-2.0-flash",
                    "description": "Model name",
                },
                "api_key": {
                    "type": "string",
                    "description": "Google AI Studio API key — overrides GOOGLE_API_KEY env var",
                },
                "vertex_project": {
                    "type": "string",
                    "description": "Google Cloud project ID (Vertex AI only)",
                },
                "vertex_location": {
                    "type": "string",
                    "default": "us-central1",
                    "description": "Vertex AI region",
                },
                "service_account_json": {
                    "type": "string",
                    "description": "Service account key JSON (Vertex AI) — leave empty to use ADC",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "The agent's instruction.",
                },
                "prompt_template": {
                    "type": "string",
                    "description": (
                        "Jinja2 template rendered with input fields. Only used "
                        "when this node runs in the flow; ignored when it is "
                        "called as a sub-agent, where the caller supplies the "
                        "arguments."
                    ),
                },
                "max_tokens": {"type": "integer", "minimum": 1, "default": 1024},
                "temperature": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 2,
                    "default": 0.7,
                },
                **io_config_properties(),
            },
            "required": [],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "parsed": {},
                "usage": {"type": "object"},
            },
        }
        self.output_handles = ["output"]
