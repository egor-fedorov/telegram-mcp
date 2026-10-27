"""Thin application facade assembled from independently testable use cases."""

from datetime import datetime
from typing import Literal

from telegram_mcp.application.access import AccessFactory
from telegram_mcp.application.archive_ports import ArchivePort
from telegram_mcp.application.attachment_ports import (
    DOWNLOAD_TTL_SECONDS,
    MAX_ATTACHMENT_BYTES,
    MAX_DOWNLOAD_BYTES,
    MAX_DOWNLOAD_FILES,
    DownloadStorePort,
)
from telegram_mcp.application.attachments import AttachmentService
from telegram_mcp.application.catalog import CatalogService
from telegram_mcp.application.change_ports import CheckpointStorePort, ExportStorePort
from telegram_mcp.application.changes import ChangesService
from telegram_mcp.application.discovery import DiscoveryService
from telegram_mcp.application.global_search import GlobalSearchPager
from telegram_mcp.application.history_export import HistoryExportService
from telegram_mcp.application.incremental import IncrementalContext
from telegram_mcp.application.message_pager import MessagePager
from telegram_mcp.application.messages import MessageReader
from telegram_mcp.application.ports import (
    Backend,
    Clock,
    CursorPort,
    ExecutionLimits,
    FolderName,
    PolicyProvider,
    RuntimePort,
)
from telegram_mcp.application.progressive_search import ProgressivePager
from telegram_mcp.application.queries import QueryService
from telegram_mcp.application.query_preparation import QueryPreparation
from telegram_mcp.application.topics import TopicPager, TopicService
from telegram_mcp.application.watch_ports import WatchStorePort
from telegram_mcp.application.watches import WatchService
from telegram_mcp.domain.changes import (
    ChangeOutput,
    ChangesResult,
    ExportFormat,
    ExportHistoryResult,
)
from telegram_mcp.domain.models import (
    AlbumResult,
    ChatPage,
    ContentType,
    ContextResult,
    DiscoveryResult,
    DownloadResult,
    Folder,
    MessageRef,
    MessageResult,
    Page,
    TopicPage,
)
from telegram_mcp.domain.watches import (
    WatchAcknowledgement,
    WatchChangesResult,
    WatchDeletion,
    WatchInfo,
    WatchList,
)


class Service:
    def __init__(
        self,
        backend: Backend,
        policy_provider: PolicyProvider,
        limits: ExecutionLimits,
        cursors: CursorPort,
        clock: Clock,
        downloads: DownloadStorePort,
        checkpoints: CheckpointStorePort,
        exports: ExportStorePort,
        watches: WatchStorePort,
        archive: ArchivePort,
    ):
        self._runtime: RuntimePort = backend
        self._policies = policy_provider
        self._limits = limits
        self._clock = clock
        self._archive = archive
        access = AccessFactory(backend, policy_provider)
        self._incremental = IncrementalContext(
            backend=backend,
            chats=backend,
            access=access,
            checkpoints=checkpoints,
            exports=exports,
            clock=clock,
            limits=limits,
            archive=archive,
        )
        self._changes = ChangesService(self._incremental)
        self._watches = WatchService(self._incremental, watches)
        self._history_export = HistoryExportService(self._incremental)
        self._attachments = AttachmentService(
            backend, access, policy_provider, downloads, clock, archive
        )
        self._catalog = CatalogService(backend, access, cursors, clock)
        self._discovery = DiscoveryService(backend, policy_provider)
        preparation = QueryPreparation(backend, backend, access, cursors, clock)
        self._queries = QueryService(
            preparation,
            MessagePager(backend, cursors, clock, limits),
            GlobalSearchPager(backend, cursors, clock, limits),
            ProgressivePager(backend, cursors, clock, limits),
        )
        self._topics = TopicService(
            backend, access, cursors, clock, TopicPager(backend, cursors, clock)
        )
        self._messages = MessageReader(backend, backend, access, self._queries, limits)

    async def close(self) -> None:
        # Nested finally blocks close everything in order and chain every failure.
        try:
            await self._attachments.close()
        finally:
            try:
                await self._incremental.close()
            finally:
                try:
                    self._watches.store.close()
                finally:
                    try:
                        await self._archive.close()
                    finally:
                        await self._runtime.close()

    async def create_watch(
        self,
        name: str,
        chat_refs: list[str] | None = None,
        folder_ids: list[int] | None = None,
        date_from: datetime | None = None,
    ) -> WatchInfo:
        return await self._watches.create(name, chat_refs, folder_ids, date_from)

    async def list_watches(self) -> WatchList:
        return await self._watches.list()

    def now(self) -> datetime:
        """The injected current time, captured once per prompt rendering."""
        return self._clock.utc_now()

    async def find_folder(self, title: str) -> FolderName:
        return await self._catalog.find_folder(title)

    def watch_name_suggestions(self, prefix: str) -> list[str]:
        return self._watches.name_suggestions(prefix)

    async def get_watch_changes(
        self, name: str, limit: int = 30, ack_batch_id: str | None = None, save: bool = False
    ) -> WatchChangesResult:
        return await self._watches.changes(name, limit, ack_batch_id, save)

    async def ack_watch_changes(self, name: str, batch_id: str) -> WatchAcknowledgement:
        return await self._watches.acknowledge(name, batch_id)

    async def delete_watch(self, name: str) -> WatchDeletion:
        return await self._watches.delete(name)

    async def get_changes(
        self,
        chat_ref: str | None = None,
        checkpoint: str | None = None,
        limit: int = 30,
        output: ChangeOutput = "inline",
        save: bool = False,
    ) -> ChangesResult:
        return await self._changes.get_changes(chat_ref, checkpoint, limit, output, save)

    async def export_history(
        self,
        chat_ref: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        checkpoint: str | None = None,
        format: ExportFormat = "jsonl",
        save: bool = False,
    ) -> ExportHistoryResult:
        return await self._history_export.export_history(
            chat_ref, date_from, date_to, checkpoint, format, save
        )

    async def read_export_resource(self, uri: str) -> tuple[str, str]:
        return await self._incremental.read_resource(uri)

    async def download_attachment(self, attachment_id: str, save: bool = False) -> DownloadResult:
        return await self._attachments.download(attachment_id, save)

    async def read_download_resource(self, uri: str) -> tuple[bytes, str]:
        return await self._attachments.read_resource(uri)

    async def get_status(self) -> dict[str, object]:
        status = await self._runtime.status()
        policy = self._policies.current()
        return {
            **status,
            "mode": "read_only",
            "allow_all_chats": policy.allow_all_chats,
            "allow_public_channel_reads": policy.allow_public_channel_reads,
            "allow_public_channel_discovery": policy.allow_public_channel_discovery,
            "allow_attachment_downloads": policy.allow_attachment_downloads,
            "archive_enabled": self._archive.enabled,
            "attachment_max_bytes": MAX_ATTACHMENT_BYTES,
            "download_ttl_seconds": DOWNLOAD_TTL_SECONDS,
            "download_max_bytes": MAX_DOWNLOAD_BYTES,
            "download_max_files": MAX_DOWNLOAD_FILES,
            "allowed_chat_ids": list(policy.allowed_chat_ids),
            "allowed_folder_ids": list(policy.allowed_folder_ids),
            "cursor_ttl_seconds": self._limits.cursor_ttl_seconds,
        }

    async def list_folders(self, query: str | None = None) -> list[Folder]:
        return await self._catalog.list_folders(query=query)

    async def list_chats(
        self,
        query: str | None = None,
        chat_type: str | None = None,
        folder_ids: list[int] | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> ChatPage:
        return await self._catalog.list_chats(
            query=query, chat_type=chat_type, folder_ids=folder_ids, limit=limit, cursor=cursor
        )

    async def get_chat(self, chat_ref: str) -> dict[str, object]:
        return await self._catalog.get_chat(chat_ref=chat_ref)

    async def search_public_channels(self, query: str, limit: int = 20) -> DiscoveryResult:
        return await self._discovery.search_public_channels(query=query, limit=limit)

    async def get_similar_channels(self, chat_ref: str, limit: int = 20) -> DiscoveryResult:
        return await self._discovery.get_similar_channels(chat_ref=chat_ref, limit=limit)

    async def get_history(
        self,
        chat_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        order: Literal["newest_first", "oldest_first"] = "newest_first",
        limit: int = 30,
        cursor: str | None = None,
        chat_ref: str | None = None,
        topic_id: int | None = None,
        chat_ids: list[str] | None = None,
        folder_ids: list[int] | None = None,
        ordering: Literal["page", "global"] | None = None,
        max_sources: int | None = None,
        max_messages: int | None = None,
    ) -> Page:
        return await self._queries.get_history(
            chat_id=chat_id,
            date_from=date_from,
            date_to=date_to,
            order=order,
            limit=limit,
            cursor=cursor,
            chat_ref=chat_ref,
            topic_id=topic_id,
            chat_ids=chat_ids,
            folder_ids=folder_ids,
            ordering=ordering,
            max_sources=max_sources,
            max_messages=max_messages,
        )

    async def get_pinned_messages(
        self, chat_ref: str, limit: int = 30, cursor: str | None = None, topic_id: int | None = None
    ) -> Page:
        return await self._queries.get_pinned_messages(
            chat_ref=chat_ref, limit=limit, cursor=cursor, topic_id=topic_id
        )

    async def search_messages(
        self,
        *,
        query: str,
        chat_ids: list[str] | None = None,
        folder_ids: list[int] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sender_id: str | None = None,
        content_type: ContentType | None = None,
        has_link: bool | None = None,
        limit: int = 30,
        cursor: str | None = None,
        order: Literal["newest_first", "oldest_first"] = "newest_first",
        ordering: Literal["page", "global"] = "page",
    ) -> Page:
        return await self._queries.search_messages(
            query=query,
            chat_ids=chat_ids,
            folder_ids=folder_ids,
            date_from=date_from,
            date_to=date_to,
            sender_id=sender_id,
            content_type=content_type,
            has_link=has_link,
            limit=limit,
            cursor=cursor,
            order=order,
            ordering=ordering,
        )

    async def list_topics(
        self, chat_ref: str, query: str | None = None, limit: int = 30, cursor: str | None = None
    ) -> TopicPage:
        return await self._topics.list_topics(
            chat_ref=chat_ref, query=query, limit=limit, cursor=cursor
        )

    async def get_messages(
        self, refs: list[str | MessageRef], text_offset: int = 0, text_limit: int = 12000
    ) -> MessageResult:
        return await self._messages.get_messages(
            refs=refs, text_offset=text_offset, text_limit=text_limit
        )

    async def get_album(self, ref: str | MessageRef, scan_limit: int = 30) -> AlbumResult:
        return await self._messages.get_album(ref=ref, scan_limit=scan_limit)

    async def get_context(
        self, ref: str | MessageRef, before: int = 10, after: int = 10, reply_depth: int = 3
    ) -> ContextResult:
        return await self._messages.get_context(
            ref=ref, before=before, after=after, reply_depth=reply_depth
        )

    async def get_thread(
        self, ref: str | MessageRef, limit: int = 30, cursor: str | None = None
    ) -> Page:
        return await self._messages.get_thread(ref=ref, limit=limit, cursor=cursor)
