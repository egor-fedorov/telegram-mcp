"""Explicit composition root; application code never constructs adapters."""

from telegram_mcp.adapters.archive import ArchiveStore
from telegram_mcp.adapters.checkpoints import CheckpointStore
from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import ConfiguredPolicyProvider, Settings
from telegram_mcp.adapters.cursors import CursorStore
from telegram_mcp.adapters.downloads import DownloadStore
from telegram_mcp.adapters.exports import ExportStore
from telegram_mcp.adapters.telegram import TelegramBackend
from telegram_mcp.adapters.watches import WatchStore
from telegram_mcp.application.archive_ports import ArchivePort
from telegram_mcp.application.attachment_ports import DownloadStorePort
from telegram_mcp.application.change_ports import CheckpointStorePort, ExportStorePort
from telegram_mcp.application.ports import Backend, Clock, CursorPort, ExecutionLimits
from telegram_mcp.application.service import Service
from telegram_mcp.application.watch_ports import WatchStorePort


def build_service(
    backend: Backend,
    settings: Settings,
    *,
    clock: Clock | None = None,
    cursors: CursorPort | None = None,
    downloads: DownloadStorePort | None = None,
    checkpoints: CheckpointStorePort | None = None,
    exports: ExportStorePort | None = None,
    watches: WatchStorePort | None = None,
    archive: ArchivePort | None = None,
) -> Service:
    clock = clock if clock is not None else SystemClock()
    limits = ExecutionLimits(
        rpc_pages_per_call=settings.rpc_pages_per_call,
        cursor_ttl_seconds=settings.cursor_ttl_seconds,
        max_cursors=settings.max_cursors,
    )
    cursors = (
        cursors
        if cursors is not None
        else CursorStore(clock, ttl=limits.cursor_ttl_seconds, maximum=limits.max_cursors)
    )
    return Service(
        backend=backend,
        policy_provider=ConfiguredPolicyProvider(settings),
        limits=limits,
        cursors=cursors,
        clock=clock,
        downloads=downloads
        if downloads is not None
        else DownloadStore(settings.session_path, clock),
        checkpoints=checkpoints
        if checkpoints is not None
        else CheckpointStore(settings.session_path, clock),
        exports=exports if exports is not None else ExportStore(settings.session_path, clock),
        watches=watches if watches is not None else WatchStore(settings.session_path),
        archive=archive if archive is not None else ArchiveStore(settings.archive_directory, clock),
    )


def main() -> None:
    from telegram_mcp.adapters.bridge import connect
    from telegram_mcp.adapters.cli import CliDependencies
    from telegram_mcp.adapters.cli import main as cli_main
    from telegram_mcp.adapters.telegram.auth import authenticate
    from telegram_mcp.adapters.transport import run_http

    cli_main(CliDependencies(TelegramBackend, build_service, authenticate, run_http, connect))
