"""Telethon adapters and their composition entrypoint."""

from telegram_mcp.adapters.telegram.backend import TelegramBackend
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime

__all__ = ["TelegramBackend", "TelegramRuntime"]
