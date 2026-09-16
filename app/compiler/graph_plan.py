"""Layer 3: compile the IR into an ADK graph plan.

The IR describes the canvas; the graph plan describes the ADK `Workflow` that
will be generated from it.  Keeping them separate means the canvas model does
not have to know about ADK's constraints, and the packaging layer does not have
to know about the canvas.

The plan is serialised to `graph.json` in every generated package, and Phase 4's
renderer reads nothing else.  Its shape is asserted by
`tests/test_template_integrity.py`.

Three ADK rules shape this module.  Each is proven in
`tests/test_adk_contract.py`:

  * **No duplicate edges.** Two edges sharing a `(from, to)` pair are rejected
    outright, even with different routes, so `_dedupe_edges` merges them into a
    single edge carrying a list of route values.
  * **Exactly one terminal node.** A node with no outgoing edge is terminal, and
    more than one makes the run fail during finalisation. `_find_terminal`
    reports the offenders by canvas id.
  * **Node names become Python identifiers** and appear in
    `event.node_info.path`, so they must be unique, valid and stable.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass, field
from typing import Any

from app.compiler.ir import IR, IRNode
from app.nodes.registry import TOOL_PROVIDER_TYPES, get_node_definition

GRAPH_SCHEMA_VERSION = 1

# The name given to the single entry node. There is exactly one A2A_START, so it
# needs no ordinal and reads better without one.
ENTRY_NODE_NAME = "a2a_start"

# Types that provide tools to a consumer rather than sitting in the flow. When
# one is wired only to a "tools" handle it is not a graph node. Derived from the
# node declarations, so tool groups are included automatically.
_TOOL_PROVIDER_TYPES = TOOL_PROVIDER_TYPES

# Branch handles that carry flow rather than a routing decision. PARALLEL_FORK
# fans out with plain edges (ADK dispatches to every successor), so its handles
# are not route values.
_FANOUT_TYPES = {"PARALLEL_FORK"}

# Canvas types that compile to a graph node but that the packaging layer does
# not yet generate a working implementation for.
# HUMAN_APPROVAL was here until it grew a template: ADK graphs interrupt
# natively with RequestInput, so the node needed no machinery of its own.
_UNSUPPORTED_TYPES = {"SUBWORKFLOW"}


class GraphPlanError(Exception):
    """A canvas that passed validation still cannot become a legal ADK graph.

    Every message names the canvas node or nodes at fault, because the user sees
    this in the UI and has to act on it.
    """


@dataclass
class PlannedNode:
    name: str                       # ADK node name / python identifier
    canvas_id: str                  # the canvas node it came from
    node_type: str                  # e.g. "TRANSFORM"
    module: str                     # e.g. "nodes.n_transform_3"
    title: str = ""
    description: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    timeout: int | None = None
    retries: int = 1
    on_error: str = "fail"
    routes: list[str] = field(default_factory=list)
    default_route: str | None = None
    is_entry: bool = False
    is_terminal: bool = False
    is_join: bool = False
    supported: bool = True

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "canvas_id": self.canvas_id,
            "type": self.node_type,
            "module": self.module,
            "title": self.title,
            "timeout": self.timeout,
            "retries": self.retries,
            "on_error": self.on_error,
        }
        if self.description:
            out["description"] = self.description
        if self.config:
            out["config"] = self.config
        if self.routes:
            out["routes"] = self.routes
        if self.default_route:
            out["default_route"] = self.default_route
        for flag in ("is_entry", "is_terminal", "is_join"):
            if getattr(self, flag):
                out[flag] = True
        if not self.supported:
            out["supported"] = False
        return out


@dataclass
class PlannedEdge:
    from_node: str                  # planned node name, or "START"
    to_node: str
    route: str | list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.from_node, "to": self.to_node, "route": self.route}


@dataclass
class EnvKey:
    key: str
    required: bool
    needed_by: list[str]
    default: str | None = None
    description: str = ""

    # A credential. Its default is written to `.env` (gitignored) but blanked in
    # the committed `.env.example`, and it is never serialised into graph.json.
    secret: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "required": self.required,
            "needed_by": self.needed_by,
        }
        # A secret's default is a credential, so it stays out of graph.json --
        # which the package commits to git and serves from GET /graph.
        if self.default is not None and not self.secret:
            out["default"] = self.default
        if self.description:
            out["description"] = self.description
        if self.secret:
            out["secret"] = True
        return out


@dataclass
class GraphPlan:
    workflow_name: str              # human-readable, for the card and README
    workflow_slug: str              # valid Python identifier, for ADK + AGENT_NAME
    workflow_description: str
    version_id: str
    canvas_schema_version: int
    nodes: list[PlannedNode]
    edges: list[PlannedEdge]
    env_keys: list[EnvKey]
    entry_node: str
    terminal_node: str
    warnings: list[str] = field(default_factory=list)

    @property
    def by_name(self) -> dict[str, PlannedNode]:
        return {n.name: n for n in self.nodes}

    @property
    def canvas_ids(self) -> dict[str, str]:
        return {n.name: n.canvas_id for n in self.nodes}

    @property
    def join_nodes(self) -> list[str]:
        return [n.name for n in self.nodes if n.is_join]

    @property
    def join_sources(self) -> dict[str, list[str]]:
        """Join node name -> the nodes feeding it, in edge order.

        ADK hands a JoinNode a dict keyed by predecessor name, and dict order
        is arrival order — a race between the branches. A MERGE that picks one
        result, or lists them, has to be reproducible, so it orders them by
        this list instead.
        """
        sources: dict[str, list[str]] = {n.name: [] for n in self.nodes if n.is_join}
        for edge in self.edges:
            if edge.to_node in sources and edge.from_node not in sources[edge.to_node]:
                sources[edge.to_node].append(edge.from_node)
        return sources

    @property
    def unsupported(self) -> list[PlannedNode]:
        return [n for n in self.nodes if not n.supported]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "workflow": {
                "name": self.workflow_name,
                "slug": self.workflow_slug,
                "description": self.workflow_description,
                "version_id": self.version_id,
                "canvas_version": self.canvas_schema_version,
            },
            "entry_node": self.entry_node,
            "terminal_node": self.terminal_node,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "join_nodes": self.join_nodes,
            "env_keys": [k.to_dict() for k in self.env_keys],
        }


# ── Naming ───────────────────────────────────────────────────────────────────


def _slug(text: str) -> str:
    """Lowercase, underscore-separated, identifier-safe."""
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", text or "").strip("_").lower()
    return re.sub(r"_+", "_", cleaned)


def workflow_identifier(name: str) -> str:
    """A valid Python identifier for a workflow's display name.

    ADK validates `Workflow(name=...)` as a node name, so a display name like
    "Phase 3 Router" or "my-workflow" is rejected outright. The template also
    passes AGENT_NAME straight to `Workflow(name=...)`, so the same constraint
    applies to whatever goes into `.env`.
    """
    slug = _slug(name)
    if slug and slug[0].isdigit():
        slug = f"wf_{slug}"
    if not slug or not slug.isidentifier() or keyword.iskeyword(slug):
        slug = "workflow"
    return slug


def assign_names(ir: IR, graph_node_ids: list[str]) -> dict[str, str]:
    """Map canvas node ids to stable, unique Python identifiers.

    Ordinals are assigned in canvas-id order rather than graph order. Canvas ids
    look like ``node_<epoch_ms>_<counter>``, so sorting by id is chronological by
    creation: a node added later sorts last and takes the next free ordinal,
    which leaves every existing node's name untouched. Ordering by position in
    the flow would instead renumber everything downstream of an insertion and
    rewrite half the package on a one-node change.

    The entry node is named ``a2a_start`` — there is exactly one, so an ordinal
    would only add noise.
    """
    names: dict[str, str] = {}
    used: set[str] = set()

    for ordinal, canvas_id in enumerate(sorted(graph_node_ids), start=1):
        ir_node = ir.nodes[canvas_id]

        if ir_node.node_type == "A2A_START":
            base = ENTRY_NODE_NAME
        else:
            base = f"n_{_slug(ir_node.node_type)}_{ordinal}"

        if not base.isidentifier() or keyword.iskeyword(base):
            base = f"n_{ordinal}"

        # Ordinals are globally unique, so a collision means the entry name was
        # claimed twice — the validator forbids that, but stay defensive.
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1

        used.add(name)
        names[canvas_id] = name

    return names


# ── Graph reachability ───────────────────────────────────────────────────────


def _flow_successors(ir_node: IRNode) -> list[tuple[str, str | None]]:
    """Outgoing flow edges as (target_canvas_id, route).

    A branch node's handles become route values, except for PARALLEL_FORK, whose
    handles are just fan-out labels: ADK dispatches a plain edge to every
    successor, so those carry no route.
    """
    if ir_node.branches:
        fanout = ir_node.node_type in _FANOUT_TYPES
        return [
            (target, None if fanout else handle)
            for handle, targets in ir_node.branches.items()
            for target in targets
        ]
    return [(target, None) for target in ir_node.next]


def _reachable_from(ir: IR, entry_id: str) -> set[str]:
    """Canvas ids reachable from the entry along flow edges."""
    seen: set[str] = set()
    stack = [entry_id]
    while stack:
        current = stack.pop()
        if current in seen or current not in ir.nodes:
            continue
        seen.add(current)
        for target, _route in _flow_successors(ir.nodes[current]):
            stack.append(target)
    return seen


# ── Edge passes ──────────────────────────────────────────────────────────────


def _dedupe_edges(edges: list[PlannedEdge]) -> tuple[list[PlannedEdge], list[str]]:
    """Collapse repeated (from, to) pairs into one edge, merging route values.

    ADK rejects a graph containing two edges with the same endpoints — even when
    their routes differ — with "Duplicate edge found: from=X, to=Y". Merging the
    routes onto a single edge is the supported way to express "these branches all
    go here"; verified in tests/test_adk_contract.py.
    """
    merged: dict[tuple[str, str], PlannedEdge] = {}
    order: list[tuple[str, str]] = []
    notes: list[str] = []

    for edge in edges:
        pair = (edge.from_node, edge.to_node)
        if pair not in merged:
            merged[pair] = PlannedEdge(edge.from_node, edge.to_node, edge.route)
            order.append(pair)
            continue

        existing = merged[pair]
        routes = _as_route_list(existing.route) + _as_route_list(edge.route)
        # Preserve first-seen order while dropping repeats.
        unique = list(dict.fromkeys(routes))

        if not unique:
            # Both edges were plain: a genuine duplicate, nothing to merge.
            notes.append(
                f"duplicate plain edge {edge.from_node} -> {edge.to_node} collapsed into one"
            )
            continue

        existing.route = unique[0] if len(unique) == 1 else unique
        notes.append(
            f"edges {edge.from_node} -> {edge.to_node} merged into one edge "
            f"carrying route(s) {unique}"
        )

    return [merged[pair] for pair in order], notes


def _as_route_list(route: str | list[str] | None) -> list[str]:
    if route is None:
        return []
    return list(route) if isinstance(route, list) else [route]


def _find_terminal(
    plan_nodes: list[PlannedNode], edges: list[PlannedEdge], canvas_ids: dict[str, str]
) -> str:
    """The single node with no outgoing edge.

    ADK allows at most one terminal output and raises during finalisation
    otherwise — after the graph has already done its work — so this is checked
    here and reported against canvas nodes the user can find.
    """
    with_outgoing = {edge.from_node for edge in edges}
    terminals = [n.name for n in plan_nodes if n.name not in with_outgoing]

    if not terminals:
        raise GraphPlanError(
            "The workflow graph has no terminal node: every path loops back. "
            "Route one path to an END node."
        )
    if len(terminals) > 1:
        listed = ", ".join(f"'{canvas_ids[name]}'" for name in sorted(terminals))
        raise GraphPlanError(
            f"The workflow graph has {len(terminals)} terminal nodes ({listed}), "
            "but ADK allows only one. Route every path to a single END node, "
            "using a MERGE node to rejoin parallel branches."
        )
    return terminals[0]


# ── Environment keys ─────────────────────────────────────────────────────────


def _env_var_name(prefix: str, label: str, suffix: str, taken: set[str]) -> str:
    """A unique, shell-safe env var name derived from a node's label."""
    base = _slug(label).upper() or "DEFAULT"
    name = f"{prefix}_{base}_{suffix}"
    counter = 2
    while name in taken:
        name = f"{prefix}_{base}_{counter}_{suffix}"
        counter += 1
    taken.add(name)
    return name


_ALWAYS: list[EnvKey] = [
    EnvKey("PORT", False, ["serving"], "8080", "Port the agent listens on."),
    EnvKey("AGENT_NAME", True, ["agent_card"], None, "Advertised agent name."),
    EnvKey("AGENT_DESCRIPTION", True, ["agent_card"], None, "Advertised description."),
    EnvKey("AGENT_VERSION", True, ["agent_card"], "1.0.0", "Advertised version."),
    EnvKey(
        "CLOUD_RUN_URL",
        False,
        ["agent_card"],
        "",
        "Public base URL; becomes the agent card's url. Blank falls back to localhost.",
    ),
    EnvKey(
        "A2A_AUTH_TOKEN",
        False,
        ["inbound_auth"],
        "",
        "When set, callers must send this as a bearer token and the agent card "
        "advertises that it is required.",
        secret=True,
    ),
    EnvKey(
        "TASK_STORE_DSN",
        False,
        ["task_persistence"],
        "",
        "SQLAlchemy async DSN. Blank uses an in-memory store, so tasks/get only "
        "works within one process and in-flight tasks are lost on restart.",
        secret=True,
    ),
]

_LLM_NODE_TYPES = {"ORCHESTRATOR_AGENT", "AGENT", "LLM_AGENT"}


def _envify_tool_entry(
    raw: dict[str, Any],
    owner: str,
    keys: list[EnvKey],
    taken: set[str],
    needs_mcp_transport: list[str],
) -> dict[str, Any]:
    """Move one tool entry's credentials to env vars, returning the safe entry.

    Each tool gets its own variables, so an agent with several MCP servers gets
    one URL variable per server rather than them sharing one.
    """
    entry = dict(raw)
    kind = entry.get("kind")
    label = entry.get("name") or entry.get("node_id") or kind or "tool"

    if kind == "group":
        entry["children"] = [
            _envify_tool_entry(child, owner, keys, taken, needs_mcp_transport)
            for child in entry.get("children") or []
        ]
        return entry

    if kind == "agent":
        # A sub-agent holds no credential itself, but it is a tool consumer, so
        # anything wired into it does.
        entry["tools"] = _envify_tools(
            entry.get("tools") or {}, owner, keys, taken, needs_mcp_transport
        )
        return entry

    if kind == "mcp":
        var = _env_var_name("MCP", label, "URL", taken)
        keys.append(
            EnvKey(
                var, True, [owner], str(entry.pop("url", "") or ""),
                f"MCP server URL for '{label}'.", secret=True,
            )
        )
        token_var = _env_var_name("MCP", label, "TOKEN", taken)
        keys.append(
            EnvKey(
                token_var, False, [owner], str(entry.pop("auth_token", "") or ""),
                f"Bearer token for '{label}', if it needs one.", secret=True,
            )
        )
        entry["url_env"] = var
        entry["auth_token_env"] = token_var
        entry.pop("auth", None)
        needs_mcp_transport.append(owner)
        return entry

    if kind == "a2a":
        var = _env_var_name("A2A", label, "URL", taken)
        keys.append(
            EnvKey(
                var, True, [owner], str(entry.pop("endpoint", "") or ""),
                f"A2A endpoint for '{label}'.", secret=True,
            )
        )
        token_var = _env_var_name("A2A", label, "TOKEN", taken)
        keys.append(
            EnvKey(
                token_var, False, [owner], str(entry.pop("auth_token", "") or ""),
                f"Bearer token for '{label}', if it needs one.", secret=True,
            )
        )
        entry["endpoint_env"] = var
        entry["auth_token_env"] = token_var
        return entry

    # A function carries code, not credentials.
    return entry


def _envify_tools(
    resolved: dict[str, Any],
    owner: str,
    keys: list[EnvKey],
    taken: set[str],
    needs_mcp_transport: list[str],
) -> dict[str, Any]:
    """Every tool a consumer offers, with credentials moved to env vars."""
    out = dict(resolved)
    default_kind = {
        "mcp_servers": "mcp",
        "a2a_agents": "a2a",
        "functions": "function",
        "groups": "group",
        "agents": "agent",
    }
    for bucket, kind in default_kind.items():
        out[bucket] = [
            _envify_tool_entry(
                {"kind": kind, **entry}, owner, keys, taken, needs_mcp_transport
            )
            for entry in resolved.get(bucket) or []
        ]
    return out


def _collect_env_keys(
    ir: IR, plan_nodes: list[PlannedNode]
) -> tuple[list[EnvKey], dict[str, dict[str, Any]]]:
    """Env keys this node set needs, and the sanitised config for each node.

    Every credential on the canvas becomes an environment variable: the value
    travels as that variable's default (written to the gitignored `.env`) and is
    stripped from the config the plan carries, so it reaches neither the
    generated code nor `graph.json`.

    Returns `(keys, configs)` where `configs[node_name]` replaces that node's
    config -- secrets removed, env var names added under `_env` and, for an
    agent's tools, on each tool entry.
    """
    keys: list[EnvKey] = list(_ALWAYS)
    taken: set[str] = {k.key for k in keys}
    configs: dict[str, dict[str, Any]] = {}

    llm_nodes = [n.name for n in plan_nodes if n.node_type in _LLM_NODE_TYPES]
    if llm_nodes:
        keys.extend(
            [
                EnvKey("GOOGLE_CLOUD_PROJECT", True, llm_nodes, "", "GCP project for Vertex AI."),
                EnvKey("VERTEX_AI_LOCATION", False, llm_nodes, "us-central1", "Vertex AI region."),
                EnvKey("LLM_MODEL", False, llm_nodes, "gemini-2.5-flash", "Default model."),
                EnvKey(
                    "GOOGLE_GENAI_USE_VERTEXAI",
                    False,
                    llm_nodes,
                    "TRUE",
                    "TRUE routes through Vertex AI; unset uses an AI Studio key.",
                ),
                EnvKey(
                    "GOOGLE_SERVICE_ACCOUNT_JSON",
                    False,
                    llm_nodes,
                    "",
                    "Service account key as one line. Blank uses Application "
                    "Default Credentials, which is the usual choice on Cloud Run.",
                    secret=True,
                ),
                EnvKey(
                    "GOOGLE_API_KEY",
                    False,
                    llm_nodes,
                    "",
                    "AI Studio key, as an alternative to Vertex AI for local development.",
                    secret=True,
                ),
            ]
        )
        taken.update(k.key for k in keys)

    needs_mcp_transport: list[str] = []

    for node in plan_nodes:
        definition = get_node_definition(node.node_type)
        config = dict(node.config or {})
        label = node.title or node.canvas_id

        # ── Credentials the canvas set directly on an LLM node ───────────────
        # Both have a well-known platform-wide variable, so they map onto it
        # rather than getting a per-node one.
        for config_key, env_key in (
            ("api_key", "GOOGLE_API_KEY"),
            ("service_account_json", "GOOGLE_SERVICE_ACCOUNT_JSON"),
        ):
            if node.node_type in _LLM_NODE_TYPES and config.get(config_key):
                for entry in keys:
                    if entry.key == env_key and not entry.default:
                        entry.default = str(config[config_key])

        # ── A tool or remote agent sitting in the flow ───────────────────────
        if node.node_type in ("TOOL", "DATASOURCE", "MCP_TOOL"):
            var = _env_var_name("MCP", label, "URL", taken)
            keys.append(
                EnvKey(
                    var, True, [node.name], str(config.get("mcp_url") or ""),
                    f"MCP server URL for '{label}'.", secret=True,
                )
            )
            token_var = _env_var_name("MCP", label, "TOKEN", taken)
            keys.append(
                EnvKey(
                    token_var, False, [node.name], str(config.get("auth_token") or ""),
                    f"Bearer token for '{label}', if it needs one.", secret=True,
                )
            )
            config["_env"] = {"mcp_url": var, "auth_token": token_var}
            needs_mcp_transport.append(node.name)

        elif node.node_type == "REMOTE_AGENT":
            var = _env_var_name("A2A", label, "URL", taken)
            keys.append(
                EnvKey(
                    var, True, [node.name], str(config.get("endpoint") or ""),
                    f"A2A endpoint for '{label}'.", secret=True,
                )
            )
            token_var = _env_var_name("A2A", label, "TOKEN", taken)
            keys.append(
                EnvKey(
                    token_var, False, [node.name], str(config.get("auth_token") or ""),
                    f"Bearer token for '{label}', if it needs one.", secret=True,
                )
            )
            config["_env"] = {"endpoint": var, "auth_token": token_var}

        # ── Tools wired into a consumer's "tools" handle ─────────────────────
        # Recursive, because a tool group's children are tools too and their
        # credentials must leave the plan by the same route.
        resolved = config.get("resolved_tools")
        if isinstance(resolved, dict):
            config["resolved_tools"] = _envify_tools(
                resolved, node.name, keys, taken, needs_mcp_transport
            )

        # ── Strip every declared secret ─────────────────────────────────────
        if definition:
            for secret_key in definition.secret_config_keys:
                config.pop(secret_key, None)

        configs[node.name] = config

    if needs_mcp_transport:
        keys.append(
            EnvKey(
                "MCP_TRANSPORT",
                False,
                sorted(set(needs_mcp_transport)),
                "streamable_http",
                "MCP transport for every configured server.",
            )
        )

    return keys, configs


# ── Entry point ──────────────────────────────────────────────────────────────


def build_graph_plan(
    ir: IR,
    *,
    workflow_name: str,
    workflow_description: str = "",
    canvas_schema_version: int = 2,
) -> GraphPlan:
    """Compile a validated IR into an ADK graph plan.

    Raises GraphPlanError when the IR cannot become a legal ADK graph. The
    semantic validator catches these cases first and reports them per canvas
    node; this is the backstop for anything that slips through, and it also
    guards the packaging API against an IR compiled by an older release.
    """
    if not ir.entrypoints:
        raise GraphPlanError(
            "The workflow has no A2A_START node, so the graph has no entry point."
        )
    if len(ir.entrypoints) > 1:
        listed = ", ".join(f"'{e}'" for e in sorted(ir.entrypoints))
        raise GraphPlanError(
            f"The workflow has {len(ir.entrypoints)} entry nodes ({listed}); "
            "an ADK graph takes exactly one."
        )

    entry_id = ir.entrypoints[0]
    warnings: list[str] = []

    # Only nodes reachable along flow edges become graph nodes. That excludes
    # tool providers wired solely to an orchestrator's "tools" handle — they
    # become ADK tools, not nodes — and any node the canvas left unconnected.
    reachable = _reachable_from(ir, entry_id)
    graph_node_ids = [nid for nid in ir.nodes if nid in reachable]

    excluded = [
        nid for nid in ir.nodes
        if nid not in reachable and ir.nodes[nid].node_type not in _TOOL_PROVIDER_TYPES
    ]
    for nid in sorted(excluded):
        warnings.append(
            f"canvas node '{nid}' ({ir.nodes[nid].node_type}) is unreachable from "
            "the entry node and is not in the generated graph"
        )

    names = assign_names(ir, graph_node_ids)

    # ── Nodes ────────────────────────────────────────────────────────────────
    plan_nodes: list[PlannedNode] = []
    for canvas_id in sorted(graph_node_ids, key=lambda nid: names[nid]):
        ir_node = ir.nodes[canvas_id]
        defn = get_node_definition(ir_node.node_type)
        name = names[canvas_id]
        policies = ir_node.policies or {}

        routes: list[str] = []
        default_route: str | None = None
        if ir_node.branches and ir_node.node_type not in _FANOUT_TYPES:
            routes = list(ir_node.branches)
            if "default" in routes:
                default_route = "default"

        supported = ir_node.node_type not in _UNSUPPORTED_TYPES
        if not supported:
            warnings.append(
                f"canvas node '{canvas_id}' ({ir_node.node_type}) is not yet "
                "generated; the package will not run until it is replaced"
            )

        plan_nodes.append(
            PlannedNode(
                name=name,
                canvas_id=canvas_id,
                node_type=ir_node.node_type,
                module=f"nodes.{name}",
                title=(ir_node.metadata or {}).get("title") or "",
                description=(ir_node.metadata or {}).get("description") or "",
                config=ir_node.config or {},
                timeout=policies.get("timeout_seconds"),
                retries=policies.get("retry_max_attempts", 1) or 1,
                on_error=policies.get("on_error", "fail"),
                routes=routes,
                default_route=default_route,
                is_entry=canvas_id == entry_id,
                is_terminal=bool(defn and defn.is_terminal),
                is_join=ir_node.node_type == "MERGE",
                supported=supported,
            )
        )

    # ── Edges ────────────────────────────────────────────────────────────────
    raw_edges: list[PlannedEdge] = [PlannedEdge("START", names[entry_id], None)]
    for canvas_id in sorted(graph_node_ids, key=lambda nid: names[nid]):
        for target, route in _flow_successors(ir.nodes[canvas_id]):
            if target not in names:
                # The validator rejects a dangling edge, so this only fires for
                # an IR built by an older release.
                raise GraphPlanError(
                    f"canvas node '{canvas_id}' has an edge to '{target}', which "
                    "is not part of the reachable graph"
                )
            raw_edges.append(PlannedEdge(names[canvas_id], names[target], route))

    edges, dedupe_notes = _dedupe_edges(raw_edges)
    warnings.extend(dedupe_notes)

    canvas_ids = {n.name: n.canvas_id for n in plan_nodes}
    terminal = _find_terminal(plan_nodes, edges, canvas_ids)

    # A terminal node that is not an END emits no content, so the A2A task would
    # never reach `completed`. Worth flagging loudly rather than shipping.
    terminal_node = next(n for n in plan_nodes if n.name == terminal)
    if terminal_node.node_type != "END":
        warnings.append(
            f"canvas node '{terminal_node.canvas_id}' ({terminal_node.node_type}) "
            "is the graph's terminal node but is not an END node; it must emit "
            "content or the A2A task will never complete"
        )

    # Sanitising here means graph.json, the rendered modules and GET /graph are
    # all free of credentials by construction, rather than each having to
    # remember to redact.
    env_keys, sanitised = _collect_env_keys(ir, plan_nodes)
    for node in plan_nodes:
        if node.name in sanitised:
            node.config = sanitised[node.name]

    slug = workflow_identifier(workflow_name)
    for key in env_keys:
        if key.key == "AGENT_NAME":
            key.default = slug

    return GraphPlan(
        workflow_name=workflow_name,
        workflow_slug=slug,
        workflow_description=workflow_description,
        version_id=ir.workflow_version_id,
        canvas_schema_version=canvas_schema_version,
        nodes=plan_nodes,
        edges=edges,
        env_keys=env_keys,
        entry_node=names[entry_id],
        terminal_node=terminal,
        warnings=warnings,
    )
