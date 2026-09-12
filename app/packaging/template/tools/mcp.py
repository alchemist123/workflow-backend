"""MCP servers exposed to agent nodes as ADK tools.

Each MCP tool becomes one `FunctionTool` whose call opens a short-lived session,
invokes the tool and closes.  That is deliberate: a long-lived session held
across an agent turn is fragile behind a load balancer, and MCP servers are
cheap to reconnect to.

`discover_tools` never raises.  A workflow whose MCP server is briefly down
still starts; the agent simply has fewer tools, and the failure is logged.
"""

from __future__ import annotations

import logging
from typing import Any

# mcp 2.x renamed this client to `streamable_http_client`.  Accept either so a
# generated package keeps working across that boundary.
try:  # mcp 1.x
    from mcp.client.streamable_http import streamablehttp_client as _http_client
except ImportError:  # pragma: no cover - mcp 2.x
    from mcp.client.streamable_http import streamable_http_client as _http_client

from mcp import ClientSession

from tools.signature import apply_schema_signature, safe_tool_name

logger = logging.getLogger("workflow.tools.mcp")

_CONNECT_TIMEOUT = 15
_CALL_TIMEOUT = 30


def normalise_url(url: str) -> str:
    """MCP streamable-HTTP endpoints live at /mcp; accept a base URL too."""
    trimmed = (url or "").rstrip("/")
    if not trimmed:
        return ""
    return trimmed if trimmed.endswith("/mcp") else f"{trimmed}/mcp"


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
            async with _http_client(
                url, headers=headers or None, timeout=_CALL_TIMEOUT
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        tool_name, arguments=kwargs or None
                    )
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            logger.warning("MCP call %s failed: %s", tool_name, exc)
            return {"error": str(exc)}

        return unwrap_result(result)

    call_mcp_tool.__name__ = safe_tool_name(server.get("name", "mcp"), tool_name)
    call_mcp_tool.__doc__ = description
    apply_schema_signature(call_mcp_tool, input_schema, fallback_param="request")
    return FunctionTool(call_mcp_tool)


def unwrap_result(result: Any) -> dict:
    """Turn an MCP CallToolResult into a plain dict."""
    if getattr(result, "isError", False):
        texts = [c.text for c in result.content if hasattr(c, "text")]
        return {"error": "; ".join(texts) or "MCP tool returned isError=True"}

    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured

    texts = [c.text for c in result.content if hasattr(c, "text")]
    if texts:
        return {"result": "\n".join(texts)}
    return {"result": str(result.content)}


async def call_tool(
    url: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    """Invoke one MCP tool directly -- used by TOOL / DATASOURCE graph nodes."""
    endpoint = normalise_url(url)
    if not endpoint:
        return {"error": "No MCP URL configured for this node."}

    async with _http_client(
        endpoint, headers=headers or None, timeout=_CALL_TIMEOUT
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
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
        endpoint = normalise_url(server.get("url", ""))
        if not endpoint:
            logger.warning("MCP server %r has no URL; skipping.", server.get("name"))
            continue

        headers = server.get("headers") or server.get("auth") or {}
        try:
            async with _http_client(
                endpoint, headers=headers or None, timeout=_CONNECT_TIMEOUT
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    for tool in listed.tools:
                        if only and tool.name != only:
                            continue
                        tools.append(_make_tool(server, tool, endpoint, headers))
        except Exception as exc:  # noqa: BLE001 - a dead server must not block boot
            logger.warning(
                "Could not discover tools on MCP server %s (%s): %s",
                server.get("name"),
                endpoint,
                exc,
            )
    return tools
