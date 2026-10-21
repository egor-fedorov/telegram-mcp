"""Serialize complete tool calls sharing one Telegram runtime."""

import asyncio
from collections.abc import Awaitable, Callable

from telegram_mcp.domain.models import ToolError


class ToolCallGate:
    def __init__(self, timeout_seconds: float = 5.0):
        self.timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()

    async def run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            await asyncio.wait_for(self._lock.acquire(), self.timeout_seconds)
        except TimeoutError:
            raise ToolError(
                "SERVER_BUSY", "Another Telegram request is running. Retry shortly.", 1
            ) from None
        try:
            return await operation()
        finally:
            self._lock.release()
