"""HUMAN_INPUT — Payment Details.

Generated from canvas node 'details'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context
from google.adk.events.request_input import RequestInput
from google.adk.workflow import node

from core.logging import node_logger
from core.state import merge_state
from nodes.base import NODE_KWARGS
NODE_ID = "n_human_input_3"

PROMPT = "Approved. Where should this be paid from, and when?"
# Hoisted rather than inlined: interpolating it inside an f-string expression
# nests the same quote style, which is a syntax error before Python 3.12.
DEFAULT_PROMPT = "Please provide the following."
ASSIGNEES: list = ['accounts@example.com']
# Advertised on the paused task so a client can build a form from it rather
# than guessing what to send back.
RESPONSE_SCHEMA: dict = {'type': 'object', 'properties': {'account': {'type': 'string', 'description': 'Account to pay from. Required.'}, 'pay_on': {'type': 'string', 'description': 'Date to pay (YYYY-MM-DD). Required.'}, 'note': {'type': 'string', 'description': 'Anything the payment run should know'}}}
# Fields the canvas marked required, enforced by this node rather than by the
# advertised schema. ADK validates that schema before the node runs again and a
# failure fails the whole run, so a blank field would destroy a workflow that
# had been waiting on a person. Asking again is the kinder answer.
REQUIRED_FIELDS: list = ['account', 'pay_on']

log = node_logger(NODE_ID)


def _interrupt_id(ctx: Context) -> str:
    """A stable id for this node's question, derived from its path in the graph.

    Deterministic on purpose. The answer comes back on
    `ctx.resume_inputs[interrupt_id]`, so a freshly generated id would never
    match on the second run and the node would ask again forever — the task
    would sit at `input-required` no matter how often the caller answered.
    ADK's own FunctionNode does the same for auth (`wf_auth:{ctx.node_path}`).
    """
    return f"input:{ctx.node_path}"


def _missing(answer: dict) -> list:
    """Required values this answer did not supply."""
    return [name for name in REQUIRED_FIELDS if answer.get(name) in (None, "")]


# `rerun_on_resume=True` is required: without it the node is marked complete
# when the run resumes and never reads the answer.
@node(name=NODE_ID, rerun_on_resume=True, **NODE_KWARGS(timeout=120))
async def n_human_input_3(ctx: Context, node_input=None):
    data = node_input or {}
    interrupt_id = _interrupt_id(ctx)
    answer = ctx.resume_inputs.get(interrupt_id)

    def _ask(message: str):
        """Park the task. ADK turns this into an interrupt event and `to_a2a`
        reports the A2A task as `input-required`."""
        return RequestInput(
            interrupt_id=interrupt_id,
            message=message,
            response_schema=RESPONSE_SCHEMA,
            # Everything the person needs in order to answer, plus who should
            # and which values this node insists on.
            payload={
                "assignees": ASSIGNEES,
                "required_fields": REQUIRED_FIELDS,
                "data": data,
            },
        )

    if answer is None:
        log.info("waiting for input from a human (%s)", interrupt_id)
        return _ask(PROMPT or DEFAULT_PROMPT)

    # Second pass: the caller answered.
    # The answer is data, not a decision. ADK has already checked it against
    # RESPONSE_SCHEMA, so anything required is present.
    if not isinstance(answer, dict):
        answer = {"value": answer}

    missing = _missing(answer)
    if missing:
        # Ask again rather than carrying on with holes in the payload. Reusing
        # the interrupt id is supported: ADK matches calls and responses by
        # count.
        log.info("answer is missing %s; asking again", ", ".join(missing))
        return _ask(
            f"{PROMPT or DEFAULT_PROMPT} "
            f"(still needed: {', '.join(missing)})"
        )

    log.info("human supplied %d value(s) (%s)", len(answer), interrupt_id)
    merge_state(ctx, answer)
    return Event(output={**data, **answer})
