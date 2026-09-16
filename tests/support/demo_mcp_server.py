"""A real MCP server, so the MCP_TOOL tests exercise a real call.

Small on purpose, and started as a subprocess by the fixture in
`tests/test_mcp_tool.py`. A mock would not have caught the two things that
actually broke: that a server can answer at a path the client did not guess,
and that a tool's result arrives as JSON *text* with no `structuredContent`.
"""

import sys

from mcp.server.fastmcp import FastMCP

# The port comes from argv, not the environment: mcp 1.30 builds its Settings
# when FastMCP is constructed and does not read FASTMCP_PORT, so an env var
# leaves every instance on the default 8000 and the second one fails to bind.
_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
# 127.0.0.1 for tests; pass 0.0.0.0 to reach it from a container.
_HOST = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"

mcp = FastMCP("demo", stateless_http=True, host=_HOST, port=_PORT)


@mcp.tool()
def price_quote(sku: str, quantity: int, currency: str = "EUR") -> dict:
    """Quote a price for a SKU and quantity."""
    unit = {"WIDGET": 25.0, "GIZMO": 9.5}.get(sku.upper(), 1.0)
    return {
        "sku": sku.upper(),
        "quantity": quantity,
        "unit_price": unit,
        "total": round(unit * quantity, 2),
        "currency": currency,
    }


@mcp.tool()
def stock_level(sku: str) -> dict:
    """How many of a SKU are in stock."""
    return {"sku": sku.upper(), "in_stock": 42}


@mcp.tool()
def always_fails(reason: str) -> dict:
    """A tool that raises, for testing the error path."""
    raise RuntimeError(reason)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
