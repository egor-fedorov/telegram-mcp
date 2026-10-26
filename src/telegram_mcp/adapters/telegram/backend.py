"""Compose Telegram gateways over one owned runtime and entity catalog."""

from datetime import datetime

from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram.attachments import AttachmentGateway
from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.discovery import DiscoveryGateway
from telegram_mcp.adapters.telegram.forums import ForumGateway
from telegram_mcp.adapters.telegram.incremental import IncrementalGateway
from telegram_mcp.adapters.telegram.messages import MessageGateway
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.attachment_ports import (
    AttachmentKind,
    AttachmentSource,
    AttachmentWriter,
)
from telegram_mcp.application.change_ports import Observation, ObservationBatch, PeerState
from telegram_mcp.application.ports import (
    Batch,
    Clock,
    DiscoveryBatch,
    GlobalBatch,
    GlobalOffset,
    HistoryWindow,
    MessageOffset,
    TopicBatch,
    TopicOffset,
)
from telegram_mcp.application.requests import MessageRead, SearchRead
from telegram_mcp.domain.models import Chat, Folder, ForumTopic, Message, MessageRef


class TelegramBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        runtime: TelegramRuntime | None = None,
        chat_cache: dict[str, Chat] | None = None,
        clock: Clock | None = None,
    ):
        self.runtime = runtime if runtime is not None else TelegramRuntime(settings)
        self.chat_gateway = ChatGateway(
            self.runtime,
            clock=clock if clock is not None else SystemClock(),
            max_dialogs=settings.max_dialogs,
            cache=chat_cache,
            catalog_cache_ttl_seconds=settings.catalog_cache_ttl_seconds,
            catalog_cache_stale_seconds=settings.catalog_cache_stale_seconds,
        )
        self.discovery_gateway = DiscoveryGateway(self.runtime, self.chat_gateway)
        self.message_gateway = MessageGateway(self.runtime, self.chat_gateway)
        self.forum_gateway = ForumGateway(self.runtime, self.chat_gateway)
        self.attachment_gateway = AttachmentGateway(self.runtime)
        self.incremental_gateway = IncrementalGateway(self.runtime, self.chat_gateway)

    async def checkpoint_binding(self) -> str:
        return await self.incremental_gateway.checkpoint_binding()

    async def peer_states(self, chat_ids: list[str]) -> dict[str, PeerState]:
        return await self.incremental_gateway.peer_states(chat_ids)

    async def recent_observations(
        self, chat_id: str, limit: int, *, before: datetime | None = None
    ) -> list[Observation]:
        return await self.incremental_gateway.recent_observations(chat_id, limit, before=before)

    async def observe_messages(
        self, chat_id: str, message_ids: list[int]
    ) -> dict[int, Observation | None]:
        return await self.incremental_gateway.observe_messages(chat_id, message_ids)

    async def read_incremental(
        self,
        chat_id: str,
        *,
        after_id: int,
        through_id: int,
        limit: int,
        date_from: datetime | None = None,
    ) -> ObservationBatch:
        return await self.incremental_gateway.read_incremental(
            chat_id, after_id=after_id, through_id=through_id, limit=limit, date_from=date_from
        )

    async def check_export_source(self, chat_id: str) -> None:
        await self.incremental_gateway.check_export_source(chat_id)

    async def status(self) -> dict[str, object]:
        return await self.runtime.status()

    async def close(self) -> None:
        try:
            await self.chat_gateway.close()
        finally:
            await self.runtime.close()

    async def catalog(self) -> tuple[list[Chat], list[Folder]]:
        return await self.chat_gateway.catalog()

    async def check_authorization(self) -> None:
        await self.chat_gateway.check_authorization()

    async def resolve_chat(self, ref: str, *, refresh: bool = False) -> Chat:
        return await self.chat_gateway.resolve_chat(ref, refresh=refresh)

    async def public_channel(self, chat_id: str) -> Chat:
        return await self.chat_gateway.public_channel(chat_id)

    async def chat_details(self, chat: Chat) -> dict[str, object]:
        return await self.chat_gateway.chat_details(chat)

    async def search_public_channels(self, query: str, limit: int) -> DiscoveryBatch:
        return await self.discovery_gateway.search_public_channels(query, limit)

    async def get_similar_channels(self, chat_ref: str) -> DiscoveryBatch:
        return await self.discovery_gateway.get_similar_channels(chat_ref)

    async def fetch_topics(
        self, chat_id: str, *, offset: TopicOffset, limit: int = 30, query: str | None = None
    ) -> TopicBatch:
        return await self.forum_gateway.fetch_topics(
            chat_id, offset=offset, limit=limit, query=query
        )

    async def get_topic(self, chat_id: str, topic_id: int) -> ForumTopic:
        return await self.forum_gateway.get_topic(chat_id, topic_id)

    async def resolve_message(self, ref: str | MessageRef) -> MessageRef:
        return await self.message_gateway.resolve_message(ref)

    async def message(self, ref: MessageRef) -> Message | None:
        return await self.message_gateway.message(ref)

    async def messages(self, chat_id: str, message_ids: list[int]) -> dict[int, Message | None]:
        return await self.message_gateway.messages(chat_id, message_ids)

    async def prepare_attachment(self, ref: MessageRef, kind: AttachmentKind) -> AttachmentSource:
        return await self.attachment_gateway.prepare_attachment(ref, kind)

    async def download_attachment(self, source: AttachmentSource, writer: AttachmentWriter) -> None:
        await self.attachment_gateway.download_attachment(source, writer)

    async def history_window(
        self, chat_id: str, *, anchor_id: int, limit: int, newer: bool
    ) -> HistoryWindow:
        return await self.message_gateway.history_window(
            chat_id, anchor_id=anchor_id, limit=limit, newer=newer
        )

    async def discussion_chat(self, chat: Chat) -> str:
        return await self.chat_gateway.discussion_chat(chat)

    async def thread_root(self, ref: MessageRef, discussion_chat_id: str) -> MessageRef:
        return await self.message_gateway.thread_root(ref, discussion_chat_id)

    async def read(
        self, chat_id: str, request: MessageRead, *, offset: MessageOffset = 0, limit: int = 30
    ) -> Batch:
        return await self.message_gateway.read(chat_id, request, offset=offset, limit=limit)

    async def search_global(
        self, request: SearchRead, *, offset: GlobalOffset, limit: int, chat_ids: list[str]
    ) -> GlobalBatch:
        return await self.message_gateway.search_global(
            request, offset=offset, limit=limit, chat_ids=chat_ids
        )
