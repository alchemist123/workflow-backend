"""What a node can expect on its input, worked out from the canvas.

A no-code mapper needs to offer a list to pick from rather than a text box, and
a compiler that checks a mapping needs to know whether the thing being mapped
actually exists. Both want the same answer: *at this node, what is known to be
available?*

It is knowable because several node types declare their output shape:

* A2A_START declares `payload_schema` — the workflow's input contract
* TRANSFORM in `fields` mode declares the fields it builds
* an agent declares `output_structure`
* HUMAN_INPUT / HUMAN_APPROVAL declare what they collect
* LOOP, WAIT and CONDITION add fixed keys of their own

Everything else — a JMESPath expression, an agent's free text, an MCP tool's
reply — is opaque, and this module says so rather than guessing. A node
downstream of an opaque node still sees everything declared *before* it,
because the payload passes through.

Two deliberate limits:

* only **ancestors** contribute. A variable saved on a branch that did not run
  is not available, and offering it would be offering something that is
  sometimes absent.
* an opaque node does not erase what came before it, but it adds nothing. The
  result is "what is guaranteed", which is the only honest basis for a
  compile-time error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.nodes.agent_io import FIELD_TYPES

# Where a field came from, for the picker to group by.
SOURCE_PAYLOAD = "payload"
SOURCE_VARIABLE = "variable"


@dataclass(frozen=True)
class InputField:
    """One thing a node can read, by the path a mapping would use."""

    path: str          # "data.qty" or "vars.order.sku"
    type: str          # a FIELD_TYPES key, or "any" when undeclared
    source: str        # SOURCE_PAYLOAD | SOURCE_VARIABLE
    from_node: str     # canvas id of the node that produces it
    label: str         # what the picker shows

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "type": self.type,
            "source": self.source,
            "from_node": self.from_node,
            "label": self.label,
        }


def _declared_fields(node: Any) -> list[tuple[str, str]] | None:
    """(name, type) pairs a node type promises, or None when opaque."""
    config = node.config or {}
    node_type = node.type

    if node_type == "A2A_START":
        if (config.get("input_mode") or "json") == "text":
            return [("text", "text")]
        fields = (config.get("payload_schema") or {}).get("fields") or []
        return [
            ((f.get("name") or "").strip(), f.get("type") or "string")
            for f in fields
            if (f.get("name") or "").strip()
        ]

    if node_type == "TRANSFORM":
        # Only the declarative mode knows its own shape; an expression does not.
        if (config.get("mode") or "jmespath") != "fields":
            return None
        return [
            ((f.get("name") or "").strip(), f.get("type") or "string")
            for f in config.get("output_fields") or []
            if (f.get("name") or "").strip()
        ]

    if node_type in ("LLM_AGENT", "AGENT", "ORCHESTRATOR_AGENT"):
        fields = (config.get("output_structure") or {}).get("fields") or []
        if not fields:
            return None
        return [
            ((f.get("name") or "").strip(), f.get("type") or "string")
            for f in fields
            if (f.get("name") or "").strip()
        ]

    if node_type == "HUMAN_INPUT":
        return [
            ((f.get("name") or "").strip(), f.get("type") or "string")
            for f in (config.get("collect_fields") or {}).get("fields") or []
            if (f.get("name") or "").strip()
        ]

    if node_type == "HUMAN_APPROVAL":
        collected = [
            ((f.get("name") or "").strip(), f.get("type") or "string")
            for f in (config.get("collect_fields") or {}).get("fields") or []
            if (f.get("name") or "").strip()
        ]
        return [("approved", "boolean"), ("comment", "string"), *collected]

    if node_type == "MCP_TOOL":
        # What a tool returns is the server's business, not the canvas's --
        # `result_key` is the one thing that is knowable, and only when set.
        key = (config.get("result_key") or "").strip()
        return [(key, "object")] if key else None

    if node_type == "WAIT":
        return [("waited_seconds", "number")]

    if node_type == "CONDITION":
        return [("_branch", "string")]

    if node_type == "LOOP":
        # What the *body* sees. The `done` side adds more, but a mapper reading
        # a loop's output is almost always in the body.
        return [("current_item", "object"), ("current_index", "integer")]

    # END has no downstream; everything else is opaque.
    return None


def _flow_parents(canvas: Any) -> dict[str, list[str]]:
    parents: dict[str, list[str]] = {n.id: [] for n in canvas.nodes}
    for edge in canvas.edges:
        if edge.target_handle == "tools":
            continue  # a tool is wired into a consumer, not into the flow
        if edge.target in parents:
            parents[edge.target].append(edge.source)
    return parents


def _ancestors(canvas: Any, node_id: str) -> list[str]:
    """Every node that can run before `node_id`, nearest first.

    Breadth-first so the closest producer of a name wins: a field re-made by
    the node just upstream is the one that will actually be on the payload.
    """
    parents = _flow_parents(canvas)
    seen: set[str] = set()
    order: list[str] = []
    frontier = list(parents.get(node_id, []))

    while frontier:
        current = frontier.pop(0)
        if current in seen or current == node_id:
            continue
        seen.add(current)
        order.append(current)
        frontier.extend(parents.get(current, []))

    return order


def available_inputs(canvas: Any, node_id: str) -> list[InputField]:
    """Everything `node_id` can read, as picker-ready entries.

    Payload fields first (nearest producer wins on a name clash), then the
    variables its ancestors saved.
    """
    nodes = {n.id: n for n in canvas.nodes}
    if node_id not in nodes:
        return []

    titles = {
        n.id: (n.metadata.title or n.id) if n.metadata else n.id for n in canvas.nodes
    }

    fields: dict[str, InputField] = {}
    variables: list[InputField] = []

    for ancestor_id in _ancestors(canvas, node_id):
        ancestor = nodes[ancestor_id]
        declared = _declared_fields(ancestor)

        for name, field_type in declared or []:
            path = f"data.{name}"
            if path in fields:
                continue  # a nearer node already produces this name
            fields[path] = InputField(
                path=path,
                type=field_type if field_type in FIELD_TYPES else "any",
                source=SOURCE_PAYLOAD,
                from_node=ancestor_id,
                label=f"{name} — from {titles[ancestor_id]}",
            )

        # A named result is readable whole, and field-wise when its shape is known.
        variable = ((ancestor.config or {}).get("output_variable") or "").strip()
        if not variable:
            continue
        variables.append(
            InputField(
                path=f"vars.{variable}",
                type="object",
                source=SOURCE_VARIABLE,
                from_node=ancestor_id,
                label=f"{variable} — all of {titles[ancestor_id]}",
            )
        )
        for name, field_type in declared or []:
            variables.append(
                InputField(
                    path=f"vars.{variable}.{name}",
                    type=field_type if field_type in FIELD_TYPES else "any",
                    source=SOURCE_VARIABLE,
                    from_node=ancestor_id,
                    label=f"{variable}.{name}",
                )
            )

    return [*fields.values(), *variables]


def input_is_opaque(canvas: Any, node_id: str) -> bool:
    """Whether some ancestor's output shape is unknown.

    When it is, a mapping source that is not in `available_inputs` might still
    resolve at runtime — so the compiler warns instead of failing.
    """
    nodes = {n.id: n for n in canvas.nodes}
    return any(
        _declared_fields(nodes[ancestor]) is None
        for ancestor in _ancestors(canvas, node_id)
        if ancestor in nodes
    )
