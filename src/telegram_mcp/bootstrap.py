"""Explicit composition root; application code never constructs adapters."""

from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import ConfiguredPolicyProvider, Settings
from telegram_mcp.adapters.cursors import CursorStore
from telegram_mcp.adapters.telegram import TelegramBackend
from telegram_mcp.application.ports import Backend, Clock, CursorPort, ExecutionLimits
from telegram_mcp.application.service import Service


def build_service(
    backend: Backend,
    settings: Settings,
    *,
    clock: Clock | None = None,
    cursors: CursorPort | None = None,
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
    )


def main() -> None:
    from telegram_mcp.adapters.cli import CliDependencies
    from telegram_mcp.adapters.cli import main as cli_main
    from telegram_mcp.adapters.telegram.auth import authenticate

    cli_main(CliDependencies(TelegramBackend, build_service, authenticate))
