"""Capability ports and fixed budgets for optional document text extraction."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from telegram_mcp.application.attachment_ports import StoredAttachment
from telegram_mcp.domain.extraction import ExtractedDocument

EXTRACTION_TTL_SECONDS = 900
EXTRACTION_TIMEOUT_SECONDS = 90
PARSER_TIMEOUT_SECONDS = 30
MAX_EXTRACTION_BYTES = 64 * 1024 * 1024
MAX_EXTRACTION_FILES = 32
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_DOCX_BYTES = 64 * 1024 * 1024
MAX_DOCX_ENTRIES = 2048


@dataclass(frozen=True)
class StoredExtraction:
    source: StoredAttachment
    sha256: str
    expires_at: datetime


class ExtractionInputPort(Protocol):
    def ensure_enabled(self) -> None: ...
    async def extraction_input(self, attachment_id: str) -> tuple[StoredAttachment, bytes]: ...
    async def revalidate(self, source: StoredAttachment) -> None: ...


class DocumentParserPort(Protocol):
    @property
    def available(self) -> bool: ...
    async def extract(self, data: bytes) -> ExtractedDocument: ...
    async def close(self) -> None: ...


class ExtractionStorePort(Protocol):
    async def reserve(self) -> str: ...
    async def publish(
        self, token: str, source: StoredAttachment, document: ExtractedDocument
    ) -> StoredExtraction: ...
    async def lookup(self, token: str) -> StoredExtraction: ...
    async def read(self, token: str) -> ExtractedDocument: ...
    async def discard(self, token: str) -> None: ...
    async def close(self) -> None: ...
