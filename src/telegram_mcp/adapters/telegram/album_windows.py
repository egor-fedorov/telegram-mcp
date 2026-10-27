"""One bounded raw history window for selected album retrieval."""

from datetime import datetime
from typing import Any

from telethon import functions, types, utils

from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import (
    MAX_TOPIC_ID,
    normalize_message,
    peer_id,
    valid_topic_id,
)
from telegram_mcp.adapters.telegram.page_issues import PageIssues
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import (
    HistoryWindow,
)
from telegram_mcp.domain.models import Chat, Message, MessageRef, ToolError

_PAGES = (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages)


class AlbumWindowReader:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats

    async def read(self, chat_id: str, *, anchor_id: int, limit: int, newer: bool) -> HistoryWindow:
        """Read one bounded history response without filling short or empty pages."""
        check_window(anchor_id, limit, newer)
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
        if not isinstance(response, _PAGES) or not all(
            isinstance(getattr(response, name, None), list)
            for name in ("messages", "users", "chats")
        ):
            raise ToolError(
                "ALBUM_SCAN_INCOMPLETE", "Telegram returned an unusable history window."
            )
        issues = PageIssues(chat_id, "ALBUM_SCAN_INCOMPLETE")
        raw_messages = response.messages
        if len(raw_messages) > limit:
            issues.incomplete("Telegram returned more messages than the requested history window.")
        entities = window_entities([*response.users, *response.chats], issues)
        positioned = window_positions(raw_messages[:limit], chat_id, anchor_id, newer, issues)
        output = window_messages(positioned, chat, chat_id, newer, entities, issues)
        # A short slice can omit restricted messages; it does not prove a history boundary.
        exhausted = not issues.items and (
            not raw_messages
            or isinstance(response, types.messages.Messages)
            and len(raw_messages) < limit
        )
        return HistoryWindow(output, min(len(raw_messages), limit), exhausted, issues.items)


def check_window(anchor_id: int, limit: int, newer: bool) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ToolError("INVALID_ARGUMENT", "limit must be an integer from 1 to 100.")
    if not valid_topic_id(anchor_id):
        raise ToolError("INVALID_ARGUMENT", "anchor_id must be a positive 32-bit integer.")
    if type(newer) is not bool:
        raise ToolError("INVALID_ARGUMENT", "newer must be a boolean.")


def window_entities(values: list[Any], issues: PageIssues) -> dict[int, Any]:
    entities: dict[int, Any] = {}
    for value in values:
        try:
            entities[utils.get_peer_id(value)] = value
        except (TypeError, ValueError):
            issues.incomplete("Telegram returned incomplete sender metadata.")
    return entities


def window_positions(
    raw_messages: list[Any], chat_id: str, anchor_id: int, newer: bool, issues: PageIssues
) -> dict[int, Any]:
    """Raw messages of this chat beyond the anchor; a real message replaces an empty one."""
    positioned: dict[int, Any] = {}
    for raw in raw_messages:
        mid = getattr(raw, "id", None)
        if not valid_topic_id(mid):
            issues.incomplete("Telegram returned a message without a usable position.")
            continue
        if not in_chat(raw, chat_id, issues):
            continue
        if mid <= anchor_id if newer else mid >= anchor_id:
            continue
        if not isinstance(raw, (types.Message, types.MessageService, types.MessageEmpty)):
            issues.incomplete("Telegram returned incomplete message metadata.")
            continue
        if mid not in positioned or isinstance(positioned[mid], types.MessageEmpty):
            positioned[mid] = raw
    return positioned


def in_chat(raw: Any, chat_id: str, issues: PageIssues) -> bool:
    raw_peer = getattr(raw, "peer_id", None)
    if raw_peer is None:
        if isinstance(raw, types.MessageEmpty):
            return True
        issues.incomplete("Telegram returned a message without a usable chat.")
        return False
    try:
        same_chat = peer_id(raw_peer) == chat_id
    except (TypeError, ValueError):
        same_chat = False
    if not same_chat:
        issues.incomplete("Telegram returned a message outside the requested chat.")
    return same_chat


def window_messages(
    positioned: dict[int, Any],
    chat: Chat,
    chat_id: str,
    newer: bool,
    entities: dict[int, Any],
    issues: PageIssues,
) -> list[Message]:
    output: list[Message] = []
    for mid in sorted(positioned, reverse=not newer):
        raw = positioned[mid]
        if isinstance(raw, types.MessageEmpty):
            issues.items.append(
                ToolError(
                    "MESSAGE_UNAVAILABLE",
                    "A scanned message is unavailable; its album membership is unknown.",
                ).issue(chat_id, MessageRef(chat_id=chat_id, message_id=mid))
            )
            continue
        if not isinstance(getattr(raw, "date", None), datetime):
            issues.incomplete("Telegram returned incomplete message metadata.")
            continue
        try:
            output.append(normalize_message(raw, chat, entities=entities))
        except (AttributeError, TypeError, ValueError):
            issues.incomplete("Telegram returned incomplete message metadata.")
    return output
