"""Tiny stdio MCP server used by tests: reports which environment variables it was started with."""

import json
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("env-echo")
WATCHED = ("ROBINHOOD_API_KEY", "ROBINHOOD_PRIVATE_KEY_BASE64", "RH_MAX_ORDER_NOTIONAL_USD", "RH_ENABLE_TRADING",
           "RH_ALLOWED_SYMBOLS", "RH_AUDIT_LOG", "RH_TIMEOUT_MS", "ANTHROPIC_API_KEY")


@mcp.tool(structured_output=False)
def get_account() -> str:
    return json.dumps({"buying_power": "123.45", "env": {k: os.environ.get(k) for k in WATCHED}})


if __name__ == "__main__":
    mcp.run()
