"""Message reads with distinct history, forum, and bounded album algorithms."""

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from telethon import functions, types, utils

from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import (
    MAX_TOPIC_ID,
    message_topic_id,
    normalize_message,
    peer_id,
    utc,
    valid_topic_id,
)
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import Batch, HistoryWindow
from telegram_mcp.application.requests import (
    HistoryRead,
    MessageRead,
    PinnedRead,
    SearchRead,
    ThreadRead,
    TopicRead,
)
from telegram_mcp.domain.models import Issue, Message, MessageRef, ToolError


class MessageGateway:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats

    async def read(
        self, chat_id: str, request: MessageRead, *, offset: int = 0, limit: int = 30
    ) -> Batch:
        if isinstance(request, TopicRead):
            if not valid_topic_id(request.topic_id):
                raise ToolError("INVALID_ARGUMENT", "topic_id must be a positive 32-bit integer.")
            return await self._read_topic(chat_id, request, offset=offset, limit=limit)
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

    async def history_window(
        self, chat_id: str, *, anchor_id: int, limit: int, newer: bool
    ) -> HistoryWindow:
        """Read one bounded history response without filling short or empty pages."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "limit must be an integer from 1 to 100.")
        if not valid_topic_id(anchor_id):
            raise ToolError("INVALID_ARGUMENT", "anchor_id must be a positive 32-bit integer.")
        if type(newer) is not bool:
            raise ToolError("INVALID_ARGUMENT", "newer must be a boolean.")
        if (newer and anchor_id == MAX_TOPIC_ID) or (not newer and anchor_id == 1):
            return HistoryWindow([], 0, True)
        chat = await self.chats.resolve_chat(chat_id)
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_input_entity(int(chat_id)))
        response = await self.runtime.call(
            client(
                functions.messages.GetHistoryRequest(
                    peer=entity,
                    offset_id=anchor_id + 1 if newer else anchor_id,
                    offset_date=None,
                    add_offset=-limit if newer else 0,
                    limit=limit,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            )
        )
        if not isinstance(
            response,
            (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages),
        ) or not all(
            isinstance(getattr(response, name, None), list)
            for name in ("messages", "users", "chats")
        ):
            raise ToolError(
                "ALBUM_SCAN_INCOMPLETE", "Telegram returned an unusable history window."
            )
        issues: list[Issue] = []

        def incomplete(detail: str) -> None:
            issue = ToolError("ALBUM_SCAN_INCOMPLETE", detail).issue(chat_id)
            if issue not in issues:
                issues.append(issue)

        raw_messages = response.messages
        if len(raw_messages) > limit:
            incomplete("Telegram returned more messages than the requested history window.")
        entities: dict[int, Any] = {}
        for value in [*response.users, *response.chats]:
            try:
                entities[utils.get_peer_id(value)] = value
            except (TypeError, ValueError):
                incomplete("Telegram returned incomplete sender metadata.")
        positioned: dict[int, Any] = {}
        for raw in raw_messages[:limit]:
            mid = getattr(raw, "id", None)
            if not valid_topic_id(mid):
                incomplete("Telegram returned a message without a usable position.")
                continue
            raw_peer = getattr(raw, "peer_id", None)
            if raw_peer is not None:
                try:
                    same_chat = peer_id(raw_peer) == chat_id
                except (TypeError, ValueError):
                    same_chat = False
                if not same_chat:
                    incomplete("Telegram returned a message outside the requested chat.")
                    continue
            elif not isinstance(raw, types.MessageEmpty):
                incomplete("Telegram returned a message without a usable chat.")
                continue
            if mid <= anchor_id if newer else mid >= anchor_id:
                continue
            if not isinstance(raw, (types.Message, types.MessageService, types.MessageEmpty)):
                incomplete("Telegram returned incomplete message metadata.")
                continue
            if mid not in positioned or isinstance(positioned[mid], types.MessageEmpty):
                positioned[mid] = raw
        output: list[Message] = []
        for mid in sorted(positioned, reverse=not newer):
            raw = positioned[mid]
            if isinstance(raw, types.MessageEmpty):
                issues.append(
                    ToolError(
                        "MESSAGE_UNAVAILABLE",
                        "A scanned message is unavailable; its album membership is unknown.",
                    ).issue(chat_id, MessageRef(chat_id=chat_id, message_id=mid))
                )
                continue
            if not isinstance(getattr(raw, "date", None), datetime):
                incomplete("Telegram returned incomplete message metadata.")
                continue
            try:
                raw._finish_init(client, entities, entity)
                output.append(normalize_message(raw, chat))
            except (AttributeError, TypeError, ValueError):
                incomplete("Telegram returned incomplete message metadata.")
        # A short slice can omit restricted messages; it does not prove a history boundary.
        exhausted = not issues and (
            not raw_messages
            or isinstance(response, types.messages.Messages)
            and len(raw_messages) < limit
        )
        return HistoryWindow(output, min(len(raw_messages), limit), exhausted, issues)

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
        request: HistoryRead | SearchRead | PinnedRead | ThreadRead,
        *,
        offset: int,
        limit: int,
    ) -> Batch:
        filters = request.filters
        date_from, date_to = filters.date_from, filters.date_to
        content_type, oldest_first = filters.content_type, filters.oldest_first
        pinned_only = isinstance(request, PinnedRead)
        query = request.query if isinstance(request, SearchRead) else None
        sender_id = request.sender_id if isinstance(request, SearchRead) else None
        thread_id = request.thread_id if isinstance(request, ThreadRead) else None
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(chat_id)
        kwargs: dict[str, Any] = {"limit": limit, "offset_id": offset, "reverse": oldest_first}
        if pinned_only:
            kwargs["filter"] = types.InputMessagesFilterPinned()
        if query is not None:
            kwargs["search"] = query
        if sender_id is not None:
            kwargs["from_user"] = int(sender_id)
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

    async def _read_topic(
        self, chat_id: str, request: TopicRead, *, offset: int, limit: int
    ) -> Batch:
        topic_id = request.topic_id
        pinned_only = request.pinned_only
        filters = request.filters
        date_from, date_to = filters.date_from, filters.date_to
        content_type, oldest_first = filters.content_type, filters.oldest_first
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "limit must be between 1 and 100.")
        if type(offset) is not int or not 0 <= offset <= MAX_TOPIC_ID:
            raise ToolError("INVALID_ARGUMENT", "Invalid message offset.")
        chat = await self.chats.resolve_chat(chat_id)
        if not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum.")
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_input_entity(int(chat_id)))
        offset_date = None
        offset_id = offset
        if oldest_first:
            if offset == MAX_TOPIC_ID:
                return Batch([], offset, True)
            offset_id = offset + 1
            if not offset and date_from is not None:
                offset_id = 0
                offset_date = date_from - timedelta(seconds=1)
        elif not offset and date_to is not None:
            offset_date = date_to + timedelta(seconds=1)
        if pinned_only:
            rpc_request = functions.messages.SearchRequest(
                peer=entity,
                q="",
                top_msg_id=topic_id,
                filter=types.InputMessagesFilterPinned(),
                min_date=None,
                max_date=date_to + timedelta(seconds=1) if date_to is not None else None,
                offset_id=offset + 1 if oldest_first else offset,
                add_offset=-limit if oldest_first else 0,
                limit=limit,
                max_id=0,
                min_id=0,
                hash=0,
            )
        else:
            rpc_request = functions.messages.GetRepliesRequest(
                peer=entity,
                msg_id=topic_id,
                offset_id=offset_id,
                offset_date=offset_date,
                add_offset=-limit if oldest_first else 0,
                limit=limit,
                max_id=0,
                min_id=0,
                hash=0,
            )
        response = await self.runtime.call(client(rpc_request))
        issues: list[Issue] = []

        def incomplete(message: str) -> None:
            issue = ToolError("PAGINATION_INCOMPLETE", message).issue(chat_id)
            if issue not in issues:
                issues.append(issue)

        if not isinstance(
            response,
            (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages),
        ):
            incomplete("Telegram returned no usable message page.")
            return Batch([], offset, True, issues)
        raw_messages = response.messages
        if not raw_messages:
            return Batch([], offset, True)
        entities = {utils.get_peer_id(value): value for value in [*response.users, *response.chats]}
        positioned: dict[int, Any] = {}
        for raw in raw_messages:
            mid = getattr(raw, "id", None)
            if not valid_topic_id(mid):
                incomplete("Telegram returned a message without a usable position.")
                continue
            raw_peer = getattr(raw, "peer_id", None)
            if raw_peer is not None and peer_id(raw_peer) != chat_id:
                incomplete("Telegram returned a message outside the requested chat.")
                continue
            if raw_peer is None and not isinstance(raw, types.MessageEmpty):
                incomplete("Telegram returned a message without a usable chat.")
                continue
            if offset and (mid <= offset if oldest_first else mid >= offset):
                continue
            positioned.setdefault(mid, raw)
        if not positioned:
            incomplete("Telegram returned a message page without an advancing position.")
            return Batch([], offset, True, issues)
        ordered = [positioned[mid] for mid in sorted(positioned, reverse=not oldest_first)]
        next_offset = ordered[-1].id
        exhausted = False
        output: list[Message] = []
        for raw in ordered:
            if isinstance(raw, types.MessageEmpty):
                continue
            if not isinstance(raw, (types.Message, types.MessageService)) or not isinstance(
                getattr(raw, "date", None), datetime
            ):
                incomplete("Telegram returned incomplete message metadata.")
                continue
            if message_topic_id(raw, chat) != topic_id:
                incomplete("Telegram returned a message outside the requested topic.")
                continue
            sent_at = utc(raw.date)
            if date_from is not None and sent_at < date_from:
                if not oldest_first:
                    exhausted = True
                continue
            if date_to is not None and sent_at >= date_to:
                if oldest_first:
                    exhausted = True
                continue
            raw._finish_init(client, entities, entity)
            message = normalize_message(raw, chat)
            if content_type is None or message.content_type == content_type:
                output.append(message)
        return Batch(output, next_offset, exhausted, issues)
