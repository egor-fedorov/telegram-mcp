"""Opt-in public-channel discovery."""

from typing import Annotated

from mcp.server import MCPServer
from mcp_types import CallToolResult
from pydantic import StringConstraints

from telegram_mcp.adapters import wire
from telegram_mcp.adapters.mcp.errors import Expose
from telegram_mcp.adapters.mcp.params import READONLY, Limit
from telegram_mcp.application.service import Service


def register(server: MCPServer[None], service: Service, expose: Expose) -> None:
    @server.tool(annotations=READONLY)
    @expose
    async def search_public_channels(
        query: Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
        ],
        limit: Limit = 20,
    ) -> Annotated[CallToolResult, wire.DiscoveryResult]:
        """Find public broadcast channels by a trimmed query of 1..1000 characters.

        Requires allow_public_channel_discovery. Returns channel metadata, not posts;
        reading selected channels requires separate retrieval permission. Results preserve
        Telegram order, are not exhaustive, and have no cursor. Filtering can return fewer
        than limit. Unknown participant counts are omitted. On temporary failure, retry this call
        after any reported wait.
        """
        return wire.encode(await service.search_public_channels(query, limit))

    @server.tool(annotations=READONLY)
    @expose
    async def get_similar_channels(
        chat_ref: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)],
        limit: Limit = 20,
    ) -> Annotated[CallToolResult, wire.DiscoveryResult]:
        """Find similar public channels for a required public broadcast source.

        chat_ref accepts a numeric ID, @username or t.me[/s]/username URL; an unknown ID
        may need a username or link. Requires allow_public_channel_discovery independently
        of retrieval permission. Returns bounded metadata with no cursor or completeness
        guarantee. telegram_total_count, when supplied, describes Telegram's response;
        locally_truncated reports the local limit separately. Never requests personalized
        recommendations without a source. On temporary failure, retry this call after any
        reported wait.
        """
        return wire.encode(await service.get_similar_channels(chat_ref, limit))
