"""Fresh folder rules over atomically published, bounded-age dialog snapshots."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from telethon import functions, types

from telegram_mcp.adapters.telegram.catalog_pages import DialogScan, DialogScanner
from telegram_mcp.adapters.telegram.mapping import chat_from_entity, folder_from_tl, peer_id, utc
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import Clock
from telegram_mcp.application.retry import RetryState
from telegram_mcp.domain.models import Chat, Folder, ToolError


@dataclass(frozen=True)
class Snapshot:
    scan: DialogScan
    completed_at: float


class DialogCatalog:
    def __init__(
        self,
        runtime: TelegramRuntime,
        clock: Clock,
        max_dialogs: int,
        cache: dict[str, Chat],
        *,
        ttl: float = 300,
        stale: float = 300,
    ):
        self.runtime, self.clock, self.cache = runtime, clock, cache
        self.scanner = DialogScanner(runtime, max_dialogs)
        self.ttl, self.stale = ttl, stale
        self.wait_seconds = 5.0
        self._snapshot: Snapshot | None = None
        self._builder: DialogScan | None = None
        self._task: asyncio.Task[None] | None = None
        self._retired: set[asyncio.Task[None]] = set()
        self._error: ToolError | None = None
        self._retry = RetryState()
        self._folder_retry = RetryState()
        self._aux_lock = asyncio.Lock()
        self._closed = False

    def _invalidate(self) -> None:
        self._snapshot = None
        self._builder = None
        self.cache.clear()
        self._retry.clear()
        self._folder_retry.clear()
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
            self._retired.add(self._task)
            self._task.add_done_callback(self._retired.discard)
            self._task = None

    async def check_authorization(self) -> None:
        try:
            await self.runtime.verify_authorization()
        except ToolError as exc:
            if exc.code == "AUTH_REQUIRED":
                self._invalidate()
            raise
        if (
            self._snapshot is not None
            and self._snapshot.scan.auth_generation != self.runtime.auth_generation
        ):
            self._invalidate()

    def _check_generation(self, generation: int) -> None:
        if self._closed:
            raise ToolError("SERVER_CLOSED", "The catalog has been closed.")
        if generation != self.runtime.auth_generation:
            self._invalidate()
            raise ToolError("AUTH_REQUIRED", "Telegram authorization changed. Run login.")

    def _usable(self, snapshot: Snapshot | None, *, grace: bool) -> bool:
        return bool(
            snapshot is not None
            and self.ttl > 0
            and snapshot.scan.auth_generation == self.runtime.auth_generation
            and self.clock.monotonic() - snapshot.completed_at
            < self.ttl + (self.stale if grace else 0)
        )

    def _start_refresh(self) -> asyncio.Task[None] | None:
        if self._task is not None and not self._task.done():
            return self._task
        if not self._retry.ready(self.clock):
            return None
        now = self.clock.monotonic()
        if (
            self._builder is None
            or now - self._builder.last_used >= 900
            or self._builder.auth_generation != self.runtime.auth_generation
        ):
            self._builder = DialogScan(now, self.runtime.auth_generation)
        self._error = None
        self._task = asyncio.create_task(self._refresh(self._builder))
        return self._task

    async def _refresh(self, builder: DialogScan) -> None:
        try:
            while builder.phase != "complete":
                started = self.clock.monotonic()
                for _ in range(5):
                    self._check_generation(builder.auth_generation)
                    client = await self.runtime.ensure()
                    await self.scanner.step(client, builder)
                    builder.last_used = self.clock.monotonic()
                    self._check_generation(builder.auth_generation)
                    if builder.phase == "complete" or self.clock.monotonic() - started >= 5:
                        break
                await asyncio.sleep(0)
            self._check_generation(builder.auth_generation)
            if self._builder is builder:
                chats = self._chats(builder)
                self._snapshot = Snapshot(builder, self.clock.monotonic())
                self._builder = None
                self._retry.clear()
                self.cache.update({chat.id: chat for chat in chats})
        except ToolError as exc:
            self._error = exc
            if exc.code == "AUTH_REQUIRED":
                self._invalidate()
            elif not exc.retryable:
                self._builder = None
            else:
                self._retry.record(exc, None, self.clock)
        except (AttributeError, TypeError, ValueError):
            self._error = ToolError("CATALOG_ERROR", "Telegram returned unusable dialog metadata.")
            self._builder = None

    def _loading(self) -> ToolError:
        count = len(self._builder.dialogs) if self._builder is not None else 0
        return ToolError(
            "CATALOG_LOADING",
            f"Catalog refresh is in progress ({count} dialogs loaded). Retry shortly.",
            1,
        )

    async def _snapshot_for_read(self) -> Snapshot:
        snapshot = self._snapshot
        if self._usable(snapshot, grace=False):
            assert snapshot is not None
            return snapshot
        task = self._start_refresh()
        if self._usable(snapshot, grace=True):
            assert snapshot is not None
            return snapshot
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), self.wait_seconds)
            except TimeoutError:
                raise self._loading() from None
        if self._error is not None:
            issue = self._retry.current_issue(self.clock)
            if issue is not None:
                raise ToolError(issue.code, issue.message, issue.retry_after_seconds)
            raise self._error
        if self._snapshot is None:
            raise self._loading()
        return self._snapshot

    async def _filters(self, client: Any) -> list[Any]:
        if not self._folder_retry.ready(self.clock):
            issue = self._folder_retry.current_issue(self.clock)
            assert issue is not None
            raise ToolError(issue.code, issue.message, issue.retry_after_seconds)
        try:
            response = await self.runtime.call(client(functions.messages.GetDialogFiltersRequest()))
            filters = response.filters if hasattr(response, "filters") else response
            if not isinstance(filters, list):
                raise ToolError("CATALOG_ERROR", "Telegram returned unusable folder metadata.")
            self._folder_retry.clear()
            return filters
        except ToolError as exc:
            if exc.code == "RATE_LIMITED":
                self._folder_retry.record(exc, None, self.clock)
            raise

    async def _auxiliary(self, client: Any, scan: DialogScan, filters: list[Any]) -> None:
        async with self._aux_lock:
            started = self.clock.monotonic()
            if scan.self_id is None and any(
                isinstance(peer, types.InputPeerSelf)
                for raw in filters
                for name in ("include_peers", "pinned_peers", "exclude_peers")
                for peer in getattr(raw, name, [])
            ):
                scan.self_id = peer_id(await self.runtime.call(client.get_me(input_peer=True)))
            if any(getattr(raw, "exclude_muted", False) for raw in filters):
                for name, peer in (
                    ("channel", types.InputNotifyBroadcasts()),
                    ("group", types.InputNotifyChats()),
                    ("private", types.InputNotifyUsers()),
                ):
                    if name not in scan.defaults:
                        if self.clock.monotonic() - started >= 5:
                            raise self._loading()
                        scan.defaults[name] = await self.runtime.call(
                            client(functions.account.GetNotifySettingsRequest(peer))
                        )

    def _chats(self, scan: DialogScan) -> list[Chat]:
        now = self.clock.utc_now()
        defaults = {
            name: (until := getattr(notify, "mute_until", None)) is not None and utc(until) > now
            for name, notify in scan.defaults.items()
        }
        chats = []
        for dialog in scan.dialogs.values():
            entity = dialog.entity
            category = (
                "private"
                if isinstance(entity, types.User)
                else "channel"
                if getattr(entity, "broadcast", False)
                else "group"
            )
            chats.append(chat_from_entity(entity, dialog, defaults.get(category, False), now=now))
        return chats

    async def read(self, *, refresh: bool = False) -> tuple[list[Chat], list[Folder]]:
        self._check_generation(self.runtime.auth_generation)
        await self.check_authorization()
        generation = self.runtime.auth_generation
        if refresh:
            task = self._task
            self._invalidate()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        try:
            client = await self.runtime.ensure()
            filters = await self._filters(client)
            self._check_generation(generation)
            snapshot = await self._snapshot_for_read()
            await self._auxiliary(client, snapshot.scan, filters)
            self._check_generation(generation)
            if self.ttl > 0 and not self._usable(snapshot, grace=True):
                self._start_refresh()
                raise self._loading()
            folders = [
                folder_from_tl(raw, index, snapshot.scan.self_id)
                for index, raw in enumerate(filters)
                if isinstance(raw, (types.DialogFilter, types.DialogFilterChatlist))
            ]
            return self._chats(snapshot.scan), folders
        except ToolError as exc:
            if exc.code == "AUTH_REQUIRED":
                self._invalidate()
            elif exc.code == "RATE_LIMITED" and self._folder_retry.ready(self.clock):
                self._folder_retry.record(exc, None, self.clock)
            raise
        except (AttributeError, TypeError, ValueError):
            raise ToolError(
                "CATALOG_ERROR", "Telegram returned unusable folder metadata."
            ) from None

    async def close(self) -> None:
        self._closed = True
        task = self._task
        self._invalidate()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
        await asyncio.gather(*self._retired, return_exceptions=True)
