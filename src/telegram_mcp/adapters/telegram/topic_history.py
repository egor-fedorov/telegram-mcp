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
from telegram_mcp.adapters.telegram.page_issues import PageIssues
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import (
    Batch,
)
from telegram_mcp.application.requests import (
    TopicRead,
)
from telegram_mcp.domain.models import Chat, Message, ToolError

_PAGES = (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages)


class TopicHistoryReader:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats

    async def read(self, chat_id: str, request: TopicRead, *, offset: int, limit: int) -> Batch:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "limit must be between 1 and 100.")
        if type(offset) is not int or not 0 <= offset <= MAX_TOPIC_ID:
            raise ToolError("INVALID_ARGUMENT", "Invalid message offset.")
        chat = await self.chats.resolve_chat(chat_id)
        if not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum.")
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_input_entity(int(chat_id)))
        oldest_first = request.filters.oldest_first
        if oldest_first and offset == MAX_TOPIC_ID:
            return Batch([], offset, True)
        rpc_request = topic_request(entity, request, offset=offset, limit=limit)
        response = await self.runtime.call(client(rpc_request))
        issues = PageIssues(chat_id, "PAGINATION_INCOMPLETE")
        if not isinstance(response, _PAGES):
            issues.incomplete("Telegram returned no usable message page.")
            return Batch([], offset, True, issues.items)
        if not response.messages:
            return Batch([], offset, True)
        positioned = advancing(response.messages, chat_id, offset, oldest_first, issues)
        if not positioned:
            issues.incomplete("Telegram returned a message page without an advancing position.")
            return Batch([], offset, True, issues.items)
        ordered = [positioned[mid] for mid in sorted(positioned, reverse=not oldest_first)]
        entities = {utils.get_peer_id(value): value for value in [*response.users, *response.chats]}
        output, exhausted = topic_messages(ordered, chat, request, entities, issues)
        return Batch(output, ordered[-1].id, exhausted, issues.items)


def topic_request(entity: Any, request: TopicRead, *, offset: int, limit: int) -> Any:
    """Pinned topic messages use search; ordinary topic history uses replies."""
    filters = request.filters
    oldest_first = filters.oldest_first
    offset_date = None
    offset_id = offset
    if oldest_first:
        offset_id = offset + 1
        if not offset and filters.date_from is not None:
            offset_id = 0
            offset_date = filters.date_from - timedelta(seconds=1)
    elif not offset and filters.date_to is not None:
        offset_date = filters.date_to + timedelta(seconds=1)
    if request.pinned_only:
        return functions.messages.SearchRequest(
            peer=entity,
            q="",
            top_msg_id=request.topic_id,
            filter=types.InputMessagesFilterPinned(),
            min_date=None,
            max_date=filters.date_to + timedelta(seconds=1)
            if filters.date_to is not None
            else None,
            offset_id=offset + 1 if oldest_first else offset,
            add_offset=-limit if oldest_first else 0,
            limit=limit,
            max_id=0,
            min_id=0,
            hash=0,
        )
    return functions.messages.GetRepliesRequest(
        peer=entity,
        msg_id=request.topic_id,
        offset_id=offset_id,
        offset_date=offset_date,
        add_offset=-limit if oldest_first else 0,
        limit=limit,
        max_id=0,
        min_id=0,
        hash=0,
    )


def advancing(
    raw_messages: list[Any], chat_id: str, offset: int, oldest_first: bool, issues: PageIssues
) -> dict[int, Any]:
    """Raw messages of this chat that move past the offset, keyed by message ID."""
    positioned: dict[int, Any] = {}
    for raw in raw_messages:
        mid = getattr(raw, "id", None)
        if not valid_topic_id(mid):
            issues.incomplete("Telegram returned a message without a usable position.")
            continue
        raw_peer = getattr(raw, "peer_id", None)
        if raw_peer is not None and peer_id(raw_peer) != chat_id:
            issues.incomplete("Telegram returned a message outside the requested chat.")
            continue
        if raw_peer is None and not isinstance(raw, types.MessageEmpty):
            issues.incomplete("Telegram returned a message without a usable chat.")
            continue
        if offset and (mid <= offset if oldest_first else mid >= offset):
            continue
        positioned.setdefault(mid, raw)
    return positioned


def topic_messages(
    ordered: list[Any],
    chat: Chat,
    request: TopicRead,
    entities: dict[int, Any],
    issues: PageIssues,
) -> tuple[list[Message], bool]:
    """Messages of the requested topic and period; True once the period is passed."""
    filters = request.filters
    exhausted = False
    output: list[Message] = []
    for raw in ordered:
        if isinstance(raw, types.MessageEmpty):
            continue
        if not isinstance(raw, (types.Message, types.MessageService)) or not isinstance(
            getattr(raw, "date", None), datetime
        ):
            issues.incomplete("Telegram returned incomplete message metadata.")
            continue
        if message_topic_id(raw, chat) != request.topic_id:
            issues.incomplete("Telegram returned a message outside the requested topic.")
            continue
        sent_at = utc(raw.date)
        if filters.date_from is not None and sent_at < filters.date_from:
            exhausted = exhausted or not filters.oldest_first
            continue
        if filters.date_to is not None and sent_at >= filters.date_to:
            exhausted = exhausted or filters.oldest_first
            continue
        message = normalize_message(raw, chat, entities=entities)
        if matches_filters(message, filters):
            output.append(message)
    return output, exhausted
