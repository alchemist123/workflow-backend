"""Whether one node's socket may be wired to another's, and why not.

The canvas used to let anything be wired to anything, because the rules lived
only in `semantic_validator.py` and nothing published them. A mistake surfaced
at Save & Compile as prose naming a node id the user had never seen. This
module is the rules themselves, in one place, so that:

* the validator judges an edge by calling into here rather than inline, and
* the canvas is served the same rules as data and can refuse — or explain — a
  connection the moment it is drawn.

One implementation, two consumers. The alternative is a canvas that disagrees
with the compiler, which is worse than no canvas checking at all: a canvas that
blocks something the compiler would have accepted stops real work.

**What belongs here and what does not.** Only rules decidable from the four
things known the instant an edge is drawn — source type, source handle, target
type, target handle. Everything the validator checks that needs the whole graph
(how many ENDs exist, whether a fork's branches reconverge, whether a node is
reachable, whether a handle was left unwired) is deliberately *not* here: it
cannot be judged mid-drag, and pretending otherwise would have the canvas
refusing edges that are only a problem in combination.

The handle a tool hangs off is `TOOLS_HANDLE`; every other target handle is
ordinary flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.nodes.registry import (
    NODE_REGISTRY,
    TOOL_CONSUMER_TYPES,
    TOOL_GROUP_TYPES,
    TOOL_PROVIDER_TYPES,
    get_node_definition,
)

# The one target handle that means "this is a tool for you", not "this runs
# next". Mirrors the checks in semantic_validator and ir.
TOOLS_HANDLE = "tools"

# Every node's inbound flow handle. The canvas has only ever used one.
INPUT_HANDLE = "input"

# The fallback branch. `graph_plan` treats an edge on this handle as the route
# to take when no named branch matched, so it is valid on any branch node
# whether or not anyone declared it.
DEFAULT_ROUTE = "default"


# One sentence per refusal code, for the canvas to render. `{source}` and
# `{target}` are filled in with a node's readable label. Kept here beside the
# rules that raise them so a new rule cannot forget its explanation.
MESSAGES: dict[str, str] = {
    "edge.from_terminal":
        "{source} is where the workflow ends, so nothing can lead out of it.",
    "edge.into_entry":
        "{target} is where the workflow starts, so nothing can lead into it.",
    "tool.consumer_not_allowed":
        "{target} has no tools socket. Tools connect to an agent or a tool group.",
    "tool.provider_not_allowed":
        "{source} cannot be used as a tool. Wire a Tool, MCP server, function, "
        "remote agent or LLM agent into a tools socket instead.",
    "tool.group_in_flow":
        "{source} is a group of tools, so it belongs on the tools socket of an "
        "agent — not in the workflow flow.",
    "edge.unknown_handle":
        "{source} has no output called “{handle}”. Renaming a branch leaves the "
        "edge that left it pointing at nothing.",
}


@dataclass(frozen=True)
class Refusal:
    """Why a connection cannot be made."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _listed(types: frozenset[str]) -> str:
    return ", ".join(sorted(types))


def output_handles_for(node_type: str, config: dict | None = None) -> list[str]:
    """The source handles a node of this type actually has.

    CONDITION and PARALLEL_FORK name their own branches in config, so their
    handles are not a fixed list — which is why `config` is accepted. Everything
    else answers from the registry.
    """
    definition = get_node_definition(node_type)
    if definition is None:
        return []
    if not definition.allows_outbound:
        return []

    config = config or {}
    if node_type in ("CONDITION", "PARALLEL_FORK"):
        branches = config.get("branches") or []
        named = [
            (b if isinstance(b, str) else (b or {}).get("name") or "").strip()
            for b in branches
        ]
        found = [n for n in named if n]
        if found:
            # `default` is a route in its own right, not a branch someone named:
            # the compiler picks it up as the fallback when no branch matches
            # (graph_plan.py), and the generated node refuses to run without one
            # if nothing else matched. So it is always available.
            return [*dict.fromkeys(found), DEFAULT_ROUTE]
        # An unconfigured branch node: fall through to whatever it declares, so
        # a half-drawn canvas is not judged for handles it has not named yet.
    return list(definition.output_handles)


def can_connect(
    source_type: str,
    source_handle: str | None,
    target_type: str,
    target_handle: str | None,
    *,
    source_config: dict | None = None,
) -> Refusal | None:
    """`None` when the edge is fine, otherwise why it is not.

    Unknown node types answer `None`: an unrecognised type is its own error,
    reported once against the node, and refusing its edges as well would bury
    that under noise.
    """
    source = get_node_definition(source_type)
    target = get_node_definition(target_type)
    if source is None or target is None:
        return None

    source_handle = source_handle or "output"
    target_handle = target_handle or INPUT_HANDLE
    is_tool_edge = target_handle == TOOLS_HANDLE

    # ── A2: a terminal node is where the workflow stops ──────────────────────
    if not source.allows_outbound:
        return Refusal(
            "edge.from_terminal",
            f"{source_type} is where the workflow ends, so nothing can lead out "
            "of it.",
        )

    # ── A1: the entry node is where it starts ────────────────────────────────
    if not target.allows_inbound:
        return Refusal(
            "edge.into_entry",
            f"{target_type} is where the workflow starts, so nothing can lead "
            "into it.",
        )

    if is_tool_edge:
        # ── A3: only some nodes have a tools socket ──────────────────────────
        if not target.accepts_tools:
            return Refusal(
                "tool.consumer_not_allowed",
                f"{target_type} has no tools socket. Tools connect to: "
                f"{_listed(TOOL_CONSUMER_TYPES)}.",
            )
        # ── A4: and only some nodes can be used as one ───────────────────────
        if not source.provides_tool and not source.is_tool_group:
            return Refusal(
                "tool.provider_not_allowed",
                f"{source_type} cannot be used as a tool. Only these can: "
                f"{_listed(TOOL_PROVIDER_TYPES)}.",
            )
    else:
        # ── A5: a tool group is a tool, so it belongs on a tools socket ──────
        if source.is_tool_group:
            return Refusal(
                "tool.group_in_flow",
                f"{source_type} is a group of tools, so its output belongs on "
                "the tools socket of an agent or another group — not in the "
                "workflow flow.",
            )
        # Note there is deliberately no rule against a *leaf* tool provider in
        # the flow. TOOL and DATASOURCE are dual-purpose: both render through
        # `nodes/mcp_tool.py.j2` as ordinary flow nodes, so wiring one onward is
        # legitimate and the compiler allows it. Refusing it here would block
        # real work — the exact failure this module exists to avoid.

    # ── A6: the handle has to be one the source actually has ─────────────────
    # Nothing checked this before, so an edge leaving a CONDITION on a branch
    # nobody named compiled happily and then never fired.
    handles = output_handles_for(source_type, source_config)
    if handles and source_handle not in handles:
        return Refusal(
            "edge.unknown_handle",
            f"{source_type} has no output called '{source_handle}'. It has: "
            f"{', '.join(handles)}.",
        )

    return None


def accepts_on_tools(node_type: str) -> bool:
    """Whether this type has a tools socket at all."""
    definition = get_node_definition(node_type)
    return bool(definition and definition.accepts_tools)


def connectable_targets(
    source_type: str, source_handle: str | None, *, source_config: dict | None = None
) -> dict[str, list[str]]:
    """Every node type this handle may legally reach, by the handle it lands on.

    This is what the canvas offers when someone drags out of a socket and drops
    on empty space: the suggestion list is the answer to this, so it cannot
    offer something the compiler will reject.
    """
    reachable: dict[str, list[str]] = {}
    for node_type in NODE_REGISTRY:
        landings = []
        for target_handle in (INPUT_HANDLE, TOOLS_HANDLE):
            if target_handle == TOOLS_HANDLE and not accepts_on_tools(node_type):
                continue
            refusal = can_connect(
                source_type,
                source_handle,
                node_type,
                target_handle,
                source_config=source_config,
            )
            if refusal is None:
                landings.append(target_handle)
        if landings:
            reachable[node_type] = landings
    return reachable


def rules_document() -> dict[str, Any]:
    """The whole rule set, for the canvas to hold as data.

    Shipping the answers rather than the logic is deliberate: the frontend has
    no test runner, so a reimplementation there would be the one untested copy
    of the rules. A lookup table cannot drift from the compiler.
    """
    types: dict[str, Any] = {}
    for node_type, definition in NODE_REGISTRY.items():
        types[node_type] = {
            "output_handles": list(definition.output_handles),
            "accepts_tools": definition.accepts_tools,
            "tool_handles": [TOOLS_HANDLE] if definition.accepts_tools else [],
            "allows_inbound": definition.allows_inbound,
            "allows_outbound": definition.allows_outbound,
            "provides_tool": definition.provides_tool,
            "is_tool_group": definition.is_tool_group,
            "is_agent": definition.is_agent,
            "uses_named_routes": definition.uses_named_routes,
            # Where a plain drag out of this type may land, by target type.
            "connects_to": connectable_targets(node_type, None),
        }

    # Every refusal this module produces, precomputed, keyed
    # "SOURCE>TARGET@handle". The canvas looks the verdict up rather than
    # deriving it, so the browser holds no copy of the rules at all -- only
    # their answers. That matters because the frontend has no test runner: a
    # reimplementation there would be the one copy nothing checks. Only
    # refusals are listed (a few hundred of ~900 combinations), so anything
    # absent is allowed.
    refusals: dict[str, str] = {}
    for source_type in NODE_REGISTRY:
        for target_type in NODE_REGISTRY:
            for target_handle in (INPUT_HANDLE, TOOLS_HANDLE):
                refusal = can_connect(source_type, None, target_type, target_handle)
                if refusal is not None:
                    refusals[f"{source_type}>{target_type}@{target_handle}"] = (
                        refusal.code
                    )

    return {
        "tools_handle": TOOLS_HANDLE,
        "input_handle": INPUT_HANDLE,
        "default_route": DEFAULT_ROUTE,
        "types": types,
        "refusals": refusals,
        # One sentence per code rather than per combination: the same refusal
        # applies hundreds of times, and repeating its text would be most of
        # the payload. `{source}` and `{target}` are substituted by the canvas
        # with the node's readable label, which is friendlier than the type
        # name this module has to hand.
        "messages": MESSAGES,
        "refusal_codes": sorted(set(refusals.values())),
        # Groups that exist only so the canvas can phrase things.
        "tool_consumers": sorted(TOOL_CONSUMER_TYPES),
        "tool_providers": sorted(TOOL_PROVIDER_TYPES),
        "tool_groups": sorted(TOOL_GROUP_TYPES),
    }
