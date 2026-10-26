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
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import (
    HistoryWindow,
)
from telegram_mcp.domain.models import Issue, Message, MessageRef, ToolError


class AlbumWindowReader:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats

    async def read(self, chat_id: str, *, anchor_id: int, limit: int, newer: bool) -> HistoryWindow:
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
                output.append(normalize_message(raw, chat, entities=entities))
            except (AttributeError, TypeError, ValueError):
                incomplete("Telegram returned incomplete message metadata.")
        # A short slice can omit restricted messages; it does not prove a history boundary.
        exhausted = not issues and (
            not raw_messages
            or isinstance(response, types.messages.Messages)
            and len(raw_messages) < limit
        )
        return HistoryWindow(output, min(len(raw_messages), limit), exhausted, issues)
