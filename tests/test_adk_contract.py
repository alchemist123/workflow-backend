"""Pin the third-party API surface the ADK graph packaging depends on.

Every assertion here corresponds to something the graph compiler or the package
template relies on.  If a `google-adk` or `a2a-sdk` upgrade changes one of them,
this file fails loudly here instead of in a customer's generated package.

Each test names the constraint it protects, because several are non-obvious and
were found by running the framework rather than by reading its docs.
"""

from __future__ import annotations

import inspect
import json
import warnings

import pytest

warnings.filterwarnings("ignore", category=UserWarning)

# Imported at module level because ADK resolves node annotations against the
# defining module's globals -- a function-local import leaves `Context`
# unresolvable.  Generated node modules import these at the top for the same
# reason.  The importability tests below re-import them deliberately, so the
# names they guard stay explicit.
from google.adk import Event  # noqa: E402
from google.adk.agents.context import Context  # noqa: E402


# ── Imports the generated code emits ─────────────────────────────────────────


def test_graph_symbols_are_importable():
    """`agent.py` and `nodes/*.py` import exactly these names."""
    from google.adk import Event, Runner, Workflow  # noqa: F401
    from google.adk.agents.context import Context  # noqa: F401
    from google.adk.workflow import (  # noqa: F401
        DEFAULT_ROUTE,
        Edge,
        JoinNode,
        RetryConfig,
        START,
        node,
    )


def test_a2a_symbols_are_importable():
    """`core/agent_card.py` and `core/a2a_app.py` import exactly these names."""
    from a2a.types import AgentCapabilities, AgentCard, AgentSkill  # noqa: F401
    from google.adk.a2a.utils.agent_to_a2a import to_a2a  # noqa: F401


def test_mcp_streamable_http_client_name():
    """`tools/mcp.py` prefers this 1.x name; mcp 2.x renamed it."""
    from mcp import ClientSession  # noqa: F401
    from mcp.client.streamable_http import streamablehttp_client  # noqa: F401


# ── Node authoring contract ──────────────────────────────────────────────────


def test_node_supports_both_parameter_bindings():
    """Generated nodes rely on the default 'state' binding.

    Under 'state', a parameter literally named `node_input` receives the upstream
    node's output verbatim and every other parameter binds from `ctx.state`.
    Under 'node_input', ADK infers schemas from the signature -- which is why the
    templates avoid it (see test_return_annotation_breaks_edge_validation).
    """
    from google.adk.workflow import node

    params = inspect.signature(node).parameters
    assert "parameter_binding" in params
    assert params["parameter_binding"].default == "state"


def test_event_convenience_kwargs():
    """`Event(output=, state=, route=, message=)` route into nested fields.

    `state` -> actions.state_delta, `route` -> actions.route, `message` -> content.
    The generated nodes use all four.
    """
    from google.adk import Event

    event = Event(output={"a": 1}, state={"k": "v"}, route="BRANCH", message="hi")

    assert event.output == {"a": 1}
    assert event.actions.state_delta.get("k") == "v"
    assert event.actions.route == "BRANCH"
    assert event.content is not None and event.content.parts


def test_state_is_not_a_dict():
    """ADK's State exposes a narrow surface; `dict(state)` raises.

    core/state.py only ever uses these methods.
    """
    from google.adk.sessions.state import State

    for method in ("get", "setdefault", "update", "to_dict", "has_delta"):
        assert hasattr(State, method), method
    assert not hasattr(State, "items")
    assert not hasattr(State, "keys")


# ── Graph validation constraints the compiler must pre-empt ──────────────────


def _fn(name):
    from google.adk import Event
    from google.adk.agents.context import Context
    from google.adk.workflow import node

    async def impl(ctx: Context, node_input=None):
        return Event(output={"n": name})

    impl.__name__ = name
    return node(impl, name=name)


def test_duplicate_edge_is_rejected():
    """Two routes to the same target must be merged into one Edge(route=[...]).

    Compiler safeguard: app/compiler dedupes (from, to) pairs.
    """
    from google.adk import Workflow
    from google.adk.workflow import START

    router, target = _fn("router"), _fn("target")

    with pytest.raises(Exception, match="Duplicate edge"):
        Workflow(
            name="dup",
            edges=[(START, router), (router, {"A": target, "B": target})],
        )


def test_merged_route_list_is_accepted():
    """The dedupe fix itself: one Edge carrying several route values."""
    from google.adk import Workflow
    from google.adk.workflow import START, Edge

    router, target = _fn("router2"), _fn("target2")

    workflow = Workflow(
        name="dedup",
        edges=[
            (START, router),
            Edge(from_node=router, to_node=target, route=["A", "B"]),
        ],
    )
    assert workflow.name == "dedup"


def test_return_annotation_breaks_edge_validation():
    """`-> Event` under 'node_input' binding injects Event's own JSON schema.

    Generated nodes therefore carry no return annotation.  This test documents
    why, and fails if ADK ever stops inferring schemas that way.
    """
    from google.adk import Workflow
    from google.adk.workflow import START, node

    async def annotated(ctx: Context, node_input=None) -> Event:
        return Event(output={})

    async def plain(ctx: Context, node_input=None):
        return Event(output={})

    bad = node(annotated, name="annotated", parameter_binding="node_input")
    good = node(plain, name="plain", parameter_binding="node_input")

    with pytest.raises(Exception, match="Schema mismatch"):
        Workflow(name="schema", edges=[(START, bad, good)])


@pytest.mark.asyncio
async def test_single_terminal_node_is_required():
    """Fan-out that never reconverges builds, then fails during finalisation.

    Compiler safeguard: exactly one terminal node, and every fork joins.
    """
    from google.adk import Runner, Workflow
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import START
    from google.genai import types

    source, left, right = _fn("src"), _fn("left"), _fn("right")

    # Graph construction succeeds -- the constraint is only enforced at runtime.
    workflow = Workflow(
        name="fan", edges=[(START, source), (source, left), (source, right)]
    )

    sessions = InMemorySessionService()
    runner = Runner(app_name="t", node=workflow, session_service=sessions)
    await sessions.create_session(app_name="t", user_id="u", session_id="s")

    with pytest.raises(Exception, match="terminal"):
        async for _ in runner.run_async(
            user_id="u",
            session_id="s",
            new_message=types.Content(role="user", parts=[types.Part(text="{}")]),
        ):
            pass


# ── Runner / event behaviour the platform's run tracking depends on ──────────


@pytest.mark.asyncio
async def test_entry_node_receives_content_not_dict():
    """The first node after START gets a genai Content; later nodes get dicts.

    core/state.py::coerce_input exists for exactly this asymmetry.
    """
    from google.adk import Event, Runner, Workflow
    from google.adk.agents.context import Context
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import START, node
    from google.genai import types

    seen: list[str] = []

    async def first(ctx: Context, node_input=None):
        seen.append(type(node_input).__name__)
        return Event(output={"ok": True})

    async def second(ctx: Context, node_input=None):
        seen.append(type(node_input).__name__)
        return Event(message="done", output={"done": True})

    workflow = Workflow(
        name="entry",
        edges=[(START, node(first, name="first"), node(second, name="second"))],
    )

    sessions = InMemorySessionService()
    runner = Runner(app_name="t", node=workflow, session_service=sessions)
    await sessions.create_session(app_name="t", user_id="u", session_id="s")
    async for _ in runner.run_async(
        user_id="u",
        session_id="s",
        new_message=types.Content(role="user", parts=[types.Part(text="{}")]),
    ):
        pass

    assert seen == ["Content", "dict"]


@pytest.mark.asyncio
async def test_node_info_path_identifies_node_and_iteration():
    """`event.node_info.path` is how per-node canvas status is derived.

    Format: '<workflow>@1/<node>@<iteration>'.  The iteration suffix is what
    makes loop progress visible.
    """
    from google.adk import Event, Runner, Workflow
    from google.adk.agents.context import Context
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import START, node
    from google.genai import types

    async def body(ctx: Context, node_input=None):
        count = (ctx.state.get("i") or 0) + 1
        ctx.state["i"] = count
        return Event(output={"i": count})

    async def guard(ctx: Context, node_input=None):
        again = (ctx.state.get("i") or 0) < 3
        return Event(route="AGAIN" if again else "DONE", output=node_input)

    async def done(ctx: Context, node_input=None):
        return Event(message="done", output={"i": ctx.state.get("i")})

    body_n = node(body, name="body")
    guard_n = node(guard, name="guard")
    done_n = node(done, name="done")

    workflow = Workflow(
        name="loop",
        edges=[
            (START, body_n, guard_n),
            (guard_n, {"AGAIN": body_n, "DONE": done_n}),
        ],
    )

    sessions = InMemorySessionService()
    runner = Runner(app_name="t", node=workflow, session_service=sessions)
    await sessions.create_session(app_name="t", user_id="u", session_id="s")

    paths = []
    async for event in runner.run_async(
        user_id="u",
        session_id="s",
        new_message=types.Content(role="user", parts=[types.Part(text="{}")]),
    ):
        if event.node_info and event.node_info.path:
            paths.append(event.node_info.path)

    assert "loop@1/body@1" in paths
    assert "loop@1/body@3" in paths, paths
    assert paths[-1] == "loop@1/done@1"


@pytest.mark.asyncio
async def test_join_node_keys_results_by_predecessor():
    """MERGE compiles to JoinNode, whose successor input is keyed by node name."""
    from google.adk import Event, Runner, Workflow
    from google.adk.agents.context import Context
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import START, JoinNode, node
    from google.genai import types

    captured: dict = {}

    async def after(ctx: Context, node_input=None):
        captured.update(node_input or {})
        return Event(message="joined", output={"ok": True})

    left, right = _fn("left"), _fn("right")
    join = JoinNode(name="join")

    workflow = Workflow(
        name="join_wf",
        edges=[
            (START, left, join),
            (START, right, join),
            (join, node(after, name="after")),
        ],
    )

    sessions = InMemorySessionService()
    runner = Runner(app_name="t", node=workflow, session_service=sessions)
    await sessions.create_session(app_name="t", user_id="u", session_id="s")
    async for _ in runner.run_async(
        user_id="u",
        session_id="s",
        new_message=types.Content(role="user", parts=[types.Part(text="{}")]),
    ):
        pass

    assert set(captured) == {"left", "right"}


# ── A2A serving contract ─────────────────────────────────────────────────────


def test_to_a2a_accepts_a_workflow_and_the_kwargs_we_pass():
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    params = inspect.signature(to_a2a).parameters
    for kwarg in ("host", "port", "agent_card", "runner", "task_store", "lifespan"):
        assert kwarg in params, kwarg


def test_agent_card_still_has_the_0_3_x_fields():
    """a2a-sdk 1.x replaced these with protobuf `supported_interfaces`.

    Generated cards advertise `url` / `preferred_transport` / `protocol_version`,
    so this fails if the pin ever slips past 1.0.
    """
    from a2a.types import AgentCapabilities, AgentCard, AgentSkill

    card = AgentCard(
        capabilities=AgentCapabilities(),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        description="contract test",
        name="contract",
        url="http://localhost:8080",
        version="1.0.0",
        preferred_transport="JSONRPC",
        protocol_version="0.3.0",
        skills=[
            AgentSkill(id="s", name="s", description="s", tags=["t"]),
        ],
    )

    assert card.url == "http://localhost:8080"
    assert card.preferred_transport == "JSONRPC"
    assert card.protocol_version == "0.3.0"


def test_a2a_task_completes_only_when_the_terminal_event_carries_content():
    """The rule that forces END nodes to emit `Event(message=...)`.

    An event with only `output` contributes no parts, so the executor never
    promotes a status message to an artifact and the task stays `working`.
    """
    from a2a.types import AgentCapabilities, AgentCard, AgentSkill
    from google.adk import Event, Workflow
    from google.adk.a2a.utils.agent_to_a2a import to_a2a
    from google.adk.agents.context import Context
    from google.adk.workflow import START, node
    from starlette.testclient import TestClient

    def build(emit_content: bool):
        async def end(ctx: Context, node_input=None):
            payload = {"done": True}
            if emit_content:
                return Event(message=json.dumps(payload), output=payload)
            return Event(output=payload)

        workflow = Workflow(name="w", edges=[(START, node(end, name="end"))])
        card = AgentCard(
            capabilities=AgentCapabilities(),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            description="d",
            name="w",
            url="http://localhost:8080",
            version="1.0.0",
            preferred_transport="JSONRPC",
            protocol_version="0.3.0",
            skills=[AgentSkill(id="s", name="s", description="s", tags=["t"])],
        )
        return to_a2a(workflow, host="localhost", port=8080, agent_card=card)

    def send(app):
        with TestClient(app) as client:
            response = client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "parts": [{"kind": "text", "text": "{}"}],
                            "messageId": "m1",
                            "kind": "message",
                        },
                        "configuration": {"blocking": True},
                    },
                },
            )
        return response.json()["result"]

    with_content = send(build(True))
    assert with_content["status"]["state"] == "completed"
    assert with_content.get("artifacts")

    without_content = send(build(False))
    assert without_content["status"]["state"] == "working"
    assert not without_content.get("artifacts")
