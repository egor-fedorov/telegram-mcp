"""Authorization, connection and policy status."""

from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp_types import CallToolResult

from telegram_mcp.adapters import wire
from telegram_mcp.adapters.mcp.confirmation import supports_confirmation
from telegram_mcp.adapters.mcp.errors import Expose
from telegram_mcp.adapters.mcp.params import READONLY
from telegram_mcp.adapters.version import get_build_info
from telegram_mcp.application.service import Service


def register(server: MCPServer[None], service: Service, expose: Expose) -> None:
    info = {**get_build_info(), "started_at": datetime.now(UTC)}

    @server.tool(annotations=READONLY)
    @expose
    async def get_status(ctx: Context[None, Any]) -> Annotated[CallToolResult, wire.Status]:
        """Check running build identity, fresh Telegram authorization and configured capabilities.

        authorized is true if confirmed, false if missing/revoked, or null if the check failed.
        error explains a failure; connected is reported independently.
        """
        return wire.encode(
            {
                **await service.get_status(),
                "confirmation_supported": supports_confirmation(ctx),
                "server": info,
                "connect": None,
            }
        )
