"""Synthetic QR exchanges; no Telegram connection or real login token."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import errors

TOKEN = "synthetic-private-login-token"
PASSWORD = "synthetic-private-password"
USER = SimpleNamespace(bot=False)


class FakeQR:
    url = "tg://login?token=" + TOKEN

    def __init__(self, outcome: str = "success", error: Exception | None = None):
        self.outcome, self.error = outcome, error
        self.started = asyncio.Event()
        self.scanned = asyncio.Event()
        self.handlers = 0

    async def wait(self):
        self.handlers += 1
        self.started.set()
        try:
            await self.scanned.wait()
            if self.error is not None:
                raise self.error
            if self.outcome == "timeout":
                raise TimeoutError()
            if self.outcome == "expired":
                raise errors.AuthTokenExpiredError(None)
            if self.outcome == "password":
                raise errors.SessionPasswordNeededError(None)
            return USER
        finally:
            self.handlers -= 1


def qr_client(qrs: list[FakeQR], *, authorized: bool = False):
    return SimpleNamespace(
        connect=AsyncMock(),
        get_me=AsyncMock(return_value=USER if authorized else None),
        qr_login=AsyncMock(side_effect=qrs),
        sign_in=AsyncMock(return_value=USER),
        start=AsyncMock(),
        disconnect=AsyncMock(),
    )


def scan_codes(qrs: list[FakeQR]):
    codes = iter(qrs)

    def display(url: str) -> None:
        qr = next(codes)
        assert qr.started.is_set() and qr.handlers == 1
        assert url == qr.url
        qr.scanned.set()

    return display
