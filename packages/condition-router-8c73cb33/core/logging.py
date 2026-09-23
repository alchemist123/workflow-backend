"""Logging setup for a generated workflow agent.

Cloud Run reads structured JSON from stdout, so `json_logs=True` (the default
when running under Cloud Run) emits one JSON object per line with the severity
field Cloud Logging expects.  Locally it stays human-readable.
"""

from __future__ import annotations

import json
import logging
import os
import sys

_LEVEL_TO_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


class CloudLoggingFormatter(logging.Formatter):
    """One JSON object per line, keyed the way Cloud Logging expects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": _LEVEL_TO_SEVERITY.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        node = getattr(record, "node", None)
        if node:
            payload["node"] = node
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None, json_logs: bool | None = None) -> None:
    """Install a single stdout handler. Safe to call more than once."""
    resolved_level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    if json_logs is None:
        # K_SERVICE is set by Cloud Run.
        json_logs = bool(os.environ.get("K_SERVICE"))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        CloudLoggingFormatter()
        if json_logs
        else logging.Formatter("%(levelname)-8s %(name)s  %(message)s")
    )

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    # ADK is chatty at INFO once a graph starts stepping.
    logging.getLogger("google_adk").setLevel("WARNING")


def node_logger(node_name: str) -> logging.LoggerAdapter:
    """A logger that tags every record with the emitting node."""
    return logging.LoggerAdapter(
        logging.getLogger(f"workflow.node.{node_name}"), {"node": node_name}
    )
