"""Ports and bounded records for selected attachment downloads."""

from dataclasses import dataclass
from typing import Literal, Protocol

from telegram_mcp.domain.models import DownloadResult, MessageRef

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_DOWNLOAD_FILES = 32
DOWNLOAD_TTL_SECONDS = 900
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_SWEEP_SECONDS = 60
RESOURCE_PREFIX = "telegram-attachment://downloads/"

AttachmentKind = Literal["photo", "document"]


@dataclass(frozen=True)
class AttachmentSource:
    attachment_id: str
    message_ref: MessageRef
    kind: AttachmentKind
    identity: str
    name: str
    mime_type: str
    size: int | None
    payload: object = None


class AttachmentWriter(Protocol):
    def write(self, data: bytes, /) -> int: ...
    def seek(self, offset: int, whence: int = 0, /) -> int: ...
    def tell(self) -> int: ...
    def truncate(self, size: int | None = None, /) -> int: ...
    def flush(self) -> None: ...


class AttachmentPort(Protocol):
    async def prepare_attachment(
        self, ref: MessageRef, kind: AttachmentKind
    ) -> AttachmentSource: ...
    async def download_attachment(
        self, source: AttachmentSource, writer: AttachmentWriter
    ) -> None: ...


@dataclass(frozen=True)
class StoredAttachment:
    result: DownloadResult
    identity: str


class DownloadStorePort(Protocol):
    async def reserve(self, source: AttachmentSource) -> AttachmentWriter: ...
    async def commit(
        self, writer: AttachmentWriter, source: AttachmentSource
    ) -> StoredAttachment: ...
    async def discard(self, writer: AttachmentWriter) -> None: ...
    async def lookup(self, token: str) -> StoredAttachment: ...
    async def read(self, token: str) -> bytes: ...
    async def close(self) -> None: ...
