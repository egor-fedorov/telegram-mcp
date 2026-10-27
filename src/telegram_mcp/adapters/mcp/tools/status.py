"""Authorization, connection and policy status."""

from typing import Annotated

from mcp.server import MCPServer
from mcp_types import CallToolResult

from telegram_mcp.adapters import wire
from telegram_mcp.adapters.mcp.errors import Expose
from telegram_mcp.adapters.mcp.params import READONLY
from telegram_mcp.application.service import Service


def register(server: MCPServer[None], service: Service, expose: Expose) -> None:
    @server.tool(annotations=READONLY)
    @expose
    async def get_status() -> Annotated[CallToolResult, wire.Status]:
        """Check fresh Telegram authorization, connection, discovery policy and read scope.

        authorized is true if confirmed, false if missing/revoked, or null if the check failed.
        error explains a failure; connected is reported independently.
        """
        return wire.encode(await service.get_status())
