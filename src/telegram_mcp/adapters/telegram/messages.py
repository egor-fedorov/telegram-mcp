"""Compose specialized readers alongside references, batches and ordinary history."""

import re
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from telethon import functions, types

from telegram_mcp.adapters.telegram.album_windows import AlbumWindowReader
from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import (
    normalize_message,
    peer_id,
    utc,
    valid_topic_id,
)
from telegram_mcp.adapters.telegram.message_search import MessageSearch
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.adapters.telegram.topic_history import TopicHistoryReader
from telegram_mcp.application.ports import (
    Batch,
    GlobalBatch,
    GlobalOffset,
    HistoryWindow,
    MessageOffset,
)
from telegram_mcp.application.requests import (
    HistoryRead,
    MessageRead,
    PinnedRead,
    SearchRead,
    ThreadRead,
    TopicRead,
)
from telegram_mcp.domain.models import Message, MessageRef, ToolError


class MessageGateway:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats
        self._search = MessageSearch(runtime, chats)
        self._topics = TopicHistoryReader(runtime, chats)
        self._albums = AlbumWindowReader(runtime, chats)

    async def read(
        self, chat_id: str, request: MessageRead, *, offset: MessageOffset = 0, limit: int = 30
    ) -> Batch:
        if isinstance(request, SearchRead):
            return await self._search.read(chat_id, request, offset=offset, limit=limit)
        if not isinstance(offset, int):
            raise ToolError("INVALID_CURSOR", "Search anchor cannot be used for this read.")
        if isinstance(request, TopicRead):
            if not valid_topic_id(request.topic_id):
                raise ToolError("INVALID_ARGUMENT", "topic_id must be a positive 32-bit integer.")
            return await self._topics.read(chat_id, request, offset=offset, limit=limit)
        return await self._read_history(chat_id, request, offset=offset, limit=limit)

    async def resolve_message(self, ref: str | MessageRef) -> MessageRef:
        if isinstance(ref, MessageRef):
            return ref
        if match := re.fullmatch(r"(-?[1-9]\d*):([1-9]\d*)", ref):
            return MessageRef(chat_id=match[1], message_id=int(match[2]))
        url = urlparse(ref)
        if (
            url.scheme not in ("https", "http")
            or url.hostname not in ("t.me", "telegram.me", "www.t.me")
            or url.username
        ):
            raise ToolError(
                "INVALID_REFERENCE",
                "Expected a t.me message link, chat_id:message_id, or MessageRef.",
            )
        parts = url.path.strip("/").split("/")
        if parts and parts[0] == "s":
            parts = parts[1:]
        if len(parts) in (3, 4) and parts[0] == "c" and all(p.isdigit() for p in parts[1:]):
            if int(parts[1]) <= 0:
                raise ToolError("INVALID_REFERENCE", "Channel IDs must be positive.")
            cid, mid = peer_id(types.PeerChannel(int(parts[1]))), int(parts[-1])
        elif len(parts) in (2, 3) and all(p.isdigit() for p in parts[1:]):
            chat = await self.chats.resolve_chat(parts[0])
            cid, mid = chat.id, int(parts[-1])
        else:
            raise ToolError("INVALID_REFERENCE", "Unsupported Telegram message link.")
        params = parse_qs(url.query)
        if "comment" in params:
            values = params["comment"]
            if len(values) != 1 or not values[0].isdigit() or int(values[0]) <= 0:
                raise ToolError("INVALID_REFERENCE", "Invalid comment ID.")
            cid = await self.chats.discussion_chat(await self.chats.resolve_chat(cid))
            mid = int(values[0])
        if mid <= 0:
            raise ToolError("INVALID_REFERENCE", "Message IDs must be positive.")
        return MessageRef(chat_id=cid, message_id=mid)

    async def message(self, ref: MessageRef) -> Message | None:
        return (await self.messages(ref.chat_id, [ref.message_id]))[ref.message_id]

    async def messages(self, chat_id: str, message_ids: list[int]) -> dict[int, Message | None]:
        unique_ids = list(dict.fromkeys(message_ids))
        result: dict[int, Message | None] = dict.fromkeys(unique_ids)
        if not unique_ids:
            return result
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(chat_id)
        raw_messages = await self.runtime.call(client.get_messages(int(chat_id), ids=unique_ids))
        for raw in raw_messages:
            if raw and not isinstance(raw, types.MessageEmpty) and raw.id in result:
                result[raw.id] = normalize_message(raw, chat)
        return result

    async def thread_root(self, ref: MessageRef, discussion_chat_id: str) -> MessageRef:
        if ref.chat_id == discussion_chat_id:
            return ref
        client = await self.runtime.ensure()
        response = await self.runtime.call(
            client(
                functions.messages.GetDiscussionMessageRequest(
                    peer=await self.runtime.call(client.get_input_entity(int(ref.chat_id))),
                    msg_id=ref.message_id,
                )
            )
        )
        for raw in response.messages:
            if peer_id(raw.peer_id) == discussion_chat_id:
                return MessageRef(chat_id=discussion_chat_id, message_id=raw.id)
        raise ToolError("THREAD_UNAVAILABLE", "No discussion root is available for this post.")

    async def _read_history(
        self,
        chat_id: str,
        request: HistoryRead | PinnedRead | ThreadRead,
        *,
        offset: int,
        limit: int,
    ) -> Batch:
        filters = request.filters
        date_from, date_to = filters.date_from, filters.date_to
        content_type, oldest_first = filters.content_type, filters.oldest_first
        pinned_only = isinstance(request, PinnedRead)
        thread_id = request.thread_id if isinstance(request, ThreadRead) else None
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(chat_id)
        kwargs: dict[str, Any] = {"limit": limit, "offset_id": offset, "reverse": oldest_first}
        if pinned_only:
            kwargs["filter"] = types.InputMessagesFilterPinned()
        if thread_id is not None:
            kwargs["reply_to"] = thread_id
        if offset == 0:
            if oldest_first and date_from:
                kwargs["offset_date"] = date_from - timedelta(seconds=1)
            elif not oldest_first and date_to:
                # Telegram timestamps are integer seconds; include then filter the boundary.
                kwargs["offset_date"] = date_to + timedelta(seconds=1)
        raw_messages = await self.runtime.call(client.get_messages(int(chat_id), **kwargs))
        usable = [m for m in raw_messages if m and not isinstance(m, types.MessageEmpty)]
        positioned = [m for m in raw_messages if m and getattr(m, "id", 0) > 0]
        next_offset = positioned[-1].id if positioned else offset
        exhausted = len(raw_messages) < limit
        output: list[Message] = []
        for raw in usable:
            sent_at = utc(raw.date)
            if date_from and sent_at < date_from:
                if not oldest_first:
                    exhausted = True
                continue
            if date_to and sent_at >= date_to:
                if oldest_first:
                    exhausted = True
                continue
            message = normalize_message(raw, chat)
            if content_type is None or message.content_type == content_type:
                output.append(message)
        return Batch(output, next_offset, exhausted)

    async def search_global(
        self, request: SearchRead, *, offset: GlobalOffset, limit: int = 30, chat_ids: list[str]
    ) -> GlobalBatch:
        return await self._search.search_global(
            request, offset=offset, limit=limit, chat_ids=chat_ids
        )

    async def history_window(
        self, chat_id: str, *, anchor_id: int, limit: int, newer: bool
    ) -> HistoryWindow:
        return await self._albums.read(chat_id, anchor_id=anchor_id, limit=limit, newer=newer)
