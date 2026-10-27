"""On-demand named consumers with durable, explicitly acknowledged delivery."""

import asyncio
import re
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

from telegram_mcp.application.access import AccessContext
from telegram_mcp.application.incremental import IncrementalContext
from telegram_mcp.application.watch_delivery import WatchDelivery
from telegram_mcp.application.watch_ports import WatchStorePort
from telegram_mcp.application.watch_sweep import WatchSweep
from telegram_mcp.domain.changes import CheckpointState
from telegram_mcp.domain.folders import select_chats
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.references import chat_target
from telegram_mcp.domain.watches import (
    BatchArchive,
    PendingDelivery,
    WatchAcknowledgement,
    WatchChangesResult,
    WatchDeletion,
    WatchInfo,
    WatchList,
    WatchState,
    watch_info,
)


@dataclass
class WatchLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class WatchService:
    def __init__(self, context: IncrementalContext, store: WatchStorePort):
        self.context, self.store = context, store
        self.delivery = WatchDelivery(context, store)
        self._locks: dict[str, WatchLock] = {}

    @asynccontextmanager
    async def _request(self, name: str) -> AsyncGenerator[None]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
            raise ToolError(
                "INVALID_ARGUMENT",
                "Use a watch name of 1..64 lowercase letters, digits, hyphens or underscores.",
            )
        async with self.context.request():
            entry = self._locks.setdefault(name, WatchLock())
            entry.users += 1
            try:
                async with entry.lock:
                    self.context.check_cooldown()
                    yield
            finally:
                entry.users -= 1
                if not entry.users:
                    del self._locks[name]

    def _get(self, name: str, binding: str) -> WatchState:
        state = self.store.get(name, binding)
        if state is None:
            raise ToolError(
                "WATCH_NOT_FOUND", "No subscription with this name exists for this authorization."
            )
        return state

    async def create(
        self,
        name: str,
        chat_refs: list[str] | None = None,
        folder_ids: list[int] | None = None,
        date_from: datetime | None = None,
    ) -> WatchInfo:
        if (chat_refs is None) == (folder_ids is None) or chat_refs == [] or folder_ids == []:
            raise ToolError(
                "INVALID_ARGUMENT", "Supply exactly one nonempty chat_refs or folder_ids selector."
            )
        if chat_refs is not None and (
            len(chat_refs) > 20 or any(not ref.strip() for ref in chat_refs)
        ):
            raise ToolError(
                "INVALID_ARGUMENT",
                "Use 1..20 nonempty chat references; use folders for larger selections.",
            )
        if folder_ids is not None and (
            len(folder_ids) > 100 or any(type(fid) is not int or fid < 0 for fid in folder_ids)
        ):
            raise ToolError("INVALID_ARGUMENT", "Use 1..100 nonnegative folder IDs.")
        if date_from is not None and (
            date_from.utcoffset() is None or date_from > self.context.clock.utc_now()
        ):
            raise ToolError(
                "INVALID_ARGUMENT", "date_from must have a timezone and must not be in the future."
            )
        refs = list(
            dict.fromkeys(str(chat_target(ref)).lstrip("@").lower() for ref in chat_refs or [])
        )
        async with self._request(name):
            try:
                async with asyncio.timeout(self.context.limits.page_deadline_seconds):
                    return await self._create(name, refs, folder_ids, date_from)
            except TimeoutError:
                raise ToolError(
                    "CONNECTION_ERROR", "Watch creation timed out; no partial watch was saved."
                ) from None

    async def _create(
        self,
        name: str,
        chat_refs: list[str],
        folder_ids: list[int] | None,
        date_from: datetime | None,
    ) -> WatchInfo:
        ctx = self.context
        access = await ctx.access.load(explicit=folder_ids is None)
        binding = await ctx.backend.checkpoint_binding()
        ids: set[str] = set()
        for ref in chat_refs or []:
            ctx.check_cooldown()
            chat = await ctx.chats.resolve_chat(ref, refresh=True)
            await access.allow_explicit(chat.id)
            ids.add(chat.id)
        folders = sorted(set(folder_ids or []))
        if folders:
            select_chats(access.chats, access.folders, access.allowed, folder_ids=folders)
        previous = self.store.get(name, binding)
        if previous:
            if (previous.chat_ids, previous.folder_ids, previous.requested_from) != (
                sorted(ids),
                folders,
                date_from,
            ):
                raise ToolError(
                    "WATCH_CONFLICT",
                    "This name already has a different selector or start date; "
                    "its position was preserved.",
                )
            return watch_info(previous)
        state = WatchState(
            name=name,
            chat_ids=sorted(ids),
            folder_ids=folders,
            requested_from=date_from,
            # Telegram timestamps have whole-second resolution. Overlap the
            # current second so a post arriving just after creation is included.
            started_at=date_from or ctx.clock.utc_now().replace(microsecond=0),
        )
        self._start_cycle(state, access)
        self.store.save(state, binding)
        return watch_info(state)

    def _start_cycle(self, state: WatchState, access: AccessContext) -> None:
        position = state.position
        members = (
            state.chat_ids
            if state.chat_ids
            else [
                chat.id
                for chat in select_chats(
                    access.chats, access.folders, access.allowed, folder_ids=state.folder_ids
                )
            ]
        )
        position.members = sorted(set(members))
        for cid in position.members:
            if cid not in position.sources:
                position.sources[cid] = CheckpointState(
                    kind="changes",
                    chat_id=cid,
                    phase="baseline",
                    date_from=position.completed_at or state.started_at,
                )
        position.remaining = list(position.members)
        position.errors = {}
        position.probe_fallback = []
        position.cycle_started_at = self.context.clock.utc_now().replace(microsecond=0)

    async def _authorize(self, state: WatchState, access: AccessContext, chat_id: str) -> None:
        self.context.check_cooldown()
        if state.chat_ids:
            await access.allow_explicit(chat_id)
        else:
            access.require_access(chat_id)

    async def changes(
        self, name: str, limit: int = 30, ack_batch_id: str | None = None, save: bool = False
    ) -> WatchChangesResult:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "Use limit 1..100.")
        if ack_batch_id is not None:
            self._validate_batch(ack_batch_id)
        if save:
            # Saving is a precondition of the whole call, before any acknowledgement.
            self.context.archive.require_enabled()
        async with self._request(name):
            ctx = self.context
            binding = await ctx.backend.checkpoint_binding()
            state = self._get(name, binding)
            access = await ctx.access.load(explicit=not state.folder_ids)
            authorize = partial(self._authorize, state, access)
            if ack_batch_id is not None:
                pending = state.pending
                if save and pending is not None and pending.result.batch_id == ack_batch_id:
                    await self.delivery.check_pending(state, authorize)
                    await self.delivery.ensure_archive(state, binding)
                await self.delivery.acknowledge(state, binding, ack_batch_id)
            if state.pending is not None:
                await self.delivery.check_pending(state, authorize)
                if save:
                    await self.delivery.ensure_archive(state, binding)
                return await self.delivery.delivered(state, verified=save)
            if not state.position.remaining:
                self._start_cycle(state, access)
            candidate = state.position.model_copy(deep=True)
            sweep = WatchSweep(ctx, name, candidate, limit, authorize)
            result = await sweep.run()
            if not result.returned_count:
                state.position = candidate
                self.store.save(state, binding)
                return result
            result.batch_id = secrets.token_urlsafe(32)
            state.pending = PendingDelivery(
                result=result,
                position=candidate,
                source_ids=sorted(sweep.delivered),
                archive=BatchArchive(key=secrets.token_urlsafe(32)) if save else None,
            )
            # Persist the batch and its reserved copy before writing the file, so a
            # failed or interrupted save leaves a replayable, unacknowledged batch.
            self.store.save(state, binding)
            if save:
                await self.delivery.ensure_archive(state, binding)
            return await self.delivery.delivered(state, verified=save)

    async def acknowledge(self, name: str, batch_id: str) -> WatchAcknowledgement:
        self._validate_batch(batch_id)
        async with self._request(name):
            await self.context.chats.check_authorization()
            binding = await self.context.backend.checkpoint_binding()
            state = self._get(name, binding)
            return await self.delivery.acknowledge(state, binding, batch_id)

    @staticmethod
    def _validate_batch(batch_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", batch_id):
            raise ToolError("INVALID_ARGUMENT", "Use the batch_id returned by get_watch_changes.")

    def name_suggestions(self, prefix: str, limit: int = 101) -> list[str]:
        """Local names for prompt completion: no Telegram request and no batch bodies."""
        if len(prefix) > 64 or not re.fullmatch(r"[a-z0-9_-]*", prefix):
            return []
        binding = self.context.backend.local_checkpoint_binding()
        if binding is None:
            return []
        try:
            return self.store.names(binding, prefix, limit)
        except ToolError:
            return []

    async def list(self) -> WatchList:
        async with self.context.request():
            await self.context.chats.check_authorization()
            binding = await self.context.backend.checkpoint_binding()
            return WatchList(watches=[watch_info(state) for state in self.store.list(binding)])

    async def delete(self, name: str) -> WatchDeletion:
        async with self._request(name):
            await self.context.chats.check_authorization()
            binding = await self.context.backend.checkpoint_binding()
            return WatchDeletion(name=name, deleted=self.store.delete(name, binding))
