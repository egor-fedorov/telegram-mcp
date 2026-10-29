"""Synthetic documents and isolated extraction contexts without a Telegram account."""

import asyncio
import io
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.downloads import DownloadStore
from telegram_mcp.adapters.extraction.store import ExtractionStore
from telegram_mcp.application.service import Service
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.extraction import ExtractedDocument
from telegram_mcp.domain.models import Chat, ToolError
from tests.support.downloads import AttachmentBackend
from tests.support.fakes import FakeClock


def pdf_bytes(pages: list[str | None], *, encrypted: bool = False) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=200, height=200)
        if text is not None:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                    NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            encoded = text.encode("cp1252").hex().encode()
            stream.set_data(b"BT /F1 12 Tf 10 100 Td <" + encoded + b"> Tj ET")
            page[NameObject("/Contents")] = stream
    if encrypted:
        writer.encrypt("test-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def docx_bytes(text: str = "Résumé 😀\nПривет 世界") -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


class FakeParser:
    def __init__(self) -> None:
        self.available = True
        self.document = ExtractedDocument(format="docx", text="Résumé 😀\nПривет 世界\n" * 4)
        self.calls = 0
        self.error: ToolError | None = None
        self.entered = asyncio.Event()
        self.resume: asyncio.Event | None = None
        self.closed = False

    async def extract(self, data: bytes) -> ExtractedDocument:
        self.calls += 1
        self.entered.set()
        if self.resume is not None:
            await self.resume.wait()
        if self.error is not None:
            raise self.error
        return self.document

    async def close(self) -> None:
        self.closed = True


def extraction_backend() -> AttachmentBackend:
    backend = AttachmentBackend()
    backend.chats = [Chat(id="1", title="Synthetic documents", type="group")]
    backend.data = docx_bytes()
    backend.source = replace(
        backend.source,
        size=len(backend.data),
        name="selected.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    return backend


def extraction_stdio_service(path: Path) -> Service:
    settings = Settings(
        allowed_chat_ids=["1"], allow_attachment_downloads=True, session_path=path / "session"
    )
    clock = SystemClock()
    return build_service(
        extraction_backend(),
        settings,
        clock=clock,
        downloads=DownloadStore(settings.session_path, clock, root=path / "downloads"),
        extractions=ExtractionStore(settings.session_path, clock, root=path / "extractions"),
    )


@asynccontextmanager
async def extraction_context(
    tmp_path: Path,
) -> AsyncGenerator[
    tuple[Service, AttachmentBackend, Settings, FakeParser, ExtractionStore, FakeClock]
]:
    clock, backend, parser = FakeClock(), AttachmentBackend(), FakeParser()
    backend.source = replace(backend.source, name="selected.docx")
    settings = Settings(
        allowed_chat_ids=["1"],
        allow_attachment_downloads=True,
        session_path=tmp_path / "account.session",
    )
    store = ExtractionStore(settings.session_path, clock, root=tmp_path / "extractions")
    service = build_service(
        backend,
        settings,
        clock=clock,
        document_parser=parser,
        extractions=store,
        downloads=DownloadStore(settings.session_path, clock, root=tmp_path / "downloads"),
    )
    try:
        yield service, backend, settings, parser, store, clock
    finally:
        await service.close()
