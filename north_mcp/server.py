"""MCP server for Cohere North: SQL access scoped to the calling North user.

    python north_mcp/server.py

Built on Cohere's north-mcp-python-sdk, which authenticates the request and exposes the
user's identity. All the access logic is in guard.py.

Set NORTH_TRUSTED_ISSUERS (comma-separated) in any shared environment. Without it the
SDK decodes the ID token without checking its signature, and the email claim that
selects the database role could be forged.
"""

import os

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from north_mcp_python_sdk import NorthMCPServer

import guard

_issuers = [s.strip() for s in os.environ.get("NORTH_TRUSTED_ISSUERS", "").split(",") if s.strip()]
mcp = NorthMCPServer("Governed SQL", trusted_issuers=_issuers or None)


def _caller_email():
    token = get_access_token()
    return token.claims.get("email") if token else None


def _ok_or_tool_error(result):
    """Return a successful result. Raise for a denied or failed call, so the MCP result
    carries isError=True and a client does not have to parse the payload to know."""
    if result["status"] != "ok":
        raise ToolError(f"{result['status']}: {result['error']}")
    return result


@mcp.tool()
def describe_access() -> dict:
    """Show which tables the current user can query and how their access is scoped.
    Call this before writing SQL."""
    return _ok_or_tool_error(guard.describe_access(_caller_email()))


@mcp.tool()
def run_query(sql: str) -> dict:
    """Run one read-only PostgreSQL SELECT as the current user and return the rows.
    Rows the user is not entitled to are filtered out by the database."""
    return _ok_or_tool_error(guard.run_query(_caller_email(), sql))


if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=int(os.environ.get("PORT", "5222")))
