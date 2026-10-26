"""Bound concurrent tool/resource calls and their FIFO admission queue."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from telegram_mcp.domain.models import ToolError


def _busy() -> ToolError:
    return ToolError(
        "SERVER_BUSY", "The request queue is full or its wait expired. Retry shortly.", 5
    )


def _closed() -> ToolError:
    return ToolError("SERVER_CLOSED", "The server has been closed.")


class ToolCallGate:
    def __init__(self, timeout_seconds: float = 120, *, capacity: int = 4, max_waiters: int = 16):
        if capacity < 1 or max_waiters < 0 or timeout_seconds <= 0:
            raise ValueError("Invalid admission limits")
        self.timeout_seconds = timeout_seconds
        self.capacity, self.max_waiters = capacity, max_waiters
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def _release(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self._active -= 1

    async def run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        if self._closed:
            raise _closed()
        if self._active < self.capacity and not self._waiters:
            self._active += 1
        else:
            if len(self._waiters) >= self.max_waiters:
                raise _busy()
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, self.timeout_seconds)
            except BaseException as exc:
                # Cancellation can arrive just after a slot was transferred to us.
                if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                    self._release()
                with suppress(ValueError):
                    self._waiters.remove(waiter)
                if isinstance(exc, TimeoutError):
                    raise _busy() from None
                raise
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            if self._closed:
                raise _closed()
            return await operation()
        finally:
            if task is not None:
                self._tasks.discard(task)
            self._release()

    async def close(self) -> None:
        self._closed = True
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_exception(_closed())
        self._waiters.clear()
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
