"""Interactive authentication's Telegram and local session resource operations."""

import os
from collections.abc import Callable
from typing import Any

from telethon import TelegramClient

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.adapters.version import get_version
from telegram_mcp.domain.models import ToolError


async def authenticate(
    settings: Settings,
    *,
    phone: Callable[[], str],
    code: Callable[[], str],
    password: Callable[[], str],
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
            client = client_factory(
                str(session),
                api_id,
                api_hash,
                device_model="Telegram Context MCP",
                app_version=get_version(),
            )
            try:
                await client.start(phone=phone, code_callback=code, password=password)
                if (await client.get_me()).bot:
                    raise ToolError("USER_ACCOUNT_REQUIRED", "This server expects a user account.")
            finally:
                await client.disconnect()
        finally:
            os.umask(previous_umask)
        session.chmod(0o600)
    finally:
        lease.close()
