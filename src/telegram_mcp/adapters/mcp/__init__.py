"""The public MCP tool surface. Authentication is deliberately a separate CLI."""

from telegram_mcp.adapters.mcp.errors import expose_tool_errors
from telegram_mcp.adapters.mcp.server import ResourceServer, create_server

__all__ = ["ResourceServer", "create_server", "expose_tool_errors"]
