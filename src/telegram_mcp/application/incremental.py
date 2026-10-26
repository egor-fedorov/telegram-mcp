"""Shared authorization, durable retry deadlines and request budgets for M4 reads."""

import asyncio
import math
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from telegram_mcp.application.access import AccessFactory
from telegram_mcp.application.change_ports import (
    CheckpointStorePort,
    ExportStorePort,
    IncrementalPort,
)
from telegram_mcp.application.ports import ChatPort, Clock, ExecutionLimits
from telegram_mcp.domain.changes import CheckpointState
from telegram_mcp.domain.models import Chat, Issue, ToolError


class BudgetExhausted(Exception):
    """A successful partial page can be continued without reporting a failure."""


class ReadBudget:
    def __init__(self, clock: Clock, limits: ExecutionLimits):
        self.clock, self.limits = clock, limits
        self.deadline = clock.monotonic() + limits.page_deadline_seconds
        self.calls = 0

    async def call[T](self, request: Callable[[], Awaitable[T]]) -> T:
        remaining = self.deadline - self.clock.monotonic()
        if self.calls >= self.limits.rpc_pages_per_call or remaining <= 0:
            raise BudgetExhausted
        self.calls += 1
        try:
            return await asyncio.wait_for(request(), timeout=remaining)
        except TimeoutError:
            raise ToolError(
                "CONNECTION_ERROR", "Incremental read timed out; continue from the checkpoint."
            ) from None


@dataclass(frozen=True)
class PreparedRead:
    state: CheckpointState
    chat: Chat
    binding: str


class IncrementalContext:
    def __init__(
        self,
        backend: IncrementalPort,
        chats: ChatPort,
        access: AccessFactory,
        checkpoints: CheckpointStorePort,
        exports: ExportStorePort,
        clock: Clock,
        limits: ExecutionLimits,
    ):
        self.backend, self.chats, self.access = backend, chats, access
        self.checkpoints, self.exports = checkpoints, exports
        self.clock, self.limits = clock, limits
        self._lock = asyncio.Lock()
        self._closed = False

    def check_cooldown(self) -> None:
        # Session-wide, checked before authorization/reconnection RPCs. Old tokens
        # and a newly created selection cannot bypass a recorded FloodWait.
        until = self.checkpoints.retry_at("session")
        if until is not None and until > self.clock.utc_now():
            raise ToolError(
                "RATE_LIMITED",
                "Telegram requests a wait before the next M4 read.",
                max(1, math.ceil((until - self.clock.utc_now()).total_seconds())),
            )

    @asynccontextmanager
    async def request(self) -> AsyncGenerator[None]:
        if self._closed:
            raise ToolError("SERVER_CLOSED", "The server has been closed.")
        try:
            await asyncio.wait_for(self._lock.acquire(), 5)
        except TimeoutError:
            raise ToolError(
                "SERVER_BUSY", "Another incremental read is running; retry shortly.", 1
            ) from None
        try:
            if self._closed:
                raise ToolError("SERVER_CLOSED", "The server has been closed.")
            self.check_cooldown()
            try:
                yield
            except ToolError as exc:
                if exc.code == "RATE_LIMITED" and exc.retry_after:
                    self.checkpoints.defer(
                        "session", self.clock.utc_now() + timedelta(seconds=exc.retry_after)
                    )
                raise
        finally:
            self._lock.release()

    async def prepare(
        self,
        kind: Literal["changes", "history"],
        chat_ref: str | None,
        checkpoint: str | None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> PreparedRead:
        if (
            (chat_ref is None) == (checkpoint is None)
            or chat_ref is not None
            and not chat_ref.strip()
        ):
            raise ToolError("INVALID_ARGUMENT", "Supply exactly one of chat_ref or checkpoint.")
        if checkpoint is not None and (date_from is not None or date_to is not None):
            raise ToolError("INVALID_ARGUMENT", "A checkpoint already contains its date bounds.")
        for date in (date_from, date_to):
            if date is not None and date.utcoffset() is None:
                raise ToolError("INVALID_ARGUMENT", "Dates must include a timezone.")
        if date_from and date_to and date_from >= date_to:
            raise ToolError("INVALID_ARGUMENT", "date_from must be earlier than date_to.")
        access = await self.access.load(explicit=True)
        binding = await self.backend.checkpoint_binding()
        if checkpoint is not None:
            state = self.checkpoints.get(checkpoint, binding, kind)
            await access.allow_explicit(state.chat_id)
            chat = await self.chats.resolve_chat(state.chat_id, refresh=True)
        else:
            assert chat_ref is not None
            chat = await self.chats.resolve_chat(chat_ref, refresh=True)
            await access.allow_explicit(chat.id)
            state = CheckpointState(
                kind=kind,
                chat_id=chat.id,
                phase="baseline" if kind == "changes" else "history",
                date_from=date_from,
                date_to=min(date_to, self.clock.utc_now()) if date_to else self.clock.utc_now(),
            )
        return PreparedRead(state, chat, binding)

    async def authorize(self, chat_id: str, *, export: bool = False) -> None:
        access = await self.access.load(explicit=True)
        await access.allow_explicit(chat_id)
        if export:
            await self.backend.check_export_source(chat_id)

    def temporary_issue(self, error: ToolError, chat_id: str) -> Issue:
        if not error.retryable:
            raise error
        if error.code == "RATE_LIMITED" and error.retry_after:
            self.checkpoints.defer(
                "session", self.clock.utc_now() + timedelta(seconds=error.retry_after)
            )
        return error.issue(chat_id)

    async def read_resource(self, uri: str) -> tuple[str, str]:
        async with self.request():
            stored = await self.exports.lookup(uri)
            await self.authorize(stored.chat_id, export=True)
            if stored.binding != await self.backend.checkpoint_binding():
                raise ToolError("EXPORT_UNAVAILABLE", "Export belongs to another authorization.")
            data = await self.exports.read(uri)
            return data.decode("utf-8"), stored.result.mime_type

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            try:
                await self.exports.close()
            finally:
                self.checkpoints.close()
