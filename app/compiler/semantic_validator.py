"""Layer 2: semantic graph validation.

Several rules here exist to catch, at save time, things that ADK would otherwise
reject when the graph is built or — worse — halfway through a run.  Each such
rule names the ADK constraint it stands in for, because the failure it prevents
is not obvious from the canvas:

  * exactly one entry and exactly one terminal node
    ADK: "Workflow X: multiple terminal nodes produced output (2). A workflow
    must have at most one terminal output." Raised during finalisation, i.e.
    after the work is done.
  * every fan-out reconverges
    Same rule: a fork whose branches each end somewhere different produces
    several terminal outputs.
  * no two edges share a (source, target) pair with different handles
    ADK: "Graph validation failed. Duplicate edge found: from=X, to=Y."  The
    compiler merges these into one Edge with a route list, so this is a warning
    rather than an error.
"""

from typing import Any

import networkx as nx

from app.compiler.connection_rules import can_connect as connection_refusal
from app.compiler.findings import Findings
from app.compiler.inputs import available_inputs, input_is_opaque
from app.nodes.agent_io import structure_errors
from app.nodes.variables import name_error as variable_name_error
from app.nodes.registry import (
    RETIRED_TRIGGER_TYPES,
    AGENT_TYPES,
    BRANCH_NODE_TYPES,
    SUBAGENT_TYPES,
    TOOL_CONSUMER_TYPES,
    TOOL_GROUP_TYPES,
    TOOL_PROVIDER_TYPES,
    get_node_definition,
)
from app.schemas.canvas import CanvasPayload

# Nodes whose outbound edges are named branches rather than plain flow.
_BRANCH_NODES = BRANCH_NODE_TYPES



def _rotate_cycle(cycle: list[str]) -> list[str]:
    """Start a cycle at its lowest node id.

    `nx.simple_cycles` is free to return the same cycle starting anywhere in
    it, and does: the identical canvas produced "agent -> shape -> agent" on
    one run and "shape -> agent -> shape" on the next, depending only on what
    else had been imported. A message that changes between runs is confusing
    to read and impossible to pin down in a test.
    """
    if not cycle:
        return cycle
    start = cycle.index(min(cycle))
    return cycle[start:] + cycle[:start]


def _check_mapping(
    canvas: Any,
    node: Any,
    fields: list[dict],
    what: str,
    out: Findings,
) -> None:
    """Check a canvas field mapping against what is actually readable here.

    Shared by a TRANSFORM's output fields and an MCP_TOOL's arguments: both
    describe "build this from what is available", both are offered from the
    same picker, and both are read by the same `core/mapping.py` at run time.
    One implementation means the checks cannot drift apart from each other or
    from what the generated code does.
    """
    known = {f.path: f for f in available_inputs(canvas, node.id)}
    opaque = input_is_opaque(canvas, node.id)
    seen_names: set[str] = set()

    for index, field in enumerate(fields):
        name = (field.get("name") or "").strip()
        label = name or f"{what} {index + 1}"
        if not name:
            out.error(f"Node '{node.id}' ({node.type}) has an {what} with no name.", node=node)
        elif name in seen_names:
            out.error(f"Node '{node.id}' ({node.type}) builds '{name}' twice.", node=node)
        seen_names.add(name)

        source = (field.get("source") or "").strip()
        if not source:
            if "default" in field and field["default"] is not None:
                continue  # a constant, not a mapping
            out.error(
                f"Node '{node.id}' ({node.type}): '{label}' has no source. "
                "Pick where its value comes from, or give it a default.", node=node)
            continue

        match = known.get(source) or known.get(f"data.{source}")
        if match is None:
            # `vars.x.y.z` is fine when `vars.x` is known but its shape is not
            # -- the lookup just goes deeper at runtime.
            prefix_known = any(source.startswith(f"{path}.") for path in known)
            if prefix_known or opaque:
                out.warn(
                    f"Node '{node.id}' ({node.type}): '{label}' reads "
                    f"'{source}', which the canvas cannot confirm is present. "
                    "It will be skipped at runtime if absent.", node=node)
            else:
                available = ", ".join(sorted(known)[:6]) or "nothing declared upstream"
                out.error(
                    f"Node '{node.id}' ({node.type}): '{label}' reads "
                    f"'{source}', which nothing upstream produces. "
                    f"Available here: {available}.", node=node)
            continue

        wanted = field.get("type") or "string"
        if wanted != match.type and match.type != "any" and wanted != "any":
            # Compatible enough to convert, so this is a note not a block.
            out.warn(
                f"Node '{node.id}' ({node.type}): '{label}' is declared "
                f"{wanted} but '{source}' is {match.type}. It will be "
                "converted, or passed through unchanged if it cannot be.", node=node)


def _check_mcp_tool(canvas: Any, node: Any, out: Findings) -> None:
    """A direct MCP tool call: reachable server, named tool, arguments that fit.

    The tool's own input schema is cached on the node when the config panel
    fetches the server's tools, so the arguments can be checked here without
    the compiler making a network call during a build. A node whose schema was
    never fetched still compiles -- the checks simply have less to say.
    """
    config = node.config or {}

    if not (config.get("mcp_url") or "").strip():
        out.error(
            f"Node '{node.id}' (MCP_TOOL) has no server URL. Enter one and "
            "fetch the server's tools.", node=node)
    if not (config.get("tool_name") or "").strip():
        out.error(
            f"Node '{node.id}' (MCP_TOOL) names no tool. Fetch the server's "
            "tools and pick one.", node=node)

    schema = config.get("tool_schema") or {}
    declared = {(p or "").strip() for p in (schema.get("properties") or {})}
    required = {(r or "").strip() for r in (schema.get("required") or [])}

    if (config.get("arg_mode") or "fields") != "fields":
        # Passthrough sends the whole payload, which carries keys from every
        # earlier node -- fine for a permissive server, a schema error on a
        # strict one.
        if declared:
            out.warn(
                f"Node '{node.id}' (MCP_TOOL) sends the whole payload as "
                f"arguments, but '{config.get('tool_name')}' declares "
                f"{len(declared)}. A server that validates its input strictly "
                "will reject the extra keys; map the arguments instead.", node=node)
        return

    fields = config.get("arg_fields") or []
    _check_mapping(canvas, node, fields, "argument", out)

    mapped = {(f.get("name") or "").strip() for f in fields}
    mapped.discard("")

    for missing in sorted(required - mapped):
        out.error(
            f"Node '{node.id}' (MCP_TOOL): '{config.get('tool_name')}' requires "
            f"the argument '{missing}', which is not mapped.", node=node)

    for extra in sorted(mapped - declared) if declared else []:
        out.warn(
            f"Node '{node.id}' (MCP_TOOL) sends '{extra}', which "
            f"'{config.get('tool_name')}' does not declare. Re-fetch the "
            "server's tools if it has changed.", node=node)


def validate_semantics(canvas: CanvasPayload) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors block compilation; warnings do not.

    Unchanged on purpose: eleven test files assert on substrings of these
    strings and the compile log shows them verbatim. `collect_findings` is the
    same work with the node or edge each result concerns attached, for a canvas
    that wants to mark the thing rather than print a list.
    """
    out = collect_findings(canvas)
    return out.as_strings({n.id: n.type for n in canvas.nodes})


def collect_findings(canvas: CanvasPayload) -> Findings:
    """Every validation result, each anchored to what it is about."""
    out = Findings()
    nodes = {n.id: n for n in canvas.nodes}
    node_ids = set(nodes)

    graph = nx.DiGraph()
    for node in canvas.nodes:
        graph.add_node(node.id, data=node)

    # ── Edge endpoints must exist ────────────────────────────────────────────
    for edge in canvas.edges:
        if edge.source not in node_ids:
            out.error(
                f"Edge '{edge.id}': source node '{edge.source}' does not exist", edge=edge)
        if edge.target not in node_ids:
            out.error(
                f"Edge '{edge.id}': target node '{edge.target}' does not exist", edge=edge)
    if out.has_errors:
        # Every rule below reads the graph; bail out rather than report noise.
        return out

    # ── An edge must leave by a handle the node actually has ─────────────────
    # Nothing checked this before: `output_handles` was never read anywhere in
    # the compiler. So an edge leaving a CONDITION by a branch nobody named --
    # the usual cause being a branch renamed after the edge was drawn -- passed
    # validation and then simply never fired at run time.
    for edge in canvas.edges:
        source = nodes[edge.source]
        refusal = connection_refusal(
            source.type,
            edge.source_handle,
            nodes[edge.target].type,
            edge.target_handle,
            source_config=source.config,
        )
        if refusal is not None and refusal.code == "edge.unknown_handle":
            out.error(f"Node '{edge.source}' ({source.type}): {refusal.message}", node=edge.source)

    # Tool-provision edges are not flow: they hang a provider off a consumer's
    # "tools" handle. Keeping them out of `graph` is what lets the flow rules
    # below judge a node by where it actually sits.
    tool_edges = [e for e in canvas.edges if e.target_handle == "tools"]
    flow_edges = [e for e in canvas.edges if e.target_handle != "tools"]

    for edge in flow_edges:
        graph.add_edge(edge.source, edge.target, data=edge)

    # Nodes that exist only to be called as a tool. Exempting these by wiring
    # rather than by node type matters now that LLM_AGENT can be either: the
    # same type is a flow node here and a sub-agent there.
    _in_flow = {e.source for e in flow_edges} | {e.target for e in flow_edges}
    tool_only: set[str] = {e.source for e in tool_edges} - _in_flow

    # ── Retired trigger types ────────────────────────────────────────────────
    for node in canvas.nodes:
        if node.type in RETIRED_TRIGGER_TYPES:
            out.error(
                f"Node '{node.id}' uses '{node.type}', which no longer exists. "
                "Workflows now start from a single A2A_START node. Re-open and "
                "re-save this workflow to migrate it automatically.", node=node)
    if out.has_errors:
        return out

    # ── Exactly one entry node ───────────────────────────────────────────────
    entry_nodes = [
        n for n in canvas.nodes
        if (defn := get_node_definition(n.type)) and defn.is_trigger
    ]
    if not entry_nodes:
        out.error("Workflow must have exactly one A2A_START node (found none)")
    elif len(entry_nodes) > 1:
        listed = ", ".join(f"'{n.id}'" for n in entry_nodes)
        out.error(
            f"Workflow must have exactly one A2A_START node (found {len(entry_nodes)}: {listed})"
        )

    # ── Entry nodes take no inbound edges; terminals take no outbound ────────
    for node in canvas.nodes:
        defn = get_node_definition(node.type)
        if not defn:
            continue
        if not defn.allows_inbound and graph.in_degree(node.id) > 0:
            out.error(
                f"Node '{node.id}' ({node.type}) is the workflow entry and must not have inbound edges", node=node)
        if not defn.allows_outbound and graph.out_degree(node.id) > 0:
            out.error(
                f"Node '{node.id}' ({node.type}) is a terminal node and must not have outbound edges", node=node)

    # ── Exactly one terminal node ────────────────────────────────────────────
    # ADK requires this, and the generated END node is what makes the A2A task
    # reach `completed`. Several END nodes means several terminal outputs.
    end_nodes = [
        n for n in canvas.nodes
        if (defn := get_node_definition(n.type)) and defn.is_terminal
    ]
    if not end_nodes:
        out.error("Workflow must have exactly one END node (found none)")
    elif len(end_nodes) > 1:
        listed = ", ".join(f"'{n.id}'" for n in end_nodes)
        out.error(
            f"Workflow must have exactly one END node (found {len(end_nodes)}: {listed}). "
            "Route every path to a single END, using a MERGE node to rejoin parallel branches."
        )

    # A node in the flow with no outbound edges is a terminal ADK did not
    # expect. Tool providers are exempt: they hang off the "tools" handle.
    if end_nodes:
        for node in canvas.nodes:
            defn = get_node_definition(node.type)
            if not defn or defn.is_terminal or defn.is_trigger:
                continue
            if node.id in tool_only:
                continue
            if graph.out_degree(node.id) == 0 and graph.in_degree(node.id) > 0:
                out.error(
                    f"Node '{node.id}' ({node.type}) has no outbound edge, so it "
                    "would end the workflow alongside the END node. Connect it "
                    "onward to the END node.", node=node)

    # ── Parallel branches must reconverge ────────────────────────────────────
    for node in canvas.nodes:
        if node.type != "PARALLEL_FORK":
            continue
        out_edges = [e for e in canvas.edges if e.source == node.id]
        if len(out_edges) < 2:
            out.error(
                f"Node '{node.id}' (PARALLEL_FORK) must have at least 2 outbound edges", node=node)
            continue
        if not _branches_reconverge(graph, node.id, [e.target for e in out_edges]):
            out.error(
                f"Node '{node.id}' (PARALLEL_FORK) has branches that never rejoin. "
                "Every branch must reach a common MERGE node, or the run fails "
                "with multiple terminal outputs.", node=node)

    # ── Duplicate (source, target) pairs ─────────────────────────────────────
    # ADK rejects two edges sharing a pair even when their handles differ, so
    # the compiler merges them into one edge carrying several route values.
    pair_handles: dict[tuple[str, str], list[str]] = {}
    for edge in canvas.edges:
        if edge.target_handle == "tools":
            continue
        pair_handles.setdefault((edge.source, edge.target), []).append(
            edge.source_handle
        )
    for (source, target), handles in pair_handles.items():
        if len(handles) > 1:
            source_node = nodes[source]
            if source_node.type in _BRANCH_NODES:
                out.warn(
                    f"Node '{source}' ({source_node.type}) has {len(handles)} edges to "
                    f"'{target}' ({', '.join(handles)}); they will be merged into a "
                    "single edge carrying all those branches.", node=source)
            else:
                out.warn(
                    f"Duplicate edge from '{source}' to '{target}' "
                    f"({len(handles)} copies); only one will be kept.", node=source)

    # ── Connectivity ─────────────────────────────────────────────────────────
    connected: set[str] = set()
    for edge in canvas.edges:
        connected.add(edge.source)
        connected.add(edge.target)
    for node in canvas.nodes:
        if (
            node.id not in connected
            and len(canvas.nodes) > 1
            and node.id not in tool_only
        ):
            out.warn(
                f"Node '{node.id}' ({node.type}) is not connected to any edge and will be skipped", node=node)

    # Every flow node must be reachable from the entry, or it never runs.
    if len(entry_nodes) == 1:
        reachable = nx.descendants(graph, entry_nodes[0].id) | {entry_nodes[0].id}
        for node in canvas.nodes:
            if node.id in reachable or node.id in tool_only:
                continue
            if node.id not in connected:
                continue  # already reported as orphaned
            out.error(
                f"Node '{node.id}' ({node.type}) is not reachable from the "
                f"A2A_START node '{entry_nodes[0].id}' and would never run.", node=node)

    # ── Cycles only through LOOP nodes ───────────────────────────────────────
    try:
        for cycle in nx.simple_cycles(graph):
            cycle_nodes = [nodes[nid] for nid in cycle if nid in nodes]
            has_loop = any(
                (defn := get_node_definition(n.type)) and defn.allows_cycle
                for n in cycle_nodes
            )
            if not has_loop:
                out.error(
                    "Cycle detected without a LOOP node: "
                    f"{' -> '.join(_rotate_cycle(cycle))}",
                    related_node_ids=_rotate_cycle(cycle)
                )
    except Exception:  # noqa: BLE001 - cycle detection is advisory
        pass

    # ── Tool wiring ──────────────────────────────────────────────────────────
    children_of: dict[str, list[str]] = {}

    for edge in tool_edges:
        source, target = nodes[edge.source], nodes[edge.target]
        if target.type not in TOOL_CONSUMER_TYPES:
            out.error(
                f"Node '{edge.target}' ({target.type}) has no tools input, so "
                f"'{edge.source}' cannot be wired into it as a tool. Tools "
                f"connect to: {', '.join(sorted(TOOL_CONSUMER_TYPES))}.", node=edge.target)
            continue
        if source.type not in TOOL_PROVIDER_TYPES:
            out.error(
                f"Node '{edge.source}' ({source.type}) cannot be used as a tool. "
                f"Only these can: {', '.join(sorted(TOOL_PROVIDER_TYPES))}.", node=edge.source)
            continue
        children_of.setdefault(edge.target, []).append(edge.source)

    # A group is a tool, so it belongs on a tools handle. Wired into the flow it
    # would be a graph node with no behaviour, which is a silent no-op.
    for node in canvas.nodes:
        if node.type not in TOOL_GROUP_TYPES:
            continue

        flow_targets = [
            e.target for e in canvas.edges
            if e.source == node.id and e.target_handle != "tools"
        ]
        if flow_targets:
            out.error(
                f"Node '{node.id}' ({node.type}) is a tool group, so its output "
                "must connect to the tools input of an agent or another group — "
                f"not into the workflow flow (currently '{flow_targets[0]}').", node=node)

        consumers = [e.target for e in tool_edges if e.source == node.id]
        if not consumers:
            out.error(
                f"Node '{node.id}' ({node.type}) is not connected to anything. "
                "Connect its output to the tools input of an agent or another group.", node=node)

        own_children = children_of.get(node.id, [])
        if not own_children:
            out.error(
                f"Node '{node.id}' ({node.type}) has no tools connected to it. "
                "Wire at least one tool, remote agent or function into it.", node=node)
        elif len(own_children) < 2:
            out.warn(
                f"Node '{node.id}' ({node.type}) has only one tool connected, so "
                "it will behave the same as connecting that tool directly.", node=node)

        # `order` names canvas nodes; a stale id means the user removed a tool
        # and the order was left behind.
        ordered = [str(x) for x in (node.config.get("order") or [])]
        for node_id in ordered:
            if node_id not in own_children:
                out.error(
                    f"Node '{node.id}' ({node.type}) lists '{node_id}' in its "
                    "execution order, but that node is not connected to it.", node=node)
        duplicates = {x for x in ordered if ordered.count(x) > 1}
        for node_id in sorted(duplicates):
            out.error(
                f"Node '{node.id}' ({node.type}) lists '{node_id}' more than once "
                "in its execution order.", node=node)
        if node.type == "SEQUENTIAL_AGENT" and own_children and not ordered:
            out.warn(
                f"Node '{node.id}' (SEQUENTIAL_AGENT) has no execution order set, "
                "so its tools run in canvas order. Set the order to be explicit.", node=node)

    # An agent attached to a tools handle is called by another model, so what
    # the caller can see about it decides whether it is usable at all.
    for node in canvas.nodes:
        if node.type not in SUBAGENT_TYPES:
            continue
        if not [e for e in tool_edges if e.source == node.id]:
            continue  # not used as a tool; it is a flow node

        if not (node.config.get("description") or "").strip():
            out.warn(
                f"Node '{node.id}' ({node.type}) is used as a tool but has no "
                "description. The calling model uses it to decide whether to "
                "pick this agent, so without one it will rarely be called.", node=node)

        if not ((node.config.get("input_structure") or {}).get("fields") or []):
            out.warn(
                f"Node '{node.id}' ({node.type}) is used as a tool but declares "
                "no input structure, so the caller can only pass it one free-text "
                "request. Add input fields to be called with real arguments.", node=node)

    # A cycle in the tool wiring cannot be seen by the flow-graph cycle check,
    # because tool edges are excluded from that graph. Left unchecked it would
    # recurse until the compiler gave up.
    tool_graph = nx.DiGraph()
    for edge in tool_edges:
        tool_graph.add_edge(edge.source, edge.target)
    try:
        for raw_cycle in nx.simple_cycles(tool_graph):
            cycle = _rotate_cycle(raw_cycle)
            out.error(
                "Tools are wired in a loop: "
                f"{' -> '.join(cycle)} -> {cycle[0]}. A group or agent cannot "
                "contain itself.", related_node_ids=cycle
            )
    except Exception:  # noqa: BLE001 - cycle detection is advisory
        pass

    # ── Per-node config rules ────────────────────────────────────────────────
    # variable name -> the node that saves it, for collision reporting.
    variable_owners: dict[str, str] = {}

    for node in canvas.nodes:
        if node.type == "CONDITION" and not node.config.get("branches"):
            out.error(
                f"Node '{node.id}' (CONDITION) must define at least one branch", node=node)

        # A field mapping is checked against what is actually available here:
        # the whole point of declaring the shape is that a source which will
        # never resolve can be caught now rather than at 3am.
        if node.type == "TRANSFORM" and (node.config.get("mode") or "fields") == "fields":
            declared = node.config.get("output_fields") or []
            if not declared:
                out.error(
                    f"Node '{node.id}' (TRANSFORM) builds no fields. Add at "
                    "least one output field, or switch to an expression mode.", node=node)
            _check_mapping(canvas, node, declared, "output field", out)

        if node.type == "MCP_TOOL":
            _check_mcp_tool(canvas, node, out)

        # A variable name that cannot be bound would simply never appear, so
        # it is caught here rather than discovered in an expression.
        variable = (node.config.get("output_variable") or "").strip()
        if variable:
            problem = variable_name_error(variable)
            if problem:
                out.error(f"Node '{node.id}' ({node.type}): {problem}", node=node)
            elif variable in variable_owners:
                out.warn(
                    f"Node '{node.id}' ({node.type}) saves to '{variable}', "
                    f"which node '{variable_owners[variable]}' also saves to. "
                    "Whichever runs last wins.", node=node)
            else:
                variable_owners[variable] = node.id

        if node.type == "WAIT":
            from app.nodes.tasks.wait import (
                BLOCKING_COMFORT_SECONDS,
                MAX_WAIT_SECONDS,
                wait_seconds,
            )

            seconds = wait_seconds(node.config)
            if seconds <= 0:
                out.error(
                    f"Node '{node.id}' (WAIT) has no duration, so it would not "
                    "wait at all. Set how long to wait, or remove the node.", node=node)
            elif seconds > MAX_WAIT_SECONDS:
                out.error(
                    f"Node '{node.id}' (WAIT) would wait {seconds / 60:.0f} "
                    f"minutes, over the {MAX_WAIT_SECONDS // 60}-minute limit. "
                    "A wait that long belongs outside the workflow — have a "
                    "scheduler start it later instead of holding a run open.", node=node)
            elif seconds > BLOCKING_COMFORT_SECONDS:
                out.warn(
                    f"Node '{node.id}' (WAIT) waits {seconds:.0f}s. A blocking "
                    "caller holds its connection open for the whole time, so "
                    "invoke this workflow in task mode and poll for the result.", node=node)

        # An input request with no fields parks the run to ask for nothing,
        # which can only ever be a mistake.
        if node.type == "HUMAN_INPUT":
            fields = (node.config.get("collect_fields") or {}).get("fields") or []
            named = [f for f in fields if (f.get("name") or "").strip()]
            if not named:
                out.error(
                    f"Node '{node.id}' (HUMAN_INPUT) collects no fields, so it "
                    "would pause the run to ask for nothing. Add at least one "
                    "field to collect.", node=node)
            if not (node.config.get("prompt") or "").strip():
                out.warn(
                    f"Node '{node.id}' (HUMAN_INPUT) has no prompt. The paused "
                    "task will not say why the workflow needs these values.", node=node)

        # An approval gate with only one branch wired silently drops the other
        # decision: the run would reach a dead end with no route to take.
        if node.type == "HUMAN_APPROVAL":
            handles = {
                e.source_handle
                for e in canvas.edges
                if e.source == node.id and e.target_handle != "tools"
            }
            for handle in ("approved", "rejected"):
                if handle not in handles:
                    out.error(
                        f"Node '{node.id}' (HUMAN_APPROVAL) has nothing on its "
                        f"'{handle}' output, so a {handle} decision would have "
                        "nowhere to go. Connect both outputs.", node=node)
            if not (node.config.get("prompt") or "").strip():
                out.warn(
                    f"Node '{node.id}' (HUMAN_APPROVAL) has no prompt. The "
                    "paused task will just say 'Approve this step?', which "
                    "tells the approver nothing about what they are approving.", node=node)

        # A LOOP with no mode-specific config is the quietest possible failure:
        # it compiles, packages, runs, and iterates zero times.
        if node.type == "LOOP":
            mode = node.config.get("mode") or "for_each"
            if mode == "for_each" and not (node.config.get("items_path") or "").strip():
                out.error(
                    f"Node '{node.id}' (LOOP) is in for_each mode but has no "
                    "items path, so it would never iterate. Set the path to the "
                    "list, e.g. 'data.items'.", node=node)
            if mode == "while" and not (node.config.get("exit_condition") or "").strip():
                out.error(
                    f"Node '{node.id}' (LOOP) is in while mode but has no exit "
                    "condition, so it would only stop at max_iterations. Set a "
                    "condition, e.g. 'i >= 5'.", node=node)

            # The loop needs both handles wired or it is not a loop.
            handles = {
                e.source_handle
                for e in canvas.edges
                if e.source == node.id and e.target_handle != "tools"
            }
            for handle, purpose in (
                ("loop_body", "the body to repeat"),
                ("done", "where to go when the loop finishes"),
            ):
                if handle not in handles:
                    out.error(
                        f"Node '{node.id}' (LOOP) has nothing on its "
                        f"'{handle}' output, which is {purpose}.", node=node)

        # Structure field names become pydantic fields and tool parameters, so
        # they have to be usable as Python identifiers and unique.
        if node.type in AGENT_TYPES:
            for key, label in (
                ("input_structure", "input structure"),
                ("output_structure", "output structure"),
            ):
                out.extend_errors(
                    f"Node '{node.id}' ({node.type}): {message}"
                    for message in structure_errors(node.config.get(key), label)
                )

            # ADK only consults `input_schema` when the agent is called as a
            # tool, so on a flow-only agent the setting has no effect.
            has_input_fields = bool(
                (node.config.get("input_structure") or {}).get("fields") or []
            )
            used_as_tool = any(e.source == node.id for e in tool_edges)
            if has_input_fields and not used_as_tool:
                out.warn(
                    f"Node '{node.id}' ({node.type}) declares an input structure, "
                    "but it only applies when the agent is called as a tool by "
                    "another agent. Here it runs in the flow, so the structure is "
                    "ignored; its input comes from the previous node.", node=node)

        # `on_error: continue` only means something where the generated module
        # emits an error payload instead of raising; elsewhere it would be
        # silently ignored, which is worse than being told.
        if node.policies.on_error == "continue":
            defn = get_node_definition(node.type)
            if defn and not defn.supports_on_error_continue:
                out.warn(
                    f"Node '{node.id}' ({node.type}) is set to continue on error, "
                    "but that only applies to nodes that call out of the workflow "
                    "(models, tools, remote agents, functions and transforms). "
                    "This node will fail the run instead.", node=node)

        if node.type == "A2A_START":
            fields = (node.config.get("payload_schema") or {}).get("fields") or []
            names = [(f.get("name") or "").strip() for f in fields]
            for index, name in enumerate(names):
                if not name:
                    out.error(
                        f"Node '{node.id}' (A2A_START) payload field {index + 1} has no name", node=node)
            duplicates = {n for n in names if n and names.count(n) > 1}
            for name in sorted(duplicates):
                out.error(
                    f"Node '{node.id}' (A2A_START) has more than one payload field named '{name}'", node=node)
            if not fields:
                out.warn(
                    f"Node '{node.id}' (A2A_START) declares no payload fields; "
                    "callers get no documented input contract and the test panel "
                    "falls back to a raw JSON editor.", node=node)

    return out


def _branches_reconverge(graph: nx.DiGraph, fork_id: str, branch_starts: list[str]) -> bool:
    """True when every branch out of a fork can reach one common MERGE node."""
    merge_candidates: set[str] | None = None
    for start in branch_starts:
        reachable = nx.descendants(graph, start) | {start}
        merges = {
            nid for nid in reachable
            if graph.nodes[nid]["data"].type == "MERGE"
        }
        merge_candidates = merges if merge_candidates is None else merge_candidates & merges
        if not merge_candidates:
            return False
    return bool(merge_candidates)
