"""Explicit public channel discovery without reading messages."""

from telethon import functions, utils

from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import peer_id, public_channel_from_entity
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import DiscoveryBatch
from telegram_mcp.domain.models import PublicChannel, ToolError


class DiscoveryGateway:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats

    async def search_public_channels(self, query: str, limit: int) -> DiscoveryBatch:
        client = await self.runtime.ensure()
        response = await self.runtime.call(
            client(functions.contacts.SearchRequest(q=query, limit=limit, broadcasts=True))
        )
        entities = {peer_id(entity): entity for entity in response.chats}
        channels: list[PublicChannel] = []
        for peer in [*response.my_results, *response.results]:
            channel = public_channel_from_entity(entities.get(peer_id(peer)))
            if channel is not None:
                channels.append(channel)
        return DiscoveryBatch(channels)

    async def get_similar_channels(self, chat_ref: str) -> DiscoveryBatch:
        # Resolve fresh under discovery permission; no dialog or message access is needed.
        entity = await self.chats.resolve_entity(chat_ref)
        if public_channel_from_entity(entity) is None:
            raise ToolError("ACCESS_DENIED", "The source must be a public broadcast channel.")
        try:
            source = utils.get_input_channel(utils.get_input_peer(entity))
        except (TypeError, ValueError):
            raise ToolError(
                "INVALID_REFERENCE", "The source channel is unavailable. Use its username or link."
            ) from None
        client = await self.runtime.ensure()
        response = await self.runtime.call(
            client(functions.channels.GetChannelRecommendationsRequest(channel=source))
        )
        channels = [
            channel
            for entity in response.chats
            if (channel := public_channel_from_entity(entity)) is not None
        ]
        return DiscoveryBatch(channels, telegram_total_count=getattr(response, "count", None))
