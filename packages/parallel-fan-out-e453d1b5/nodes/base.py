"""The contract every generated node module follows.

A generated node file is small and self-contained:

    from google.adk import Event
    from google.adk.agents.context import Context
    from google.adk.workflow import node

    from core.state import merge_state
    from nodes.base import NODE_KWARGS, node_error

    CONFIG = {...}                      # frozen from the canvas

    @node(name="n_transform_7", **NODE_KWARGS(timeout=60, retries=1))
    async def n_transform_7(ctx: Context, node_input=None):
        ...
        return Event(output=out)

Three ADK rules shape that shape, and they are the reason this module exists
rather than each node re-deriving them:

  * Parameter binding defaults to ``'state'``.  A parameter literally named
    ``node_input`` receives the upstream node's output verbatim; any other
    parameter would be looked up in ``ctx.state`` and raise if absent.
  * Nodes carry **no return annotation**.  Annotating ``-> Event`` makes ADK
    infer Event's own JSON schema as the node's ``output_schema``, and the graph
    then fails validation with "Schema mismatch on edge ...".
  * Only the terminal node emits ``content``.  See ``terminal_event``.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from google.adk import Event
from google.adk.workflow import RetryConfig, node

from core.state import as_text
from core.variables import set_variable

logger = logging.getLogger("workflow.node")


def flow_node(*, name: str, variable: str = "", **kwargs):
    """ADK's ``@node``, plus saving this node's result as a named variable.

    Wrapping the decorator rather than editing every ``return`` is deliberate:
    the node modules have 27 return sites between them (a router has three, a
    remote agent four), and a variable that is saved on some paths but not
    others is worse than no variable at all.

    The wrapper keeps the ``(ctx, node_input=None)`` signature every generated
    node uses, so ADK's parameter binding sees exactly what it saw before.
    """

    def decorate(func):
        @functools.wraps(func)
        async def wrapper(ctx, node_input=None):
            event = await func(ctx, node_input)
            if variable and isinstance(event, Event) and event.output is not None:
                set_variable(ctx, variable, event.output)
            return event

        # functools.wraps sets __wrapped__, which ADK unwraps when it reads the
        # signature -- so the node it builds is shaped by the original function.
        return node(name=name, **kwargs)(wrapper)

    return decorate


def NODE_KWARGS(  # noqa: N802 - reads as a constant at the decoration site
    *,
    timeout: float | None = None,
    retries: int = 1,
    rerun_on_resume: bool = False,
) -> dict[str, Any]:
    """Translate a canvas node's policies into `@node(...)` keyword arguments.

    `retries` is the canvas's total attempt count, so 1 means "no retry" and
    adds no RetryConfig at all.
    """
    kwargs: dict[str, Any] = {}
    if timeout and timeout > 0:
        kwargs["timeout"] = float(timeout)
    if retries and retries > 1:
        kwargs["retry_config"] = RetryConfig(max_attempts=int(retries))
    if rerun_on_resume:
        kwargs["rerun_on_resume"] = True
    return kwargs


def node_error(node_name: str, exc: BaseException) -> Event:
    """A uniform error payload for a node whose `on_error` policy is `continue`.

    Downstream nodes can branch on `_error`; a router can send it to an error
    handle.  Nodes whose policy is `fail` simply let the exception propagate and
    the A2A task ends up `failed`.
    """
    logger.warning("node %s failed: %s", node_name, exc, exc_info=exc)
    return Event(
        output={
            "_error": True,
            "_error_node": node_name,
            "_error_type": type(exc).__name__,
            "error": str(exc),
        }
    )


def terminal_event(result: Any) -> Event:
    """The event a terminal (END) node must return.

    An A2A task only reaches `completed` if the aggregated status message has
    content parts: the executor promotes those parts to the result artifact and
    marks the task done.  An event carrying only `output` contributes no parts,
    so the task would sit at `working` forever with no artifacts.

    Emitting `message` here -- and only here -- means the artifact holds the
    workflow's result rather than a transcript of every node.
    """
    return Event(message=as_text(result), output=result)


def route_event(route: str, payload: Any = None) -> Event:
    """The event a router (CONDITION / LOOP guard) node must return.

    `route` is matched against the keys of the dict on the outgoing edge, with
    `DEFAULT_ROUTE` as the fallback.
    """
    return Event(route=route, output=payload)
