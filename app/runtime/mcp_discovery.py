"""Ask an MCP server what tools it has, for the canvas's tool picker.

The MCP_TOOL node calls one named tool on one server. Making a no-code user
type that name — and then type each of the tool's argument names by hand, with
no way to know whether they got them right until the workflow runs — is the
whole problem this module exists to remove. The config panel asks the server
instead, and what comes back drives a dropdown and an argument mapper.

The answer is also *stored on the node*, not just displayed. That matters: the
compiler can then check at build time that every required argument is mapped,
without reaching out to the network during a build.

Two deliberate properties:

* it never raises for a server problem. A wrong URL, a dead host or a refused
  token is information the panel should show, not a 500;
* it is the platform reaching out to a URL the user supplied, which is exactly
  what a no-code MCP builder has to do, but it does mean the backend can be
  pointed at hosts the browser cannot reach. The timeout is short and nothing
  about the response is executed — it is read as data and shown.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client as _sse_client

try:  # mcp 1.x
    from mcp.client.streamable_http import streamablehttp_client as _http_client
except ImportError:  # pragma: no cover - mcp 2.x renamed it
    from mcp.client.streamable_http import streamable_http_client as _http_client

logger = logging.getLogger(__name__)

# Short: someone is watching a spinner, and an MCP server that takes longer
# than this to list its tools will not be pleasant to call either.
DISCOVERY_TIMEOUT = 15.0

# Canvas field types, so the picker can pre-fill an argument row. Mirrors
# app/nodes/agent_io.FIELD_TYPES.
_JSON_TO_CANVAS = {
    "string": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}


def candidates(url: str) -> list[tuple[str, str]]:
    """The (transport, endpoint) pairs to try for a URL someone typed.

    Deliberately a copy of `candidates()` in the package template's
    `tools/mcp.py`, not an import of it: a generated package is standalone —
    its modules are named `tools`, `core`, `nodes`, which is precisely why the
    platform never imports one in-process. A test pins the two to the same
    answers instead, by running the template's copy inside a real package.

    A no-code user pastes whatever their MCP server's page showed them, which
    is a base URL as often as an endpoint, and SSE as often as streamable HTTP.
    """
    trimmed = (url or "").rstrip("/")
    if not trimmed:
        return []

    if trimmed.endswith("/sse"):
        return [("sse", trimmed)]
    if trimmed.endswith("/mcp"):
        return [("http", trimmed), ("sse", f"{trimmed.rsplit('/', 1)[0]}/sse")]
    return [("http", f"{trimmed}/mcp"), ("sse", f"{trimmed}/sse"), ("http", trimmed)]


@asynccontextmanager
async def connect(url: str, headers: dict | None = None, timeout: float = 15.0):
    """An initialised MCP session, trying each candidate endpoint in turn."""
    first_error: Exception | None = None
    for transport, endpoint in candidates(url):
        client = _sse_client if transport == "sse" else _http_client
        try:
            async with client(
                endpoint, headers=headers or None, timeout=timeout
            ) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session
                    return
        except GeneratorExit:  # pragma: no cover - the caller stopped early
            raise
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            if first_error is None:
                first_error = exc
            logger.debug("MCP %s at %s did not answer: %s", transport, endpoint, exc)

    raise first_error or ConnectionError(f"No MCP server answered at {url}.")


@dataclass
class ToolArgument:
    name: str
    type: str
    required: bool
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
        }


@dataclass
class DiscoveredTool:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    arguments: list[ToolArgument] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "arguments": [a.to_dict() for a in self.arguments],
        }


def arguments_from_schema(schema: dict | None) -> list[ToolArgument]:
    """Flatten an MCP tool's JSON Schema into rows the panel can render.

    Only the top level: an MCP tool's arguments are its top-level properties,
    and a nested object is mapped whole, from a source that produces an object.
    """
    properties = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    rows: list[ToolArgument] = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        declared = spec.get("type")
        if isinstance(declared, list):  # ["string", "null"] — take the real one
            declared = next((t for t in declared if t != "null"), None)
        rows.append(
            ToolArgument(
                name=name,
                type=_JSON_TO_CANVAS.get(declared or "", "string"),
                required=name in required,
                description=(spec.get("description") or "").strip(),
            )
        )
    return rows


async def list_tools(
    url: str, auth_token: str = "", timeout: float = DISCOVERY_TIMEOUT
) -> tuple[list[DiscoveredTool], str | None]:
    """The tools on an MCP server, and an error to show if there are none.

    Returns `(tools, error)` rather than raising: every failure here is
    something the person editing the node needs to read.
    """
    url = (url or "").strip()
    if not url:
        return [], "Enter the MCP server URL first."

    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

    try:
        async with asyncio.timeout(timeout):
            async with connect(url, headers, timeout) as session:
                listed = await session.list_tools()
    except asyncio.TimeoutError:
        return [], f"{url} did not answer within {int(timeout)}s."
    except Exception as exc:  # noqa: BLE001 - shown to the user, not raised
        logger.info("MCP discovery failed for %s: %s", url, exc)
        return [], _readable(url, exc)

    tools = []
    for tool in listed.tools:
        schema = getattr(tool, "inputSchema", None) or getattr(
            tool, "input_schema", None
        ) or {}
        tools.append(
            DiscoveredTool(
                name=tool.name,
                description=(tool.description or "").strip(),
                input_schema=schema,
                arguments=arguments_from_schema(schema),
            )
        )

    if not tools:
        return [], f"{url} answered, but it publishes no tools."
    return tools, None


def _flatten(exc: BaseException) -> list[BaseException]:
    """Every leaf of an exception group, innermost first.

    anyio runs the transport in a task group, so a refused connection arrives
    as `unhandled errors in a TaskGroup (1 sub-exception)` — which is exactly
    what the Fetch tools button used to show someone who had mistyped a port.
    The useful error is inside.
    """
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for inner in exc.exceptions:
            leaves.extend(_flatten(inner))
        return leaves
    return [exc]


def _readable(url: str, exc: Exception) -> str:
    """Turn a transport exception into something worth showing in a panel."""
    leaves = _flatten(exc)
    # The outermost group's own message says nothing; a leaf says what failed.
    text = next(
        (str(leaf) for leaf in leaves if str(leaf).strip()),
        type(leaves[0]).__name__ if leaves else type(exc).__name__,
    )
    lowered = text.lower()
    if "nodename nor servname" in lowered or "name or service not known" in lowered:
        return f"That host does not resolve: {url}"
    if "session terminated" in lowered or "bad request" in lowered:
        return (
            f"{url} answered, but not as an MCP server. MCP endpoints are "
            "usually at /mcp or /sse — check the path."
        )
    if "401" in text or "unauthorized" in lowered:
        return "The server rejected the token. Check the auth token."
    if "403" in text or "forbidden" in lowered:
        return "The server refused the request (403)."
    if "404" in text:
        return (
            f"Nothing is listening at {url}. MCP servers are usually at "
            "/mcp or /sse — try the full endpoint URL."
        )
    if (
        "connect" in lowered
        or "refused" in lowered
        or "resolve" in lowered
        or "name or service" in lowered
        or any(isinstance(leaf, OSError) for leaf in leaves)
    ):
        return f"Could not reach {url}. Is the server running? ({text})"
    return f"Could not list tools: {text}"
