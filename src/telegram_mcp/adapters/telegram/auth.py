"""Interactive authentication's Telegram and local session resource operations."""

import asyncio
import os
from collections.abc import Callable
from typing import Any

from telethon import TelegramClient

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.adapters.telegram.qr_auth import authorize_qr
from telegram_mcp.adapters.version import get_version
from telegram_mcp.domain.models import ToolError


async def authenticate(
    settings: Settings,
    *,
    phone: Callable[[], str],
    code: Callable[[], str],
    password: Callable[[], str],
    show_qr: Callable[[str], None] | None = None,
    client_factory: Callable[..., Any] = TelegramClient,
) -> None:
    api_id, api_hash = settings.credentials()
    session = settings.session_path
    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease = SessionLease(session)
    lease.acquire(conflict_message="Stop the MCP server before logging in.")
    try:
        previous_umask = os.umask(0o077)
        try:
            options = (
                {
                    "request_retries": 1,
                    "connection_retries": 1,
                    "flood_sleep_threshold": 0,
                    "raise_last_call_error": True,
                }
                if show_qr is not None
                else {}
            )
            client = client_factory(
                str(session),
                api_id,
                api_hash,
                device_model="Telegram Context MCP",
                app_version=get_version(),
                **options,
            )
            try:
                if show_qr is None:
                    await client.start(phone=phone, code_callback=code, password=password)
                    user = await client.get_me()
                else:
                    user = await authorize_qr(
                        client,
                        show_qr=show_qr,
                        password=password,
                        timeout_seconds=settings.rpc_timeout_seconds,
                    )
                if user is None:
                    raise ToolError("AUTH_REQUIRED", "Telegram authorization was not confirmed.")
                if user.bot:
                    raise ToolError("USER_ACCOUNT_REQUIRED", "This server expects a user account.")
            finally:
                if show_qr is None:
                    await client.disconnect()
                else:
                    await asyncio.wait_for(client.disconnect(), settings.rpc_timeout_seconds)
        finally:
            os.umask(previous_umask)
        session.chmod(0o600)
    except Exception as exc:
        if show_qr is None or isinstance(exc, (ToolError, OSError, EOFError)):
            raise
        raise ToolError(
            "QR_LOGIN_FAILED", "QR login could not be completed. Retry later."
        ) from None
    finally:
        lease.close()
