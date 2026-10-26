import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.downloads import DownloadStore
from telegram_mcp.application.attachment_ports import (
    AttachmentKind,
    AttachmentSource,
    AttachmentWriter,
)
from telegram_mcp.application.service import Service
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import MessageRef, ToolError
from tests.support.fakes import FakeBackend, FakeClock


class AttachmentBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.data = b"Selected original attachment\x00\xff"
        self.source = AttachmentSource(
            "1:10:document",
            MessageRef(chat_id="1", message_id=10),
            "document",
            "doc:123",
            "report.bin",
            "application/octet-stream",
            len(self.data),
        )
        self.prepare_error: ToolError | None = None
        self.download_error: ToolError | None = None
        self.downloads = 0
        self.entered = asyncio.Event()
        self.resume: asyncio.Event | None = None

    async def prepare_attachment(self, ref: MessageRef, kind: AttachmentKind) -> AttachmentSource:
        self.backend_calls.append("prepare_attachment")
        if self.prepare_error:
            raise self.prepare_error
        return replace(self.source, attachment_id=f"{ref.key}:{kind}", message_ref=ref, kind=kind)

    async def download_attachment(self, source: AttachmentSource, writer: AttachmentWriter) -> None:
        self.backend_calls.append("download_attachment")
        self.downloads += 1
        writer.write(self.data)
        self.entered.set()
        if self.resume is not None:
            await self.resume.wait()
        if self.download_error:
            raise self.download_error


@asynccontextmanager
async def setup_downloads(
    tmp_path: Path,
    *,
    archive: bool = False,
) -> AsyncGenerator[tuple[Service, AttachmentBackend, Settings, DownloadStore, FakeClock]]:
    backend = AttachmentBackend()
    settings = Settings(
        allow_attachment_downloads=True,
        allowed_chat_ids=["1"],
        session_path=tmp_path / "account.session",
        archive_directory=tmp_path / "archive" if archive else None,
    )
    clock = FakeClock()
    store = DownloadStore(tmp_path / "account.session", clock, root=tmp_path / "downloads")
    service = build_service(backend, settings, clock=clock, downloads=store)
    try:
        yield service, backend, settings, store, clock
    finally:
        await service.close()
