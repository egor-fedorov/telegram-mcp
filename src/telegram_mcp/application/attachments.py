"""Explicit bounded downloads and authorization of temporary resource reads."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import cast

from telegram_mcp.application.access import AccessFactory
from telegram_mcp.application.archive_ports import ArchivePort
from telegram_mcp.application.attachment_ports import (
    DOWNLOAD_TIMEOUT_SECONDS,
    MAX_ATTACHMENT_BYTES,
    RESOURCE_PREFIX,
    AttachmentKind,
    AttachmentPort,
    DownloadStorePort,
)
from telegram_mcp.application.ports import Clock, PolicyProvider
from telegram_mcp.application.retry import RetryState
from telegram_mcp.domain.archiving import ArchiveSource
from telegram_mcp.domain.models import DownloadResult, MessageRef, ToolError

logger = logging.getLogger(__name__)


def parse_attachment_id(value: str) -> tuple[MessageRef, AttachmentKind]:
    match = re.fullmatch(r"(-?[1-9]\d{0,19}):([1-9]\d{0,9}):(photo|document)", value)
    if match is None or int(match[2]) > 2**31 - 1:
        raise ToolError(
            "INVALID_ARGUMENT", "Use an attachments[].id returned by a message-reading tool."
        )
    return MessageRef(chat_id=match[1], message_id=int(match[2])), cast(AttachmentKind, match[3])


class AttachmentService:
    def __init__(
        self,
        backend: AttachmentPort,
        access: AccessFactory,
        policies: PolicyProvider,
        store: DownloadStorePort,
        clock: Clock,
        archive: ArchivePort,
    ):
        self.backend = backend
        self.access = access
        self.policies = policies
        self.store = store
        self.clock = clock
        self.archive = archive
        self.retry = RetryState()
        self._lock = asyncio.Lock()
        self._closed = False

    def _enabled(self) -> None:
        if self._closed:
            raise ToolError("DOWNLOAD_UNAVAILABLE", "The download service has been closed.")
        if not self.policies.current().allow_attachment_downloads:
            raise ToolError(
                "DOWNLOADS_DISABLED", "Enable allow_attachment_downloads in local configuration."
            )

    async def _run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        self._enabled()
        try:
            await asyncio.wait_for(self._lock.acquire(), 5)
        except TimeoutError:
            raise ToolError(
                "SERVER_BUSY", "Another attachment request is running. Retry shortly.", 1
            ) from None
        try:
            self._enabled()
            if not self.retry.ready(self.clock):
                issue = self.retry.current_issue(self.clock)
                assert issue is not None
                raise ToolError(issue.code, issue.message, issue.retry_after_seconds)
            try:
                async with asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS):
                    result = await operation()
                self.retry.clear()
                return result
            except ToolError as exc:
                if exc.code == "RATE_LIMITED":
                    self.retry.record(exc, None, self.clock)
                raise
            except TimeoutError:
                raise ToolError(
                    "CONNECTION_ERROR", "Attachment request timed out. Retry from the beginning."
                ) from None
        finally:
            self._lock.release()

    async def _authorize(self, ref: MessageRef) -> None:
        self._enabled()
        context = await self.access.load(explicit=True)
        await context.allow_explicit(ref.chat_id)
        self._enabled()

    async def download(self, attachment_id: str, save: bool = False) -> DownloadResult:
        if save:
            self.archive.require_enabled()

        async def operation() -> DownloadResult:
            ref, kind = parse_attachment_id(attachment_id)
            await self._authorize(ref)
            source = await self.backend.prepare_attachment(ref, kind)
            if source.size is not None and source.size > MAX_ATTACHMENT_BYTES:
                raise ToolError("ATTACHMENT_TOO_LARGE", "Attachments are limited to 20 MiB.")
            writer = await self.store.reserve(source)
            committed = False
            delivered = False
            resource_token = None
            try:
                await self.backend.download_attachment(source, writer)
                self._enabled()
                stored = await self.store.commit(writer, source)
                committed = True
                resource_token = stored.result.resource_uri.removeprefix(RESOURCE_PREFIX)
                if save:
                    data = await self.store.read(
                        stored.result.resource_uri.removeprefix(RESOURCE_PREFIX)
                    )
                    stored.result.saved_file = await self.archive.save(
                        data,
                        stored.result.name,
                        stored.result.sha256,
                        mime_type=stored.result.mime_type,
                        source=ArchiveSource(
                            tool="download_attachment",
                            chat_id=ref.chat_id,
                            message_ref=ref,
                            url=(
                                f"https://t.me/c/{-int(ref.chat_id) - 10**12}/{ref.message_id}"
                                if int(ref.chat_id) < -(10**12)
                                else None
                            ),
                        ),
                    )
                delivered = True
                return stored.result
            finally:
                if not committed:
                    try:
                        await self.store.discard(writer)
                    except ToolError:
                        logger.warning("Partial download cleanup failed; cleanup will retry.")
                elif not delivered and resource_token is not None:
                    await self.store.discard_resource(resource_token)

        return await self._run(operation)

    async def read_resource(self, uri: str) -> tuple[bytes, str]:
        async def operation() -> tuple[bytes, str]:
            token = uri.removeprefix(RESOURCE_PREFIX)
            if not uri.startswith(RESOURCE_PREFIX) or not re.fullmatch(
                r"[\w-]{43}", token, re.ASCII
            ):
                raise ToolError("DOWNLOAD_UNAVAILABLE", "Unknown or expired download resource.")
            stored = await self.store.lookup(token)
            ref, kind = parse_attachment_id(stored.result.attachment_id)
            await self._authorize(ref)
            current = await self.backend.prepare_attachment(ref, kind)
            if current.identity != stored.identity:
                raise ToolError("ATTACHMENT_UNAVAILABLE", "The source attachment has changed.")
            self._enabled()
            return await self.store.read(token), stored.result.mime_type

        return await self._run(operation)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            await self.store.close()
