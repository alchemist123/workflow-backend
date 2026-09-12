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

import networkx as nx

from app.nodes.agent_io import structure_errors
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


def validate_semantics(canvas: CanvasPayload) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors block compilation; warnings do not."""
    errors: list[str] = []
    warnings: list[str] = []
    nodes = {n.id: n for n in canvas.nodes}
    node_ids = set(nodes)

    graph = nx.DiGraph()
    for node in canvas.nodes:
        graph.add_node(node.id, data=node)

    # ── Edge endpoints must exist ────────────────────────────────────────────
    for edge in canvas.edges:
        if edge.source not in node_ids:
            errors.append(
                f"Edge '{edge.id}': source node '{edge.source}' does not exist"
            )
        if edge.target not in node_ids:
            errors.append(
                f"Edge '{edge.id}': target node '{edge.target}' does not exist"
            )
    if errors:
        # Every rule below reads the graph; bail out rather than report noise.
        return errors, warnings

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
            errors.append(
                f"Node '{node.id}' uses '{node.type}', which no longer exists. "
                "Workflows now start from a single A2A_START node. Re-open and "
                "re-save this workflow to migrate it automatically."
            )
    if errors:
        return errors, warnings

    # ── Exactly one entry node ───────────────────────────────────────────────
    entry_nodes = [
        n for n in canvas.nodes
        if (defn := get_node_definition(n.type)) and defn.is_trigger
    ]
    if not entry_nodes:
        errors.append("Workflow must have exactly one A2A_START node (found none)")
    elif len(entry_nodes) > 1:
        listed = ", ".join(f"'{n.id}'" for n in entry_nodes)
        errors.append(
            f"Workflow must have exactly one A2A_START node (found {len(entry_nodes)}: {listed})"
        )

    # ── Entry nodes take no inbound edges; terminals take no outbound ────────
    for node in canvas.nodes:
        defn = get_node_definition(node.type)
        if not defn:
            continue
        if not defn.allows_inbound and graph.in_degree(node.id) > 0:
            errors.append(
                f"Node '{node.id}' ({node.type}) is the workflow entry and must not have inbound edges"
            )
        if not defn.allows_outbound and graph.out_degree(node.id) > 0:
            errors.append(
                f"Node '{node.id}' ({node.type}) is a terminal node and must not have outbound edges"
            )

    # ── Exactly one terminal node ────────────────────────────────────────────
    # ADK requires this, and the generated END node is what makes the A2A task
    # reach `completed`. Several END nodes means several terminal outputs.
    end_nodes = [
        n for n in canvas.nodes
        if (defn := get_node_definition(n.type)) and defn.is_terminal
    ]
    if not end_nodes:
        errors.append("Workflow must have exactly one END node (found none)")
    elif len(end_nodes) > 1:
        listed = ", ".join(f"'{n.id}'" for n in end_nodes)
        errors.append(
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
                errors.append(
                    f"Node '{node.id}' ({node.type}) has no outbound edge, so it "
                    "would end the workflow alongside the END node. Connect it "
                    "onward to the END node."
                )

    # ── Parallel branches must reconverge ────────────────────────────────────
    for node in canvas.nodes:
        if node.type != "PARALLEL_FORK":
            continue
        out_edges = [e for e in canvas.edges if e.source == node.id]
        if len(out_edges) < 2:
            errors.append(
                f"Node '{node.id}' (PARALLEL_FORK) must have at least 2 outbound edges"
            )
            continue
        if not _branches_reconverge(graph, node.id, [e.target for e in out_edges]):
            errors.append(
                f"Node '{node.id}' (PARALLEL_FORK) has branches that never rejoin. "
                "Every branch must reach a common MERGE node, or the run fails "
                "with multiple terminal outputs."
            )

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
                warnings.append(
                    f"Node '{source}' ({source_node.type}) has {len(handles)} edges to "
                    f"'{target}' ({', '.join(handles)}); they will be merged into a "
                    "single edge carrying all those branches."
                )
            else:
                warnings.append(
                    f"Duplicate edge from '{source}' to '{target}' "
                    f"({len(handles)} copies); only one will be kept."
                )

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
            warnings.append(
                f"Node '{node.id}' ({node.type}) is not connected to any edge and will be skipped"
            )

    # Every flow node must be reachable from the entry, or it never runs.
    if len(entry_nodes) == 1:
        reachable = nx.descendants(graph, entry_nodes[0].id) | {entry_nodes[0].id}
        for node in canvas.nodes:
            if node.id in reachable or node.id in tool_only:
                continue
            if node.id not in connected:
                continue  # already reported as orphaned
            errors.append(
                f"Node '{node.id}' ({node.type}) is not reachable from the "
                f"A2A_START node '{entry_nodes[0].id}' and would never run."
            )

    # ── Cycles only through LOOP nodes ───────────────────────────────────────
    try:
        for cycle in nx.simple_cycles(graph):
            cycle_nodes = [nodes[nid] for nid in cycle if nid in nodes]
            has_loop = any(
                (defn := get_node_definition(n.type)) and defn.allows_cycle
                for n in cycle_nodes
            )
            if not has_loop:
                errors.append(
                    f"Cycle detected without a LOOP node: {' -> '.join(cycle)}"
                )
    except Exception:  # noqa: BLE001 - cycle detection is advisory
        pass

    # ── Tool wiring ──────────────────────────────────────────────────────────
    children_of: dict[str, list[str]] = {}

    for edge in tool_edges:
        source, target = nodes[edge.source], nodes[edge.target]
        if target.type not in TOOL_CONSUMER_TYPES:
            errors.append(
                f"Node '{edge.target}' ({target.type}) has no tools input, so "
                f"'{edge.source}' cannot be wired into it as a tool. Tools "
                f"connect to: {', '.join(sorted(TOOL_CONSUMER_TYPES))}."
            )
            continue
        if source.type not in TOOL_PROVIDER_TYPES:
            errors.append(
                f"Node '{edge.source}' ({source.type}) cannot be used as a tool. "
                f"Only these can: {', '.join(sorted(TOOL_PROVIDER_TYPES))}."
            )
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
            errors.append(
                f"Node '{node.id}' ({node.type}) is a tool group, so its output "
                "must connect to the tools input of an agent or another group — "
                f"not into the workflow flow (currently '{flow_targets[0]}')."
            )

        consumers = [e.target for e in tool_edges if e.source == node.id]
        if not consumers:
            errors.append(
                f"Node '{node.id}' ({node.type}) is not connected to anything. "
                "Connect its output to the tools input of an agent or another group."
            )

        own_children = children_of.get(node.id, [])
        if not own_children:
            errors.append(
                f"Node '{node.id}' ({node.type}) has no tools connected to it. "
                "Wire at least one tool, remote agent or function into it."
            )
        elif len(own_children) < 2:
            warnings.append(
                f"Node '{node.id}' ({node.type}) has only one tool connected, so "
                "it will behave the same as connecting that tool directly."
            )

        # `order` names canvas nodes; a stale id means the user removed a tool
        # and the order was left behind.
        ordered = [str(x) for x in (node.config.get("order") or [])]
        for node_id in ordered:
            if node_id not in own_children:
                errors.append(
                    f"Node '{node.id}' ({node.type}) lists '{node_id}' in its "
                    "execution order, but that node is not connected to it."
                )
        duplicates = {x for x in ordered if ordered.count(x) > 1}
        for node_id in sorted(duplicates):
            errors.append(
                f"Node '{node.id}' ({node.type}) lists '{node_id}' more than once "
                "in its execution order."
            )
        if node.type == "SEQUENTIAL_AGENT" and own_children and not ordered:
            warnings.append(
                f"Node '{node.id}' (SEQUENTIAL_AGENT) has no execution order set, "
                "so its tools run in canvas order. Set the order to be explicit."
            )

    # An agent attached to a tools handle is called by another model, so what
    # the caller can see about it decides whether it is usable at all.
    for node in canvas.nodes:
        if node.type not in SUBAGENT_TYPES:
            continue
        if not [e for e in tool_edges if e.source == node.id]:
            continue  # not used as a tool; it is a flow node

        if not (node.config.get("description") or "").strip():
            warnings.append(
                f"Node '{node.id}' ({node.type}) is used as a tool but has no "
                "description. The calling model uses it to decide whether to "
                "pick this agent, so without one it will rarely be called."
            )

        if not ((node.config.get("input_structure") or {}).get("fields") or []):
            warnings.append(
                f"Node '{node.id}' ({node.type}) is used as a tool but declares "
                "no input structure, so the caller can only pass it one free-text "
                "request. Add input fields to be called with real arguments."
            )

    # A cycle in the tool wiring cannot be seen by the flow-graph cycle check,
    # because tool edges are excluded from that graph. Left unchecked it would
    # recurse until the compiler gave up.
    tool_graph = nx.DiGraph()
    for edge in tool_edges:
        tool_graph.add_edge(edge.source, edge.target)
    try:
        for cycle in nx.simple_cycles(tool_graph):
            errors.append(
                "Tools are wired in a loop: "
                f"{' -> '.join(cycle)} -> {cycle[0]}. A group or agent cannot "
                "contain itself."
            )
    except Exception:  # noqa: BLE001 - cycle detection is advisory
        pass

    # ── Per-node config rules ────────────────────────────────────────────────
    for node in canvas.nodes:
        if node.type == "CONDITION" and not node.config.get("branches"):
            errors.append(
                f"Node '{node.id}' (CONDITION) must define at least one branch"
            )

        # An input request with no fields parks the run to ask for nothing,
        # which can only ever be a mistake.
        if node.type == "HUMAN_INPUT":
            fields = (node.config.get("collect_fields") or {}).get("fields") or []
            named = [f for f in fields if (f.get("name") or "").strip()]
            if not named:
                errors.append(
                    f"Node '{node.id}' (HUMAN_INPUT) collects no fields, so it "
                    "would pause the run to ask for nothing. Add at least one "
                    "field to collect."
                )
            if not (node.config.get("prompt") or "").strip():
                warnings.append(
                    f"Node '{node.id}' (HUMAN_INPUT) has no prompt. The paused "
                    "task will not say why the workflow needs these values."
                )

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
                    errors.append(
                        f"Node '{node.id}' (HUMAN_APPROVAL) has nothing on its "
                        f"'{handle}' output, so a {handle} decision would have "
                        "nowhere to go. Connect both outputs."
                    )
            if not (node.config.get("prompt") or "").strip():
                warnings.append(
                    f"Node '{node.id}' (HUMAN_APPROVAL) has no prompt. The "
                    "paused task will just say 'Approve this step?', which "
                    "tells the approver nothing about what they are approving."
                )

        # A LOOP with no mode-specific config is the quietest possible failure:
        # it compiles, packages, runs, and iterates zero times.
        if node.type == "LOOP":
            mode = node.config.get("mode") or "for_each"
            if mode == "for_each" and not (node.config.get("items_path") or "").strip():
                errors.append(
                    f"Node '{node.id}' (LOOP) is in for_each mode but has no "
                    "items path, so it would never iterate. Set the path to the "
                    "list, e.g. 'data.items'."
                )
            if mode == "while" and not (node.config.get("exit_condition") or "").strip():
                errors.append(
                    f"Node '{node.id}' (LOOP) is in while mode but has no exit "
                    "condition, so it would only stop at max_iterations. Set a "
                    "condition, e.g. 'i >= 5'."
                )

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
                    errors.append(
                        f"Node '{node.id}' (LOOP) has nothing on its "
                        f"'{handle}' output, which is {purpose}."
                    )

        # Structure field names become pydantic fields and tool parameters, so
        # they have to be usable as Python identifiers and unique.
        if node.type in AGENT_TYPES:
            for key, label in (
                ("input_structure", "input structure"),
                ("output_structure", "output structure"),
            ):
                errors.extend(
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
                warnings.append(
                    f"Node '{node.id}' ({node.type}) declares an input structure, "
                    "but it only applies when the agent is called as a tool by "
                    "another agent. Here it runs in the flow, so the structure is "
                    "ignored; its input comes from the previous node."
                )

        # `on_error: continue` only means something where the generated module
        # emits an error payload instead of raising; elsewhere it would be
        # silently ignored, which is worse than being told.
        if node.policies.on_error == "continue":
            defn = get_node_definition(node.type)
            if defn and not defn.supports_on_error_continue:
                warnings.append(
                    f"Node '{node.id}' ({node.type}) is set to continue on error, "
                    "but that only applies to nodes that call out of the workflow "
                    "(models, tools, remote agents, functions and transforms). "
                    "This node will fail the run instead."
                )

        if node.type == "A2A_START":
            fields = (node.config.get("payload_schema") or {}).get("fields") or []
            names = [(f.get("name") or "").strip() for f in fields]
            for index, name in enumerate(names):
                if not name:
                    errors.append(
                        f"Node '{node.id}' (A2A_START) payload field {index + 1} has no name"
                    )
            duplicates = {n for n in names if n and names.count(n) > 1}
            for name in sorted(duplicates):
                errors.append(
                    f"Node '{node.id}' (A2A_START) has more than one payload field named '{name}'"
                )
            if not fields:
                warnings.append(
                    f"Node '{node.id}' (A2A_START) declares no payload fields; "
                    "callers get no documented input contract and the test panel "
                    "falls back to a raw JSON editor."
                )

    return errors, warnings


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
