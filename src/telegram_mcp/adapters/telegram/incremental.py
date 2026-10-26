"""Bounded current observations using public Telethon history and batch APIs."""

import hashlib
from datetime import datetime, timedelta
from typing import Any

from telethon import types, utils

from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import normalize_message
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.change_ports import Observation, ObservationBatch
from telegram_mcp.domain.changes import content_revision
from telegram_mcp.domain.models import Chat, ToolError


def observation(raw: Any, chat: Chat) -> Observation:
    message = normalize_message(raw, chat)
    media = getattr(raw, "media", None)
    identity = next(
        (
            f"{kind}:{value.id}"
            for kind in ("photo", "document")
            if (value := getattr(media, kind, None)) is not None and hasattr(value, "id")
        ),
        "",
    )
    restricted = bool(
        getattr(raw, "noforwards", False)
        or getattr(raw, "ttl_period", None)
        or getattr(media, "ttl_seconds", None)
    )
    return Observation(message, content_revision(message, identity), not restricted)


class IncrementalGateway:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime, self.chats = runtime, chats

    async def checkpoint_binding(self) -> str:
        client = await self.runtime.ensure()
        auth_key = client.session.auth_key
        if auth_key is None or not auth_key.key:
            raise ToolError("AUTH_REQUIRED", "No authorized Telegram session is available.")
        # Bind snapshots to this authorization without storing the key itself.
        return hashlib.sha256(auth_key.key).hexdigest()

    async def recent_observations(
        self, chat_id: str, limit: int, *, before: datetime | None = None
    ) -> list[Observation]:
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(chat_id)
        kwargs: dict[str, Any] = {"limit": limit}
        if before is not None:
            kwargs["offset_date"] = before + timedelta(seconds=1)
        rows = await self.runtime.call(client.get_messages(int(chat_id), **kwargs))
        return [
            observation(raw, chat)
            for raw in rows
            if raw and not isinstance(raw, types.MessageEmpty)
        ]

    async def observe_messages(
        self, chat_id: str, message_ids: list[int]
    ) -> dict[int, Observation | None]:
        if not message_ids:
            return {}
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(chat_id)
        result: dict[int, Observation | None] = dict.fromkeys(message_ids)
        rows = await self.runtime.call(client.get_messages(int(chat_id), ids=list(result)))
        for raw in rows:
            if raw and not isinstance(raw, types.MessageEmpty) and raw.id in result:
                result[raw.id] = observation(raw, chat)
        return result

    async def read_incremental(
        self,
        chat_id: str,
        *,
        after_id: int,
        through_id: int,
        limit: int,
        date_from: datetime | None = None,
    ) -> ObservationBatch:
        if after_id >= through_id:
            return ObservationBatch([], after_id, True)
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(chat_id)
        kwargs: dict[str, Any] = {
            "offset_id": after_id,
            "max_id": through_id + 1,
            "reverse": True,
            "limit": limit,
        }
        if after_id == 0 and date_from is not None:
            kwargs["offset_date"] = date_from - timedelta(seconds=1)
        rows = await self.runtime.call(client.get_messages(int(chat_id), **kwargs))
        usable = [
            raw
            for raw in rows
            if raw and not isinstance(raw, types.MessageEmpty) and after_id < raw.id <= through_id
        ]
        usable.sort(key=lambda raw: raw.id)
        position = usable[-1].id if usable else after_id
        return ObservationBatch(
            [observation(raw, chat) for raw in usable],
            position,
            len(rows) < limit or position >= through_id,
        )

    async def check_export_source(self, chat_id: str) -> None:
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_entity(int(chat_id)))
        if (
            not isinstance(entity, (types.User, types.Chat, types.Channel))
            or str(utils.get_peer_id(entity)) != chat_id
        ):
            raise ToolError("SOURCE_UNAVAILABLE", "The export source is unavailable.")
        if getattr(entity, "noforwards", False):
            raise ToolError("EXPORT_PROTECTED", "Telegram restricts saving content from this chat.")
