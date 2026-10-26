"""Deterministic overlapping reads for real transport tests."""

import asyncio
from pathlib import Path

from telegram_mcp.adapters.config import Settings
from telegram_mcp.application.ports import Batch, MessageOffset
from telegram_mcp.application.requests import MessageRead
from telegram_mcp.application.service import Service
from telegram_mcp.bootstrap import build_service
from tests.support.fakes import FakeBackend, msg


class ConcurrentBackend(FakeBackend):
    def __init__(self):
        super().__init__([msg(cid, mid) for cid in ("1", "2", "3") for mid in range(1, 4)])
        self.first_started = asyncio.Event()
        self.all_started = asyncio.Event()
        self.entered: set[str] = set()

    async def read(
        self,
        chat_id: str,
        request: MessageRead,
        *,
        offset: MessageOffset = 0,
        limit: int = 30,
    ) -> Batch:
        if chat_id == "1":
            self.first_started.set()
        await self.first_started.wait()
        self.entered.add(chat_id)
        if len(self.entered) == 3:
            self.all_started.set()
        # The first request cannot finish until the other two are admitted.
        await self.all_started.wait()
        return await super().read(chat_id, request, offset=offset, limit=limit)


def concurrent_stdio_service(path: Path) -> Service:
    return build_service(
        ConcurrentBackend(),
        Settings(session_path=path / "account.session", allowed_chat_ids=["1", "2", "3"]),
    )
