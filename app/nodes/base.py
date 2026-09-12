"""What a node type is, from the platform's point of view.

A NodeDefinition is a *declaration*, not an implementation: the palette entry,
the config contract the UI edits and the compiler validates, and the handles
the canvas can wire. It deliberately carries no behaviour.

Node behaviour lives once, in the package template
(`app/packaging/template/nodes/` and `app/packaging/render_templates/nodes/`).
It used to live here too, executed by a second engine in `app/runtime/`, and
the two had already diverged — LOOP ran real iterations in the platform and a
single pass in a generated package. Removing this half is what makes a test
run and a deployment the same thing.
"""

from dataclasses import dataclass, field


@dataclass
class PaletteMetadata:
    label: str
    category: str          # "triggers" | "flow" | "ai" | "data"
    color: str             # hex
    icon: str              # lucide icon name
    description: str
    wave: int = 1          # 1 = first-wave, 2 = second-wave


@dataclass
class NodeDefinition:
    node_type: str
    version: str
    palette: PaletteMetadata

    # JSON Schema for the node's config block
    config_schema: dict = field(default_factory=dict)

    # JSON Schema for expected input payload
    input_schema: dict = field(default_factory=lambda: {"type": "object"})

    # JSON Schema for output payload
    output_schema: dict = field(default_factory=lambda: {"type": "object"})

    # Named output handles (e.g. CONDITION has "true" and "false")
    output_handles: list[str] = field(default_factory=lambda: ["output"])

    # Whether this node can have inbound edges
    allows_inbound: bool = True

    # Whether this node can have outbound edges
    allows_outbound: bool = True

    # Whether this node starts an execution path (is a trigger)
    is_trigger: bool = False

    # Whether this node terminates an execution path
    is_terminal: bool = False

    # Whether cycles through this node are allowed
    allows_cycle: bool = False

    # Whether this node can honour `on_error: continue`.
    #
    # True for nodes that fail for reasons outside the workflow -- a model call,
    # an MCP server, a remote agent, user code -- where carrying on with an
    # error payload is a reasonable choice, and the generated module emits
    # `node_error()` so a downstream router can branch on `_error`.
    #
    # False for structural nodes, where continuing is meaningless rather than
    # lenient: a CONDITION that "continues" has no route to take, an END that
    # continues cannot emit a result, and a failed A2A_START has no payload to
    # pass on. The validator warns rather than accepting a setting that would be
    # silently ignored.
    supports_on_error_continue: bool = False

    # Whether this node has a "tools" target handle — i.e. other nodes can be
    # wired into it to become tools it may call. True for the agent types and
    # for tool groups (a group's children arrive on its tools handle).
    accepts_tools: bool = False

    # Whether this node is a tool group: it collects several tools and exposes
    # them to its consumer as one, run sequentially or concurrently. Groups are
    # not graph nodes — like a TOOL wired to an agent, they are resolved into
    # the consumer's tool list.
    is_tool_group: bool = False

    # Whether this node's outbound edges are named routes rather than plain
    # flow, i.e. the generated module emits `Event(route=...)` and only the
    # matching edge is taken.
    #
    # Not derivable from `output_handles`: an agent or a tool also has two
    # handles ("output" and "error"), but those are a result and a failure
    # path, not a choice the node makes. This was a hardcoded list until
    # HUMAN_APPROVAL grew a template and was left out of it, so both its
    # branches ran and the losing one's output overwrote the winner's.
    uses_named_routes: bool = False

    # Whether this node is generated as an ADK `LlmAgent`.
    #
    # Drives two things: the node may declare an input/output structure (ADK's
    # `input_schema` / `output_schema`), and it may be attached to another agent
    # as a sub-agent rather than hand-wrapped as a tool.
    is_agent: bool = False

    # Whether this node can be wired *into* a "tools" handle.
    #
    # The leaf providers (an MCP tool, a datasource, a remote agent, an inline
    # function) plus LLM_AGENT, which ADK exposes to its consumer as a tool by
    # itself. Tool groups are providers too, but they declare that through
    # `is_tool_group`, because a group is also a consumer.
    provides_tool: bool = False

    # Config keys whose values are credentials, or may embed them.
    #
    # These never reach the generated code or `graph.json`: the compiler moves
    # each one to an environment variable, writes the canvas value as that
    # variable's default in `.env` (which is gitignored), and leaves it blank in
    # the committed `.env.example`. `graph.json` is both committed by the
    # package's git repo and served by `GET /graph`, so a literal there would be
    # exposed twice over.
    secret_config_keys: frozenset[str] = frozenset()

    def validate_config(self, config: dict) -> list[str]:
        """Validate node config against config_schema. Returns list of error strings."""
        from jsonschema import validate, ValidationError
        errors = []
        try:
            validate(instance=config, schema=self.config_schema)
        except ValidationError as e:
            errors.append(e.message)
        return errors
