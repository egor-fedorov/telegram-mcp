"""Chat catalog, entity resolution, and related chat metadata."""

import re
from typing import Any
from urllib.parse import urlparse

from telethon import functions, types

from telegram_mcp.adapters.telegram.mapping import (
    chat_from_entity,
    folder_from_tl,
    peer_id,
    public_channel_from_entity,
    utc,
)
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.models import Chat, Folder, ToolError


class ChatGateway:
    def __init__(
        self,
        runtime: TelegramRuntime,
        *,
        clock: Clock,
        max_dialogs: int,
        cache: dict[str, Chat] | None = None,
    ):
        self.runtime = runtime
        self.clock = clock
        self.max_dialogs = max_dialogs
        self.cache = cache if cache is not None else {}

    async def catalog(self) -> tuple[list[Chat], list[Folder]]:
        client = await self.runtime.ensure()
        filters = await self.runtime.call(client(functions.messages.GetDialogFiltersRequest()))
        raw_folders = filters.filters if hasattr(filters, "filters") else filters
        dialogs = await self.runtime.call(
            client.get_dialogs(limit=self.max_dialogs + 1, ignore_migrated=True)
        )
        if len(dialogs) > self.max_dialogs:
            raise ToolError(
                "CATALOG_LIMIT", "Dialog catalog exceeds max_dialogs; increase it in local config."
            )
        self_id = next(
            (peer_id(d.entity) for d in dialogs if getattr(d.entity, "is_self", False)), None
        )
        if self_id is None and any(
            isinstance(peer, types.InputPeerSelf)
            for raw in raw_folders
            for field in ("include_peers", "pinned_peers", "exclude_peers")
            for peer in getattr(raw, field, [])
        ):
            self_id = peer_id(await self.runtime.call(client.get_me(input_peer=True)))
        folders = [
            folder_from_tl(f, i, self_id)
            for i, f in enumerate(raw_folders)
            if isinstance(f, (types.DialogFilter, types.DialogFilterChatlist))
        ]
        defaults: dict[str, bool] = {}
        if any(f.exclude_muted for f in folders):
            for name, peer in (
                ("channel", types.InputNotifyBroadcasts()),
                ("group", types.InputNotifyChats()),
                ("private", types.InputNotifyUsers()),
            ):
                notify = await self.runtime.call(
                    client(functions.account.GetNotifySettingsRequest(peer))
                )
                until = getattr(notify, "mute_until", None)
                defaults[name] = until is not None and utc(until) > self.clock.utc_now()
        chats: list[Chat] = []
        for dialog in dialogs:
            entity = dialog.entity
            category = (
                "private"
                if isinstance(entity, types.User)
                else "channel"
                if getattr(entity, "broadcast", False)
                else "group"
            )
            chats.append(
                chat_from_entity(
                    entity, dialog, defaults.get(category, False), now=self.clock.utc_now()
                )
            )
        self.cache = {c.id: c for c in chats}
        return chats, folders

    async def resolve_entity(self, ref: str) -> Any:
        value = ref.strip()
        if value.startswith(("t.me/", "telegram.me/", "www.t.me/")):
            value = "https://" + value
        if value.startswith(("https://", "http://")):
            url = urlparse(value)
            if url.hostname not in ("t.me", "telegram.me", "www.t.me") or url.username:
                raise ToolError(
                    "INVALID_REFERENCE", "Use a Telegram chat ID, username or t.me link."
                )
            value = url.path.strip("/")
            if value.startswith("s/"):
                value = value[2:]
            if "/" in value or value.startswith("+"):
                raise ToolError(
                    "INVALID_REFERENCE", "Invite links and message links are not chat references."
                )
        target: int | str = int(value) if re.fullmatch(r"-?[1-9]\d*", value) else value
        if isinstance(target, str) and not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}|me", target):
            raise ToolError("INVALID_REFERENCE", "Use a numeric chat ID or exact username.")
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
