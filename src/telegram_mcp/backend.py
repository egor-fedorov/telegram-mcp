"""Narrow, typed interface used by the service and offline tests."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from telegram_mcp.models import Chat, ContentType, Folder, Message, MessageRef, PublicChannel


@dataclass
class Batch:
    messages: list[Message]
    next_offset: int
    exhausted: bool


@dataclass
class DiscoveryBatch:
    channels: list[PublicChannel]
    telegram_total_count: int | None = None


class Backend(Protocol):
    async def status(self) -> dict[str, object]: ...
    async def close(self) -> None: ...
    async def catalog(self) -> tuple[list[Chat], list[Folder]]: ...
    async def resolve_chat(self, ref: str, *, refresh: bool = False) -> Chat: ...
    async def public_channel(self, chat_id: str) -> Chat: ...
    async def search_public_channels(self, query: str, limit: int) -> DiscoveryBatch: ...
    async def get_similar_channels(self, chat_ref: str) -> DiscoveryBatch: ...
    async def chat_details(self, chat: Chat) -> dict[str, object]: ...
    async def resolve_message(self, ref: str | MessageRef) -> MessageRef: ...
    async def message(self, ref: MessageRef) -> Message | None: ...
    async def messages(self, chat_id: str, message_ids: list[int]) -> dict[int, Message | None]: ...
    async def discussion_chat(self, chat: Chat) -> str: ...
    async def thread_root(self, ref: MessageRef, discussion_chat_id: str) -> MessageRef: ...
    async def fetch(
        self,
        chat_id: str,
        *,
        offset: int = 0,
        limit: int = 30,
        query: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sender_id: str | None = None,
        content_type: ContentType | None = None,
        oldest_first: bool = False,
        thread_id: int | None = None,
    ) -> Batch: ...
