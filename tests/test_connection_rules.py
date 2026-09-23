"""The rules the canvas enforces must be exactly the rules the compiler does.

The canvas cannot ask the compiler mid-drag, so it holds its own copy of the
connection rules (`app/compiler/connection_rules.py`, served to the browser as
data). Two copies of a rule is how a canvas ends up refusing an edge the
compiler would have accepted — which is worse than no canvas checking at all,
because it stops real work with no way around it.

So the copies are proved equivalent here, exhaustively: every ordered pair of
node types, on both the flow handle and the tools handle. 21 types is 882
combinations, which is cheap to check and impossible to drift from.

Deliberately *not* unified into one implementation. The validator's prose is
asserted by 60 substring checks across 11 test files, and `can_connect` needs
different wording anyway — it addresses someone looking at a canvas, not
reading a compile log. Two implementations with a total equivalence proof is
the stronger arrangement: neither can move without this failing.
"""

from __future__ import annotations

import warnings

import pytest

from app.compiler.connection_rules import (
    INPUT_HANDLE,
    TOOLS_HANDLE,
    can_connect,
    connectable_targets,
    output_handles_for,
    rules_document,
)
from app.compiler.semantic_validator import validate_semantics
from app.nodes.registry import NODE_REGISTRY
from app.schemas.canvas import CURRENT_SCHEMA_VERSION, CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)

# The five connection refusals the validator can produce, by a substring unique
# to each. If the validator's wording changes, this list is where it is noticed.
_VALIDATOR_REFUSALS = (
    "must not have inbound edges",
    "must not have outbound edges",
    "has no tools input",
    "cannot be used as a tool",
    "is a tool group, so its output",
)

_ALL_TYPES = sorted(NODE_REGISTRY)


def _node(node_id: str, node_type: str, config: dict | None = None) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": node_id, "description": ""},
        "config": config or {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1},
                     "on_error": "fail"},
    }


def _edge(source: str, target: str, source_handle: str, target_handle: str) -> dict:
    return {
        "id": f"e_{source}_{target}_{target_handle}",
        "source": source, "source_handle": source_handle,
        "target": target, "target_handle": target_handle, "condition": None,
    }


def _legal_handle(node_type: str) -> str:
    """A source handle this type actually has, so A6 is not what fires.

    The equivalence being proved is about A1-A5, which the validator also
    implements. A6 is new and has no counterpart there, so feeding a branch
    node the handle `output` it does not have would make every row disagree for
    a reason that is not the rule under test.
    """
    handles = output_handles_for(node_type)
    return handles[0] if handles else "output"


def _canvas_with(source_type: str, target_type: str, target_handle: str) -> dict:
    """A canvas whose only interesting feature is the one edge under test.

    Everything else is there so the graph-level rules have nothing to say: a
    trigger, a terminal, and a path between them. The nodes under test are
    attached to that path, so the verdict isolates the edge.
    """
    nodes = [
        _node("start", "A2A_START", {"input_mode": "json", "state_key": "wf"}),
        _node("src", source_type),
        _node("tgt", target_type),
        _node("end", "END", {"output_mapping": {}}),
    ]
    edges = [_edge("src", "tgt", _legal_handle(source_type), target_handle)]

    # Give both nodes a place in the flow where their own type allows it, so a
    # verdict is about the edge under test rather than about orphanhood. A tool
    # group is never wired onward: doing so trips A5 on the *harness's* edge,
    # which would report a disagreement about an edge nobody is testing.
    source = NODE_REGISTRY[source_type]
    target = NODE_REGISTRY[target_type]
    if source.allows_inbound and not source.is_tool_group:
        edges.append(_edge("start", "src", "output", INPUT_HANDLE))
    if target.allows_outbound and not target.is_tool_group:
        edges.append(_edge("tgt", "end", _legal_handle(target_type), INPUT_HANDLE))
    if target_handle == TOOLS_HANDLE and not target.is_tool_group:
        edges.append(_edge("start", "tgt", "output", INPUT_HANDLE))

    return {"schema_version": CURRENT_SCHEMA_VERSION, "nodes": nodes, "edges": edges}


def _validator_refuses(canvas: dict) -> list[str]:
    """The connection-level errors the validator reports for this canvas."""
    errors, _warnings = validate_semantics(CanvasPayload.model_validate(canvas))
    return [e for e in errors if any(marker in e for marker in _VALIDATOR_REFUSALS)]


# ── The equivalence proof ────────────────────────────────────────────────────


@pytest.mark.parametrize("source_type", _ALL_TYPES)
@pytest.mark.parametrize("target_handle", [INPUT_HANDLE, TOOLS_HANDLE])
def test_the_canvas_and_the_compiler_agree(source_type, target_handle):
    """For every target type: `can_connect` refuses exactly when the validator does."""
    disagreements = []
    for target_type in _ALL_TYPES:
        canvas = _canvas_with(source_type, target_type, target_handle)
        refusal = can_connect(
            source_type, _legal_handle(source_type), target_type, target_handle
        )
        reported = _validator_refuses(canvas)

        if bool(refusal) != bool(reported):
            disagreements.append(
                f"{source_type} --{target_handle}--> {target_type}: "
                f"canvas says {refusal.code if refusal else 'OK'}, "
                f"compiler says {reported or 'OK'}"
            )

    assert not disagreements, "\n".join(disagreements)


def test_the_proof_actually_covers_something():
    """A guard on the guard: an equivalence test over nothing proves nothing."""
    refused = sum(
        1
        for source in _ALL_TYPES
        for target in _ALL_TYPES
        for handle in (INPUT_HANDLE, TOOLS_HANDLE)
        if can_connect(source, "output", target, handle)
    )
    total = len(_ALL_TYPES) ** 2 * 2
    # Most combinations are legal; the refusals are the interesting minority.
    assert 100 < refused < total, f"{refused} of {total} refused"


# ── The individual rules, stated once so they are readable ───────────────────


@pytest.mark.parametrize(
    ("source", "handle", "target", "target_handle", "code"),
    [
        # A tool into an agent's socket, and the same tool into the flow: both
        # fine. TOOL and DATASOURCE are dual-purpose — they also render as flow
        # nodes through nodes/mcp_tool.py.j2.
        ("TOOL", "output", "ORCHESTRATOR_AGENT", TOOLS_HANDLE, None),
        ("TOOL", "output", "TRANSFORM", INPUT_HANDLE, None),
        ("DATASOURCE", "output", "TRANSFORM", INPUT_HANDLE, None),
        # A1 / A2: the ends of the workflow.
        ("TRANSFORM", "output", "A2A_START", INPUT_HANDLE, "edge.into_entry"),
        ("END", "output", "TRANSFORM", INPUT_HANDLE, "edge.from_terminal"),
        # A3 / A4: who has a tools socket, and who may hang off one.
        ("TOOL", "output", "TRANSFORM", TOOLS_HANDLE, "tool.consumer_not_allowed"),
        ("TRANSFORM", "output", "ORCHESTRATOR_AGENT", TOOLS_HANDLE,
         "tool.provider_not_allowed"),
        # A5: a group is a tool, not a step.
        ("SEQUENTIAL_AGENT", "output", "END", INPUT_HANDLE, "tool.group_in_flow"),
        ("SEQUENTIAL_AGENT", "output", "ORCHESTRATOR_AGENT", TOOLS_HANDLE, None),
        # A6: the handle has to exist.
        ("CONDITION", "invented", "END", INPUT_HANDLE, "edge.unknown_handle"),
        ("HUMAN_APPROVAL", "approved", "END", INPUT_HANDLE, None),
        ("HUMAN_APPROVAL", "maybe", "END", INPUT_HANDLE, "edge.unknown_handle"),
    ],
)
def test_each_rule(source, handle, target, target_handle, code):
    refusal = can_connect(source, handle, target, target_handle)
    assert (refusal.code if refusal else None) == code, refusal


def test_a_refusal_says_what_to_do_instead():
    """The message is shown to someone looking at a canvas, not a compile log."""
    refusal = can_connect("TRANSFORM", "output", "ORCHESTRATOR_AGENT", TOOLS_HANDLE)
    assert "cannot be used as a tool" in refusal.message
    assert "TOOL" in refusal.message, "it should name what can"


def test_an_unknown_node_type_refuses_nothing():
    """An unrecognised type is its own error, reported once against the node.

    Refusing its edges as well would bury that under noise.
    """
    assert can_connect("MADE_UP", "output", "END", INPUT_HANDLE) is None
    assert can_connect("TRANSFORM", "output", "MADE_UP", INPUT_HANDLE) is None


# ── Handles ──────────────────────────────────────────────────────────────────


def test_a_branch_nodes_handles_come_from_its_config():
    """CONDITION and PARALLEL_FORK name their own branches.

    Plus `default`, which is a route rather than a branch anyone named: the
    compiler takes an edge on it as the fallback when nothing matched.
    """
    assert output_handles_for("CONDITION", {"branches": [
        {"name": "big"}, {"name": "small"},
    ]}) == ["big", "small", "default"]
    assert output_handles_for("PARALLEL_FORK", {"branches": ["a", "b"]}) == [
        "a", "b", "default",
    ]


def test_renaming_a_branch_strands_the_edge_that_left_it():
    """Which is the point of A6, and was previously silent.

    A CONDITION starts with the declared `true` / `false` / `default` handles.
    Configure real branches and an edge still pointing at `true` is now
    pointing at nothing — it compiled happily before and then never fired.
    """
    assert can_connect("CONDITION", "true", "END", INPUT_HANDLE) is None

    configured = {"branches": [{"name": "big"}, {"name": "small"}]}
    refusal = can_connect(
        "CONDITION", "true", "END", INPUT_HANDLE, source_config=configured
    )
    assert refusal is not None and refusal.code == "edge.unknown_handle"
    assert "big, small" in refusal.message, refusal.message

    assert can_connect(
        "CONDITION", "big", "END", INPUT_HANDLE, source_config=configured
    ) is None


def test_a_terminal_node_offers_no_outputs():
    assert output_handles_for("END") == []


# ── What the canvas is served ────────────────────────────────────────────────


def test_the_rules_document_covers_every_registered_type():
    doc = rules_document()
    assert set(doc["types"]) == set(NODE_REGISTRY)


def test_the_document_says_where_a_tools_socket_is():
    doc = rules_document()
    assert doc["types"]["ORCHESTRATOR_AGENT"]["tool_handles"] == [TOOLS_HANDLE]
    assert doc["types"]["TRANSFORM"]["tool_handles"] == []


def test_the_suggestions_are_the_rules_and_nothing_else():
    """The drag-to-empty menu is built from this, so it cannot offer a refusal."""
    for source_type in _ALL_TYPES:
        offered = connectable_targets(source_type, "output")
        for target_type, handles in offered.items():
            for handle in handles:
                assert can_connect(source_type, "output", target_type, handle) is None


def test_a_terminal_node_suggests_nothing():
    assert connectable_targets("END", "output") == {}


def test_a_tool_group_only_suggests_tools_sockets():
    offered = connectable_targets("SEQUENTIAL_AGENT", "output")
    assert offered, "a group must be connectable somewhere"
    assert all(handles == [TOOLS_HANDLE] for handles in offered.values()), offered


# ── What the browser is actually sent ────────────────────────────────────────


def test_every_refusal_has_a_sentence_for_the_canvas():
    """The browser renders `messages[code]`, so a code without one shows nothing.

    This is the failure a new rule would introduce silently: the edge turns red
    and says "This connection is not allowed", which tells someone nothing
    about what to do instead.
    """
    from app.compiler.connection_rules import MESSAGES

    doc = rules_document()
    for code in doc["refusal_codes"]:
        assert code in MESSAGES, f"{code} has no message for the canvas"
    # And the handle rule, which is evaluated in the browser rather than
    # precomputed, because it depends on a node's configured branch names.
    assert "edge.unknown_handle" in MESSAGES


def test_the_sentences_only_use_placeholders_the_canvas_fills_in():
    from app.compiler.connection_rules import MESSAGES

    allowed = {"source", "target", "handle"}
    for code, template in MESSAGES.items():
        import re

        used = set(re.findall(r"\{(\w+)\}", template))
        assert used <= allowed, f"{code} uses {used - allowed}"


def test_the_precomputed_verdicts_are_the_rule_itself():
    """The canvas trusts this table completely, so it must be exhaustive.

    Anything absent is taken as allowed, which is the right default only if
    every refusal is present.
    """
    doc = rules_document()
    for source in _ALL_TYPES:
        for target in _ALL_TYPES:
            for handle in (INPUT_HANDLE, TOOLS_HANDLE):
                refusal = can_connect(source, None, target, handle)
                listed = doc["refusals"].get(f"{source}>{target}@{handle}")
                assert listed == (refusal.code if refusal else None), (
                    source, target, handle,
                )


# ── What the canvas draws a node from ────────────────────────────────────────


def test_the_palette_carries_everything_the_canvas_needs_to_draw_a_node():
    """The canvas renders from this, so a missing field is a node it cannot draw.

    It used to render from a hand-written list in `src/nodes/index.ts` instead,
    which had drifted: `AGENT` was absent from it and showed as an amber
    "Unknown node type" card, tools socket and all. The list is now a fallback
    and this is the source, so a new node type needs no frontend edit — but
    only if every field the canvas reads is actually here.
    """
    from app.nodes.registry import get_palette

    needed = {
        "type", "label", "category", "color", "icon", "description", "wave",
        "is_trigger", "is_terminal", "output_handles", "tool_handles",
        "allows_inbound", "allows_outbound", "accepts_tools", "provides_tool",
        "is_tool_group", "is_agent", "uses_named_routes",
    }
    palette = get_palette()
    assert {n["type"] for n in palette} == set(NODE_REGISTRY)
    for entry in palette:
        assert needed <= set(entry), f"{entry['type']} is missing {needed - set(entry)}"


def test_the_tools_socket_is_derived_not_declared():
    """`tool_handles` existed only in the frontend's copy before this.

    Which is precisely how the two could disagree about which nodes have one.
    """
    from app.nodes.registry import get_palette

    for entry in get_palette():
        expected = [TOOLS_HANDLE] if entry["accepts_tools"] else []
        assert entry["tool_handles"] == expected, entry["type"]


def test_the_palette_does_not_leak_credential_field_names():
    """`secret_config_keys` is deliberately not published."""
    from app.nodes.registry import get_palette

    for entry in get_palette():
        assert "secret_config_keys" not in entry
