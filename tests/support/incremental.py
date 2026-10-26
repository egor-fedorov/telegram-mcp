"""Explicit offline fixtures for durable reads and temporary export resources."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from telegram_mcp.adapters.checkpoints import CheckpointStore
from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.exports import ExportStore
from telegram_mcp.application.service import Service
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Chat
from tests.support.fakes import BASE, FakeBackend, FakeClock, msg

EXPORT_TEXT = "😀 Original source\n" * 30000


def export_stdio_service(path: Path) -> Service:
    clock = SystemClock()
    settings = Settings(session_path=path / "account.session", allowed_chat_ids=["1"])
    backend = FakeBackend([msg("1", mid, EXPORT_TEXT) for mid in range(1, 5)])
    return build_service(
        backend,
        settings,
        clock=clock,
        checkpoints=CheckpointStore(settings.session_path, clock, root=path / "checkpoints"),
        exports=ExportStore(settings.session_path, clock, root=path / "exports"),
    )


@dataclass
class IncrementalFixture:
    service: Service
    backend: FakeBackend
    settings: Settings
    clock: FakeClock
    checkpoints: CheckpointStore
    exports: ExportStore


@asynccontextmanager
async def incremental_fixture(
    path: Path,
    *,
    backend: FakeBackend | None = None,
    settings: Settings | None = None,
    clock: FakeClock | None = None,
) -> AsyncGenerator[IncrementalFixture]:
    backend = (
        backend
        if backend is not None
        else FakeBackend(
            [msg("1", mid) for mid in range(1, 4)],
            chats=[Chat(id="1", title="Source", type="group")],
        )
    )
    clock = clock if clock is not None else FakeClock(now=BASE + timedelta(days=30))
    settings = settings if settings is not None else Settings(allowed_chat_ids=["1"])
    settings.session_path = path / "account.session"
    checkpoints = CheckpointStore(settings.session_path, clock, root=path / "checkpoints")
    exports = ExportStore(settings.session_path, clock, root=path / "exports")
    service = build_service(
        backend, settings, clock=clock, checkpoints=checkpoints, exports=exports
    )
    try:
        yield IncrementalFixture(service, backend, settings, clock, checkpoints, exports)
    finally:
        await service.close()
