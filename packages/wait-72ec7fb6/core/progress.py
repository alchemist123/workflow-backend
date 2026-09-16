"""A line of progress per node, for whoever is driving this package.

The package answers over A2A, and A2A has nothing to say about nodes: with
streaming enabled a three-node graph emits `submitted`, `working`, `working` —
task transitions, not steps — and the metadata on those frames carries
`adk_app_name` / `adk_user_id` / `adk_session_id` / `adk_invocation_id` and no
node path at all. ADK's converter also only emits an A2A event for an ADK event
that carries `content`, and only the terminal node emits content here. So a
caller watching the A2A stream cannot tell which node is running, and making it
able to would mean changing what the agent returns to every real caller.

This is the other channel: one JSON object per line on stderr, each prefixed so
it can be told apart from log output. stderr rather than stdout because stdout
carries the run's JSON envelope, and a prefix rather than a log record because
the platform runs this process at `LOG_LEVEL=WARNING` and progress must not
depend on log configuration.

Nothing reads it unless something is watching, and a closed or unwritable
stream is never allowed to fail a run — this is telemetry, not the result.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

# Marks a progress line among ordinary stderr output.
PREFIX = "@@PROGRESS "

# Off unless the caller asks, so a plain `python run_once.py` stays readable.
ENABLED = os.environ.get("WORKFLOW_PROGRESS") == "1"


def emit(event: str, node: str, **fields: Any) -> None:
    """Report one step. Never raises, never blocks on anything that matters."""
    if not ENABLED:
        return
    try:
        line = json.dumps({"e": event, "node": node, "at": time.time(), **fields})
        sys.stderr.write(f"{PREFIX}{line}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - telemetry must not break the run
        pass
