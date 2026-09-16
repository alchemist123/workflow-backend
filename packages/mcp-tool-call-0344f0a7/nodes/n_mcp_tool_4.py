"""MCP_TOOL — Price Quote.

Generated from canvas node 'quote'. Do not edit by hand:
re-package from the platform after changing the canvas.
"""

from __future__ import annotations

from google.adk import Event
from google.adk.agents.context import Context

from core.config import settings
from core.logging import node_logger
from core.mapping import build as build_mapping
from core.state import merge_state
from core.variables import all_variables
from nodes.base import NODE_KWARGS, flow_node, node_error
from tools import mcp as mcp_tools

NODE_ID = "n_mcp_tool_4"
# Name this node saves its result under, for later nodes to read.
OUTPUT_VARIABLE = "quote"
ON_ERROR = "fail"

TOOL_NAME = "price_quote"
# One entry per tool argument, saying where its value comes from. Data, not
# code: there is no expression here, so an argument name cannot inject Python.
ARG_FIELDS: list[dict] = [{'name': 'sku', 'source': 'data.code', 'type': 'string', 'required': True}, {'name': 'quantity', 'source': 'data.units', 'type': 'integer', 'required': True}, {'name': 'currency', 'source': '', 'type': 'string', 'default': 'GBP'}]

# The URL and token come from the environment, with no literal fallback: an MCP
# URL can embed basic-auth credentials, and this file is committed to git. The
# canvas value is written into `.env` (gitignored) as this variable's default.
URL_ENV_KEY = "MCP_PRICE_QUOTE_URL"
TOKEN_ENV_KEY = "MCP_PRICE_QUOTE_TOKEN"

log = node_logger(NODE_ID)


def _headers() -> dict[str, str]:
    token = settings.env_value(TOKEN_ENV_KEY)
    return {"Authorization": f"Bearer {token}"} if token else {}


@flow_node(name=NODE_ID, variable=OUTPUT_VARIABLE, **NODE_KWARGS(timeout=60))
async def n_mcp_tool_4(ctx: Context, node_input=None):
    data = node_input or {}
    url = settings.env_value(URL_ENV_KEY)
    if not url:
        message = f"{NODE_ID}: no MCP server URL. Set {URL_ENV_KEY} in .env."
        if ON_ERROR != "continue":
            raise ValueError(message)
        return node_error(NODE_ID, ValueError(message))

    # Only the mapped arguments are sent. Forwarding the whole payload -- which
    # is what passthrough mode does -- fails on any MCP server that validates
    # its input schema strictly, because the payload almost always carries keys
    # from earlier nodes that the tool never declared.
    try:
        arguments = build_mapping(ARG_FIELDS, data, all_variables(ctx), where=NODE_ID)
    except Exception as exc:  # noqa: BLE001
        if ON_ERROR != "continue":
            raise
        return node_error(NODE_ID, exc)

    log.info("calling MCP tool %r with %d argument(s)", TOOL_NAME, len(arguments))
    try:
        result = await mcp_tools.call_tool(url, TOOL_NAME, arguments, _headers())
    except Exception as exc:  # noqa: BLE001
        if ON_ERROR != "continue":
            raise
        return node_error(NODE_ID, exc)

    # `call_tool` reports a tool-level failure as {"error": ...} rather than
    # raising, because an MCP error is a result the server chose to return.
    # Either way it becomes the same shape as any other failed node: raised, or
    # an `_error` payload a CONDITION can branch on.
    if isinstance(result, dict) and "error" in result:
        failure = RuntimeError(f"{NODE_ID}: {result['error']}")
        if ON_ERROR != "continue":
            raise failure
        return node_error(NODE_ID, failure)

    # Merged, so the next node reads the tool's own keys directly. A tool that
    # returns something other than an object is wrapped, since a payload has to
    # be a dict.
    out = dict(result) if isinstance(result, dict) else {"result": result}
    merge_state(ctx, out)
    return Event(output={**data, **out})
