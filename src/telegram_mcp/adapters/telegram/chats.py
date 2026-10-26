"""Entity resolution and chat metadata over a shared dialog catalog."""

from typing import Any

from telethon import functions, types

from telegram_mcp.adapters.telegram.catalog import DialogCatalog
from telegram_mcp.adapters.telegram.mapping import (
    chat_from_entity,
    peer_id,
    public_channel_from_entity,
)
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.models import Chat, Folder, ToolError
from telegram_mcp.domain.references import chat_target


class ChatGateway:
    def __init__(
        self,
        runtime: TelegramRuntime,
        *,
        clock: Clock,
        max_dialogs: int,
        cache: dict[str, Chat] | None = None,
        catalog_cache_ttl_seconds: float = 300,
        catalog_cache_stale_seconds: float = 300,
    ):
        self.runtime, self.clock = runtime, clock
        self.cache = cache if cache is not None else {}
        self.dialog_catalog = DialogCatalog(
            runtime,
            clock,
            max_dialogs,
            self.cache,
            ttl=catalog_cache_ttl_seconds,
            stale=catalog_cache_stale_seconds,
        )

    async def check_authorization(self) -> None:
        await self.dialog_catalog.check_authorization()

    async def catalog(self, *, refresh: bool = False) -> tuple[list[Chat], list[Folder]]:
        return await self.dialog_catalog.read(refresh=refresh)

    async def close(self) -> None:
        await self.dialog_catalog.close()

    async def resolve_entity(self, ref: str) -> Any:
        target = chat_target(ref)
        client = await self.runtime.ensure()
        try:
            return await self.runtime.call(client.get_entity(target))
        except ToolError as exc:
            if isinstance(target, int) and exc.code == "INVALID_REFERENCE":
                raise ToolError(
                    "INVALID_REFERENCE",
                    "Telegram has no entity for this numeric chat ID. Use its username or t.me "
                    "link first.",
                ) from None
            raise

    async def resolve_chat(self, ref: str, *, refresh: bool = False) -> Chat:
        if not refresh and ref.strip() in self.cache:
            return self.cache[ref.strip()]
        entity = await self.resolve_entity(ref)
        chat = chat_from_entity(entity, now=self.clock.utc_now())
        self.cache[chat.id] = chat
        return chat

    async def public_channel(self, chat_id: str) -> Chat:
        # get_entity performs a fresh GetChannels RPC for a known numeric entity;
        # the access hash may be cached, but public visibility must never be cached.
        entity = await self.resolve_entity(chat_id)
        if public_channel_from_entity(entity) is None:
            raise ToolError("ACCESS_DENIED", "This chat is not a public broadcast channel.")
        chat = chat_from_entity(entity, now=self.clock.utc_now())
        if chat.id != chat_id:
            raise ToolError("ACCESS_DENIED", "Telegram returned a different channel identity.")
        self.cache[chat.id] = chat
        return chat

    async def chat_details(self, chat: Chat) -> dict[str, object]:
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_input_entity(int(chat.id)))
        if chat.type in ("private", "bot", "saved"):
            full = await self.runtime.call(client(functions.users.GetFullUserRequest(entity)))
            about = getattr(full.full_user, "about", None)
            linked = None
        elif isinstance(entity, types.InputPeerChannel):
            full = await self.runtime.call(client(functions.channels.GetFullChannelRequest(entity)))
            about = full.full_chat.about
            linked = getattr(full.full_chat, "linked_chat_id", None)
        else:
            full = await self.runtime.call(
                client(functions.messages.GetFullChatRequest(abs(int(chat.id))))
            )
            about = full.full_chat.about
            linked = None
        return {
            "description": about,
            "linked_chat_id": peer_id(types.PeerChannel(linked)) if linked else None,
            "server_can_send": False,
        }

    async def discussion_chat(self, chat: Chat) -> str:
        if chat.type == "group":
            return chat.id
        if chat.type != "channel":
            raise ToolError(
                "THREAD_UNAVAILABLE",
                "Telegram threads are available in supported groups and channels.",
            )
        details = await self.chat_details(chat)
        linked = details["linked_chat_id"]
        if not isinstance(linked, str):
            raise ToolError("THREAD_UNAVAILABLE", "Channel has no linked discussion group.")
        return linked
