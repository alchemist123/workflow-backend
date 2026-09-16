"""WAIT — Rate limit.

Generated from canvas node 'cooloff'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

import asyncio
import time

from google.adk import Event
from google.adk.agents.context import Context

from core.logging import node_logger
from nodes.base import NODE_KWARGS, flow_node

NODE_ID = "n_wait_2"
# Name this node saves its result under, for later nodes to read.
OUTPUT_VARIABLE = ""

WAIT_SECONDS = 1.0

log = node_logger(NODE_ID)


# The timeout is derived from the wait, not from the canvas policy: ADK kills a
# node that outlives its timeout (`NodeTimeoutError: Node '...' timed out after
# N seconds`), and a node whose whole job is to take a long time must not be
# killed for taking it.
@flow_node(name=NODE_ID, variable=OUTPUT_VARIABLE, **NODE_KWARGS(timeout=31))
async def n_wait_2(ctx: Context, node_input=None):
    data = node_input or {}

    # `asyncio.sleep` yields the event loop, so a parallel branch keeps running
    # while this one waits rather than being held up behind it.
    started = time.monotonic()
    log.info("waiting %.3gs", WAIT_SECONDS)
    if WAIT_SECONDS > 0:
        await asyncio.sleep(WAIT_SECONDS)
    elapsed = time.monotonic() - started
    log.info("waited %.3fs", elapsed)

    # The payload carries the *configured* wait, not the measured one. The
    # measured value differs by a millisecond or two between runs, which makes
    # the workflow's output irreproducible — and the package's own suite checks
    # that a blocking call and a polled call return the same result. The real
    # elapsed time is in the log above, where a difference is information
    # rather than noise.
    return Event(output={**data, "waited_seconds": WAIT_SECONDS})
