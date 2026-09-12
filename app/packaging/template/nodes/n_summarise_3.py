"""FUNCTION -- extractive summary for the `long` branch.

Deliberately model-free so the template's test suite runs offline. A generated
FUNCTION node has this shape with the canvas's own body in `_run`.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context
from google.adk.workflow import node

from core.logging import node_logger
from core.state import merge_state
from nodes.base import NODE_KWARGS, node_error

NODE_ID = "n_summarise_3"

CONFIG: dict = {"max_sentences": 2, "on_error": "continue"}

log = node_logger(NODE_ID)


def _run(data: dict) -> dict:
    text = str(data.get("text", ""))
    sentences = [s.strip() for s in text.replace("!", ".").replace("?", ".").split(".")]
    kept = [s for s in sentences if s][: CONFIG["max_sentences"]]
    return {"summary": ". ".join(kept) + ("." if kept else "")}


@node(name=NODE_ID, **NODE_KWARGS(timeout=60, retries=2))
async def n_summarise_3(ctx: Context, node_input=None):
    data = node_input or {}
    try:
        result = _run(data)
    except Exception as exc:  # noqa: BLE001
        if CONFIG["on_error"] != "continue":
            raise
        return node_error(NODE_ID, exc)

    log.info("summarised to %d chars", len(result["summary"]))
    merge_state(ctx, result)
    return Event(output={**data, **result})
