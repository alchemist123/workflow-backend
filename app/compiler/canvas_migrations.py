"""Forward migrations for stored canvas JSON.

A canvas saved before the move to ADK graph workflows still names an
HTTP_TRIGGER, SCHEDULE_TRIGGER, WEBHOOK_TRIGGER or QUEUE_TRIGGER node, none of
which exist any more.  `migrate_canvas` rewrites those in place so an old
workflow opens, validates and packages without the user rebuilding it.

Migrations are keyed on `schema_version` and are idempotent: running one twice
is a no-op, so it is safe to apply on every read as well as in the one-shot
backfill (`scripts/migrate_canvases.py`).

Version history
    1  original: four trigger node types, no schema_version field
    2  A2A_START replaces all triggers; single entry, single terminal
    3  ORCHESTRATOR_AGENT loses tool_execution_mode; sequential/parallel tool
       execution is drawn with SEQUENTIAL_AGENT / PARALLEL_AGENT nodes
    4  MODEL becomes LLM_AGENT, which can also be attached to another agent or
       a tool group as a sub-agent
    5  LOOP config written by the old panel (`items_expression`) becomes the
       `items_path` the node actually declares
    6  HUMAN_APPROVAL loses timeout_seconds / on_timeout, which nothing ever
       implemented and the packaged agent cannot
"""

from __future__ import annotations

from typing import Any

from app.schemas.canvas import CURRENT_SCHEMA_VERSION

# Old type -> how its config carries over to A2A_START.
_RETIRED_TRIGGERS = {
    "HTTP_TRIGGER",
    "SCHEDULE_TRIGGER",
    "WEBHOOK_TRIGGER",
    "QUEUE_TRIGGER",
}


def canvas_schema_version(canvas: dict[str, Any]) -> int:
    """The stored version; 1 when the field predates versioning."""
    try:
        return int(canvas.get("schema_version") or 1)
    except (TypeError, ValueError):
        return 1


def needs_migration(canvas: dict[str, Any]) -> bool:
    return canvas_schema_version(canvas) < CURRENT_SCHEMA_VERSION


def migrate_canvas(canvas: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Bring a canvas up to CURRENT_SCHEMA_VERSION.

    Returns `(canvas, notes)`.  `notes` describes what changed, for the backfill
    script's output and for surfacing in the UI.  The input is not mutated.
    """
    if not isinstance(canvas, dict):
        return canvas, []

    working = {**canvas}
    notes: list[str] = []

    if canvas_schema_version(working) < 2:
        working, migration_notes = _migrate_v1_to_v2(working)
        notes.extend(migration_notes)

    if canvas_schema_version(working) < 3:
        working, migration_notes = _migrate_v2_to_v3(working)
        notes.extend(migration_notes)

    if canvas_schema_version(working) < 4:
        working, migration_notes = _migrate_v3_to_v4(working)
        notes.extend(migration_notes)

    if canvas_schema_version(working) < 5:
        working, migration_notes = _migrate_v4_to_v5(working)
        notes.extend(migration_notes)

    if canvas_schema_version(working) < 6:
        working, migration_notes = _migrate_v5_to_v6(working)
        notes.extend(migration_notes)

    if canvas_schema_version(working) < 7:
        working, migration_notes = _migrate_v6_to_v7(working)
        notes.extend(migration_notes)

    working["schema_version"] = CURRENT_SCHEMA_VERSION
    return working, notes


def _migrate_v1_to_v2(canvas: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Rewrite retired trigger nodes as A2A_START.

    Node id, position, title and outbound edges are all preserved, so the canvas
    looks the same and nothing downstream has to be rewired.
    """
    notes: list[str] = []
    nodes: list[dict] = [dict(n) for n in canvas.get("nodes") or []]

    trigger_indices = [
        i for i, node in enumerate(nodes) if node.get("type") in _RETIRED_TRIGGERS
    ]
    if not trigger_indices:
        return {**canvas, "nodes": nodes}, notes

    # Keep the first trigger as the entry node; a canvas with several is now
    # invalid, so the extras are dropped along with their edges.
    keep_index, *drop_indices = trigger_indices
    kept = nodes[keep_index]
    old_type = kept["type"]

    nodes[keep_index] = _to_a2a_start(kept)
    notes.append(
        f"node '{kept.get('id')}': {old_type} -> A2A_START"
        + (
            " (payload fields carried over from body_schema)"
            if (kept.get("config") or {}).get("body_schema")
            else ""
        )
    )

    dropped_ids = {nodes[i].get("id") for i in drop_indices}
    for index in drop_indices:
        notes.append(
            f"node '{nodes[index].get('id')}': {nodes[index]['type']} removed "
            "(a workflow may only have one entry node)"
        )
    if dropped_ids:
        nodes = [n for n in nodes if n.get("id") not in dropped_ids]

    edges = [
        dict(e)
        for e in canvas.get("edges") or []
        if e.get("source") not in dropped_ids and e.get("target") not in dropped_ids
    ]
    removed_edges = len(canvas.get("edges") or []) - len(edges)
    if removed_edges:
        notes.append(f"{removed_edges} edge(s) removed with the extra entry nodes")

    return {**canvas, "nodes": nodes, "edges": edges}, notes


def _migrate_v2_to_v3(canvas: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop `tool_execution_mode` from ORCHESTRATOR_AGENT nodes.

    The setting never had an implementation in a generated package, so removing
    it changes no behaviour. What replaces it is a SEQUENTIAL_AGENT or
    PARALLEL_AGENT node between the tools and the agent — which cannot be
    inferred, because the old flag said nothing about *which* tools should be
    grouped or in what order. A node that had it set to `parallel` therefore
    gets a note saying so, rather than a guessed rewiring.
    """
    notes: list[str] = []
    nodes: list[dict] = []

    for node in canvas.get("nodes") or []:
        node = dict(node)
        config = dict(node.get("config") or {})
        mode = config.pop("tool_execution_mode", None)

        if mode is not None:
            node["config"] = config
            notes.append(
                f"node '{node.get('id')}': tool_execution_mode={mode!r} removed"
            )
            if mode == "parallel":
                metadata = dict(node.get("metadata") or {})
                note = (
                    "Previously set to run tool calls in parallel. That setting "
                    "is gone: wire the tools into a Parallel Tools node and "
                    "connect it to this agent instead."
                )
                existing = (metadata.get("description") or "").strip()
                metadata["description"] = (
                    f"{existing}\n\n{note}".strip() if existing else note
                )
                node["metadata"] = metadata
                notes.append(
                    f"node '{node.get('id')}': how to restore parallel tool "
                    "execution noted in its description"
                )

        nodes.append(node)

    return {**canvas, "nodes": nodes}, notes


def _migrate_v3_to_v4(canvas: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Rename MODEL nodes to LLM_AGENT.

    A pure rename: every MODEL config key is still a valid LLM_AGENT key, so
    node id, position, config and edges all carry over untouched and the node
    behaves exactly as before. What is new is only what it *can* now do — take
    an input/output structure, and be wired into an agent or a tool group.

    The one config key that does not survive is `response_format: "json"`. It
    asked for a JSON reply without saying what shape, which ADK's
    `output_schema` supersedes; a node that used it gets a note rather than a
    guessed structure, since there is nothing in the old value to derive one
    from.
    """
    notes: list[str] = []
    nodes: list[dict] = []

    for node in canvas.get("nodes") or []:
        if node.get("type") != "MODEL":
            nodes.append(node)
            continue

        node = dict(node)
        node["type"] = "LLM_AGENT"
        config = dict(node.get("config") or {})
        response_format = config.pop("response_format", None)
        node["config"] = config
        notes.append(f"node '{node.get('id')}': MODEL -> LLM_AGENT")

        if response_format == "json":
            metadata = dict(node.get("metadata") or {})
            note = (
                "Previously set to return JSON. Declare an output structure on "
                "this node instead, which constrains the reply to a named shape "
                "rather than just to valid JSON."
            )
            existing = (metadata.get("description") or "").strip()
            metadata["description"] = (
                f"{existing}\n\n{note}".strip() if existing else note
            )
            node["metadata"] = metadata
            notes.append(
                f"node '{node.get('id')}': response_format='json' removed; "
                "output structure noted in its description"
            )

        nodes.append(node)

    return {**canvas, "nodes": nodes}, notes


def _migrate_v4_to_v5(canvas: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Point LOOP config at the keys the node actually reads.

    The config panel used to write `items_expression` and `item_variable`, and
    offered no way to set `items_path`, `mode` or `exit_condition` — the keys the
    LOOP node declares and the generated module reads. So a loop built in the UI
    arrived with no list to walk and iterated zero times, silently.

    `items_expression` held exactly what `items_path` wants (a field name or a
    dotted path), so it carries straight over. `item_variable` has no equivalent
    and is dropped: the loop always exposes the current element as
    `current_item`, which is not configurable.
    """
    notes: list[str] = []
    nodes: list[dict] = []

    for node in canvas.get("nodes") or []:
        if node.get("type") != "LOOP":
            nodes.append(node)
            continue

        node = dict(node)
        config = dict(node.get("config") or {})
        node_id = node.get("id")

        expression = str(config.pop("items_expression", "") or "").strip()
        variable = config.pop("item_variable", None)

        if expression and not (config.get("items_path") or "").strip():
            config["items_path"] = expression
            notes.append(
                f"node '{node_id}': items_expression {expression!r} -> items_path"
            )
        elif expression:
            notes.append(f"node '{node_id}': dropped unused items_expression")

        if variable not in (None, "", "item"):
            notes.append(
                f"node '{node_id}': item_variable {variable!r} dropped; the "
                "current element is always 'current_item'"
            )

        # Mode was never settable either, so an old node has none.
        if not config.get("mode"):
            config["mode"] = "for_each"
            notes.append(f"node '{node_id}': mode defaulted to for_each")

        node["config"] = config
        nodes.append(node)

    return {**canvas, "nodes": nodes}, notes


def _migrate_v5_to_v6(canvas: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop the HUMAN_APPROVAL settings the package cannot honour.

    `timeout_seconds` and `on_timeout` promised that an approval would expire.
    Nothing implemented them, and the packaged agent cannot: the node returns a
    `RequestInput` and the graph parks at A2A `input-required`, so there is no
    process waiting for a clock to fire. Expiring an approval needs a scheduler
    outside the package, so the settings go rather than stay as controls that
    quietly do nothing.
    """
    notes: list[str] = []
    nodes: list[dict] = []

    for node in canvas.get("nodes") or []:
        if node.get("type") != "HUMAN_APPROVAL":
            nodes.append(node)
            continue

        node = dict(node)
        config = dict(node.get("config") or {})
        node_id = node.get("id")

        dropped = [key for key in ("timeout_seconds", "on_timeout") if key in config]
        for key in dropped:
            config.pop(key)
        if dropped:
            notes.append(
                f"node '{node_id}': {', '.join(dropped)} removed "
                "(an approval cannot expire inside the package)"
            )

        # The config panel wrote `approvers` and `message`; the node declares
        # `assignees` and `prompt`, and nothing read the panel's names. Same
        # shape of mistake as the LOOP panel in v5.
        legacy = {"approvers": "assignees", "message": "prompt"}
        for old_key, new_key in legacy.items():
            if old_key not in config:
                continue
            value = config.pop(old_key)
            if config.get(new_key):
                notes.append(f"node '{node_id}': dropped unused {old_key}")
                continue
            if old_key == "approvers" and isinstance(value, str):
                # The panel collected them as one comma-separated string.
                value = [part.strip() for part in value.split(",") if part.strip()]
            if value:
                config[new_key] = value
                notes.append(f"node '{node_id}': {old_key} -> {new_key}")

        node["config"] = config
        nodes.append(node)

    return {**canvas, "nodes": nodes}, notes


def _migrate_v6_to_v7(canvas: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Repair the ways a fork/merge pair drawn in the UI could not work.

    PARALLEL_FORK's outgoing handles come from `branches`, and a node dropped
    from the palette had none. It could not be wired (no handles to drag from)
    and could not compile (the schema wants at least two). Where edges exist,
    their handles say what the branches were; a fork with no edges at all gets
    the default pair the palette now drops. A fork also loses `output_variable`
    — it hands each branch the payload it was given, so it has no result of its
    own to name.

    MERGE loses `strategy`. It compiles to an ADK JoinNode, whose
    `_requires_all_predecessors` is True and not configurable: it always waits
    for every branch. "Continue on the first" was never implementable, and the
    config panel compounded it by offering `all` / `first` / `any`, none of
    which were even in the schema's enum — so every Merge configured from the
    panel failed validation outright. `merge_mode`, which does now decide how
    the branch outputs are combined, is kept as it stands.
    """
    notes: list[str] = []
    nodes: list[dict] = []
    edges = canvas.get("edges") or []

    for node in canvas.get("nodes") or []:
        node_type = node.get("type")
        if node_type not in ("MERGE", "PARALLEL_FORK"):
            nodes.append(node)
            continue

        node = dict(node)
        config = dict(node.get("config") or {})
        node_id = node.get("id")

        if node_type == "MERGE":
            if config.pop("strategy", None) is not None:
                notes.append(
                    f"node '{node_id}': strategy removed "
                    "(a merge always waits for every branch)"
                )
        else:
            branches = [b for b in (config.get("branches") or []) if b]
            used = [
                e.get("source_handle") or "output"
                for e in edges
                if e.get("source") == node_id
            ]
            merged = branches + [h for h in used if h not in branches]
            if not merged:
                merged = ["branch_1", "branch_2"]
            if merged != branches:
                config["branches"] = merged
                notes.append(
                    f"node '{node_id}': branches set to {merged} "
                    "(a fork with none had no handles to wire)"
                )

        # A fork hands each branch the payload it was given, so it has no
        # result of its own to name.
        if node_type == "PARALLEL_FORK" and config.pop("output_variable", None):
            notes.append(f"node '{node_id}': output_variable removed")

        node["config"] = config
        nodes.append(node)

    return {**canvas, "nodes": nodes}, notes


def _to_a2a_start(node: dict[str, Any]) -> dict[str, Any]:
    """Convert one retired trigger node into an A2A_START node."""
    old_config = node.get("config") or {}
    old_type = node.get("type")

    config: dict[str, Any] = {
        "input_mode": "json",
        "state_key": "wf",
    }

    # HTTP_TRIGGER already stored the payload contract the test form renders
    # from, under the old name `body_schema`. Carry it straight over.
    body_schema = old_config.get("body_schema")
    if isinstance(body_schema, dict) and body_schema.get("fields"):
        config["payload_schema"] = {"fields": body_schema["fields"]}
    else:
        config["payload_schema"] = {"fields": []}

    metadata = dict(node.get("metadata") or {})
    if not (metadata.get("title") or "").strip():
        metadata["title"] = "A2A Start"

    # Record what this node used to be, so the old settings are not silently
    # lost — a cron expression or queue name may still be needed elsewhere.
    carried = {
        key: old_config[key]
        for key in ("path", "method", "cron", "timezone", "queue_name", "secret")
        if old_config.get(key)
    }
    if carried:
        summary = ", ".join(f"{k}={v!r}" for k, v in carried.items())
        note = f"Migrated from {old_type} ({summary}). Scheduling and routing now live outside the workflow."
        existing = (metadata.get("description") or "").strip()
        metadata["description"] = f"{existing}\n\n{note}".strip() if existing else note

    return {
        **node,
        "type": "A2A_START",
        "version": "1",
        "config": config,
        "metadata": metadata,
    }
