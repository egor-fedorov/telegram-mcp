"""Explicit extraction with fresh source authorization before every delivery."""

import asyncio
import re
from contextlib import suppress

from telegram_mcp.application.attachments import parse_attachment_id
from telegram_mcp.application.extraction_ports import (
    EXTRACTION_TIMEOUT_SECONDS,
    DocumentParserPort,
    ExtractionInputPort,
    ExtractionStorePort,
)
from telegram_mcp.domain.extraction import MAX_TEXT_LIMIT, ExtractionResult, text_slice
from telegram_mcp.domain.models import ToolError


class ExtractionService:
    def __init__(
        self, inputs: ExtractionInputPort, parser: DocumentParserPort, store: ExtractionStorePort
    ):
        self.inputs, self.parser, self.store = inputs, parser, store
        self._slot = asyncio.Lock()
        self._active: set[asyncio.Task[object]] = set()
        self._closed = False

    async def extract(
        self,
        attachment_id: str | None = None,
        extraction_id: str | None = None,
        text_offset: int = 0,
        text_limit: int = 12000,
    ) -> ExtractionResult:
        if (attachment_id is None) == (extraction_id is None):
            raise ToolError(
                "INVALID_ARGUMENT", "Supply exactly one attachment_id or extraction_id."
            )
        if text_offset < 0 or not 1 <= text_limit <= MAX_TEXT_LIMIT:
            raise ToolError("INVALID_ARGUMENT", "Invalid extraction text_offset or text_limit.")
        if attachment_id is not None:
            _, kind = parse_attachment_id(attachment_id)
            if text_offset != 0:
                raise ToolError("INVALID_ARGUMENT", "Use extraction_id to continue extracted text.")
            if kind != "document":
                raise ToolError("DOCUMENT_UNSUPPORTED", "Select a PDF or DOCX document attachment.")
        self._enabled()
        task = asyncio.current_task()
        assert task is not None
        self._active.add(task)
        waiting = False
        try:
            async with asyncio.timeout(EXTRACTION_TIMEOUT_SECONDS):
                if extraction_id is not None:
                    return await self._read(extraction_id, text_offset, text_limit)
                assert attachment_id is not None
                waiting = True
                async with self._slot:
                    waiting = False
                    self._enabled()
                    return await self._create(attachment_id, text_limit)
        except TimeoutError:
            if waiting:
                raise ToolError(
                    "SERVER_BUSY", "The extraction worker is busy. Retry shortly.", 1
                ) from None
            raise ToolError(
                "EXTRACTION_TIMEOUT", "Extraction exceeded its 90-second deadline."
            ) from None
        finally:
            self._active.discard(task)

    def _enabled(self) -> None:
        if self._closed:
            raise ToolError("EXTRACTION_EXPIRED", "The extraction service has closed.")
        if not self.parser.available:
            raise ToolError(
                "EXTRACTION_UNAVAILABLE",
                "Install telegram-context-mcp[extraction] in the session-owning server environment "
                "and restart the server.",
            )
        self.inputs.ensure_enabled()

    async def _create(self, attachment_id: str, limit: int) -> ExtractionResult:
        token = await self.store.reserve()
        delivered = False
        try:
            source, data = await self.inputs.extraction_input(attachment_id)
            document = await self.parser.extract(data)
            await self.inputs.revalidate(source)
            self._enabled()
            await self.store.publish(token, source, document)
            result = await self._result(token, 0, limit)
            delivered = True
            return result
        finally:
            if not delivered:
                await self.store.discard(token)

    async def _read(self, token: str, offset: int, limit: int) -> ExtractionResult:
        if not re.fullmatch(r"[\w-]{43}", token, re.ASCII):
            raise ToolError("EXTRACTION_EXPIRED", "Unknown or expired extraction.")
        stored = await self.store.lookup(token)
        await self.inputs.revalidate(stored.source)
        self._enabled()
        return await self._result(token, offset, limit)

    async def _result(self, token: str, offset: int, limit: int) -> ExtractionResult:
        stored = await self.store.lookup(token)
        document = await self.store.read(token)
        text, following, spans = text_slice(document, offset, limit)
        source = stored.source.result
        return ExtractionResult(
            extraction_id=token,
            attachment_id=source.attachment_id,
            message_ref=source.message_ref,
            name=source.name,
            format=document.format,
            source_sha256=source.sha256,
            text=text,
            text_offset=offset,
            text_total_length=len(document.text),
            next_text_offset=following,
            page_count=document.page_count,
            page_spans=spans,
            expires_at=stored.expires_at,
            issues=document.issues,
        )

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._active)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        try:
            await self.parser.close()
        finally:
            await self.store.close()
