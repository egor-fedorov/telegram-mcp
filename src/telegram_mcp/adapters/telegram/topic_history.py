"""Selected-topic history and pins retain their own sparse-page semantics."""

from datetime import datetime, timedelta
from typing import Any

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
from telegram_mcp.adapters.telegram.message_filters import matches_filters
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import (
    Batch,
)
from telegram_mcp.application.requests import (
    TopicRead,
)
from telegram_mcp.domain.models import Issue, Message, ToolError


class TopicHistoryReader:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats

    async def read(  # noqa: C901
        self, chat_id: str, request: TopicRead, *, offset: int, limit: int
    ) -> Batch:
        topic_id = request.topic_id
        pinned_only = request.pinned_only
        filters = request.filters
        date_from, date_to = filters.date_from, filters.date_to
        oldest_first = filters.oldest_first
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
            message = normalize_message(raw, chat, entities=entities)
            if matches_filters(message, filters):
                output.append(message)
        return Batch(output, next_offset, exhausted, issues)
