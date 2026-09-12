"""TRANSFORM -- normalise the inbound text and measure it.

A generated TRANSFORM node looks like this: the canvas expression is emitted as
real module-level Python rather than a string handed to `exec`, so it is
readable in a diff, importable by the tests, and checked by `py_compile` at
build time.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import merge_state
from nodes.base import NODE_KWARGS, node_error

NODE_ID = "n_normalise_1"

CONFIG: dict = {
    "mode": "python",
    "on_error": "fail",
}

log = node_logger(NODE_ID)


@node(name=NODE_ID, **NODE_KWARGS(timeout=30))
async def n_normalise_1(ctx: Context, node_input=None):
    data = node_input or {}
    try:
        text = str(data.get("text", "")).strip()
        words = text.split()
        result = {
            "text": text,
            "word_count": len(words),
            "char_count": len(text),
        }
    except Exception as exc:  # noqa: BLE001 - policy is `fail`, so re-raise
        if CONFIG["on_error"] != "continue":
            raise
        return node_error(NODE_ID, exc)

    log.info("normalised %d words", result["word_count"])
    merge_state(ctx, result)
    return Event(output={**data, **result})
