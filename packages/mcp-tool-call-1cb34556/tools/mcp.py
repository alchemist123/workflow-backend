"""MCP servers exposed to agent nodes as ADK tools.

Each MCP tool becomes one `FunctionTool` whose call opens a short-lived session,
invokes the tool and closes.  That is deliberate: a long-lived session held
across an agent turn is fragile behind a load balancer, and MCP servers are
cheap to reconnect to.

`discover_tools` never raises.  A workflow whose MCP server is briefly down
still starts; the agent simply has fewer tools, and the failure is logged.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

# mcp 2.x renamed this client to `streamable_http_client`.  Accept either so a
# generated package keeps working across that boundary.
try:  # mcp 1.x
    from mcp.client.streamable_http import streamablehttp_client as _http_client
except ImportError:  # pragma: no cover - mcp 2.x
    from mcp.client.streamable_http import streamable_http_client as _http_client

from mcp import ClientSession
from mcp.client.sse import sse_client as _sse_client

from tools.signature import apply_schema_signature, safe_tool_name

logger = logging.getLogger("workflow.tools.mcp")

_CONNECT_TIMEOUT = 15
_CALL_TIMEOUT = 30


def candidates(url: str) -> list[tuple[str, str]]:
    """The (transport, endpoint) pairs to try for a URL someone typed.

    A no-code user pastes whatever their MCP server's page showed them, which
    is a base URL as often as an endpoint, and SSE as often as streamable
    HTTP. Blindly appending `/mcp` — which this used to do — turned a working
    `https://host/sse` into a 404 and reported it as "server unreachable".

    Ordered most-likely first, and an explicit path is always tried as given
    before anything is appended to it.
    """
    trimmed = (url or "").rstrip("/")
    if not trimmed:
        return []

    if trimmed.endswith("/sse"):
        return [("sse", trimmed)]
    if trimmed.endswith("/mcp"):
        return [("http", trimmed), ("sse", f"{trimmed.rsplit('/', 1)[0]}/sse")]
    # A bare host: try the two conventional paths, then the URL itself in case
    # the server is mounted at the root.
    return [("http", f"{trimmed}/mcp"), ("sse", f"{trimmed}/sse"), ("http", trimmed)]


def normalise_url(url: str) -> str:
    """The endpoint most likely to answer. Kept for callers that want one URL."""
    found = candidates(url)
    return found[0][1] if found else ""


@asynccontextmanager
async def _session(transport: str, endpoint: str, headers: dict | None, timeout: int):
    """An initialised MCP session over one transport."""
    client = _sse_client if transport == "sse" else _http_client
    async with client(endpoint, headers=headers or None, timeout=timeout) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# Which transport answered for a URL, so the second call to a server does not
# pay for the first one's guessing. Process-local and purely an optimisation.
_resolved: dict[str, tuple[str, str]] = {}


@asynccontextmanager
async def connect(url: str, headers: dict | None = None, timeout: int = _CALL_TIMEOUT):
    """Open a session to `url`, working out how to reach it.

    Raises the *first* transport's error when every candidate fails: it is the
    one the user most likely meant, so its message is the most useful.
    """
    known = _resolved.get(url)
    attempts = [known] if known else candidates(url)
    if not attempts:
        raise ValueError("No MCP server URL was given.")

    first_error: Exception | None = None
    for transport, endpoint in attempts:
        try:
            async with _session(transport, endpoint, headers, timeout) as session:
                _resolved[url] = (transport, endpoint)
                yield session
                return
        except GeneratorExit:  # pragma: no cover - the caller stopped early
            raise
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            if first_error is None:
                first_error = exc
            _resolved.pop(url, None)
            logger.debug("MCP %s at %s did not answer: %s", transport, endpoint, exc)

    raise first_error or ConnectionError(f"No MCP server answered at {url}.")


def _make_tool(server: dict[str, Any], tool: Any, url: str, headers: dict | None):
    from google.adk.tools import FunctionTool

    tool_name = tool.name
    description = tool.description or f"MCP tool {tool_name}"
    # The MCP server tells us exactly what this tool accepts. Without it the
    # wrapper would declare no parameters and the model could only ever call
    # the tool empty-handed -- see tools/signature.py.
    input_schema = getattr(tool, "inputSchema", None) or getattr(
        tool, "input_schema", None
    )

    async def call_mcp_tool(**kwargs) -> dict:
        try:
            async with connect(url, headers) as session:
                result = await session.call_tool(tool_name, arguments=kwargs or None)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            logger.warning("MCP call %s failed: %s", tool_name, exc)
            return {"error": str(exc)}

        return unwrap_result(result)

    call_mcp_tool.__name__ = safe_tool_name(server.get("name", "mcp"), tool_name)
    call_mcp_tool.__doc__ = description
    apply_schema_signature(call_mcp_tool, input_schema, fallback_param="request")
    return FunctionTool(call_mcp_tool)


def unwrap_result(result: Any) -> dict:
    """Turn an MCP CallToolResult into a plain dict.

    Three shapes, in order of how much the server told us:

    1. `structuredContent`, when the server sends it — already a dict.
    2. Text content that parses as JSON. Many servers return their result as
       a JSON string and no structured content at all (the stock
       `mcp.server.fastmcp` does), and leaving that as a string means the next
       node in the workflow reads `data["result"]` as text it cannot index —
       which defeats the point of passing a tool's output onward.
    3. Text, as text.
    """
    if getattr(result, "isError", False):
        texts = [c.text for c in result.content if hasattr(c, "text")]
        return {"error": "; ".join(texts) or "MCP tool returned isError=True"}

    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured

    texts = [c.text for c in result.content if hasattr(c, "text")]
    if not texts:
        return {"result": str(result.content)}

    joined = "\n".join(texts)
    try:
        parsed = json.loads(joined)
    except ValueError:
        return {"result": joined}
    if isinstance(parsed, dict):
        return parsed
    # A list or scalar is still a result, but a payload has to be a dict.
    return {"result": parsed}


async def call_tool(
    url: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    """Invoke one MCP tool directly -- used by TOOL / DATASOURCE graph nodes."""
    if not (url or "").strip():
        return {"error": "No MCP URL configured for this node."}

    async with connect(url, headers) as session:
        result = await session.call_tool(tool_name, arguments=arguments or None)
    return unwrap_result(result)


async def discover_tools(
    servers: list[dict[str, Any]], only: str | None = None
) -> list:
    """List the tools on every server and wrap each as an ADK FunctionTool.

    `only` restricts the result to one tool name, which is what a group child
    wants: it names a single MCP tool, not the whole server.
    """
    tools: list = []
    for server in servers or []:
        url = (server.get("url") or "").strip()
        if not url:
            logger.warning("MCP server %r has no URL; skipping.", server.get("name"))
            continue

        headers = server.get("headers") or server.get("auth") or {}
        endpoint = normalise_url(url)
        try:
            async with connect(url, headers, _CONNECT_TIMEOUT) as session:
                listed = await session.list_tools()
                for tool in listed.tools:
                    if only and tool.name != only:
                        continue
                    tools.append(_make_tool(server, tool, url, headers))
        except Exception as exc:  # noqa: BLE001 - a dead server must not block boot
            logger.warning(
                "Could not discover tools on MCP server %s (%s): %s",
                server.get("name"),
                endpoint,
                exc,
            )
    return tools
