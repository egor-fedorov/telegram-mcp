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
from telegram_mcp.application.change_ports import Observation
from telegram_mcp.application.change_reader import advance_changes
from telegram_mcp.application.incremental import BudgetExhausted, IncrementalContext
from telegram_mcp.application.watch_ports import WatchStorePort
from telegram_mcp.domain.changes import Change, CheckpointState
from telegram_mcp.domain.folders import select_chats
from telegram_mcp.domain.models import Issue, ToolError
from telegram_mcp.domain.text import clip
from telegram_mcp.domain.watches import (
    PendingDelivery,
    WatchAcknowledgement,
    WatchChangesResult,
    WatchDeletion,
    WatchInfo,
    WatchList,
    WatchProgress,
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
            len(chat_refs) > 1000 or any(not ref.strip() for ref in chat_refs)
        ):
            raise ToolError("INVALID_ARGUMENT", "Use 1..1000 nonempty chat references.")
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
        async with self._request(name):
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
        position.cycle_started_at = self.context.clock.utc_now().replace(microsecond=0)

    async def _authorize(self, state: WatchState, access: AccessContext, chat_id: str) -> None:
        self.context.check_cooldown()
        if state.chat_ids:
            await access.allow_explicit(chat_id)
        else:
            access.require_access(chat_id)

    async def changes(self, name: str, limit: int = 30) -> WatchChangesResult:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "Use limit 1..100.")
        async with self._request(name):
            ctx = self.context
            binding = await ctx.backend.checkpoint_binding()
            state = self._get(name, binding)
            access = await ctx.access.load(explicit=not state.folder_ids)
            if state.pending is not None:
                replay_budget = ctx.budget()
                try:
                    for cid in state.pending.source_ids:
                        await self._authorize(state, access, cid)
                        await replay_budget.call(
                            partial(ctx.backend.check_export_source, cid), page=False
                        )
                except BudgetExhausted:
                    raise ToolError(
                        "CONNECTION_ERROR", "Pending batch checks timed out; retry this watch."
                    ) from None
                return state.pending.result
            if not state.position.remaining:
                self._start_cycle(state, access)
            candidate = state.position.model_copy(deep=True)
            result = WatchChangesResult(name=name)
            budget = ctx.budget()
            text_left = ctx.limits.text_budget
            delivered: set[str] = set()
            issues: list[Issue] = []
            attempted = 0

            def emit(change: Change, observed: Observation | None) -> bool:
                nonlocal text_left
                if result.returned_count >= limit or text_left <= 0:
                    return False
                delivered.add(change.message_ref.chat_id)
                if observed is not None and not observed.exportable:
                    result.omitted_count += 1
                    issues.append(
                        Issue(
                            code="EXPORT_PROTECTED",
                            message="Telegram restricts saving this message in a pending batch.",
                            message_ref=change.message_ref,
                        )
                    )
                else:
                    if change.message is not None:
                        message = clip(change.message, min(text_left, ctx.limits.text_limit))
                        text_left -= len(message.text)
                        change = change.model_copy(update={"message": message})
                    result.changes.append(change)
                result.returned_count += 1
                return True

            # Visit a source at most once in this call. A partial source rotates
            # behind its peers; a failed source is retried in the next sweep.
            for cid in list(candidate.remaining):
                if (
                    result.returned_count >= limit
                    or text_left <= 0
                    or budget.calls >= ctx.limits.rpc_pages_per_call
                    or ctx.clock.monotonic() >= budget.deadline
                ):
                    break
                candidate.remaining.remove(cid)
                candidate.errors.pop(cid, None)
                attempted += 1
                try:
                    await self._authorize(state, access, cid)
                    # Metadata shares the deadline, but cannot consume the only
                    # data page when rpc_pages_per_call is configured to one.
                    await budget.call(partial(ctx.backend.check_export_source, cid), page=False)
                    scan = await advance_changes(
                        ctx,
                        candidate.sources[cid],
                        budget,
                        emit,
                        limit=limit - result.returned_count,
                        isolate_errors=True,
                    )
                    if scan.issues:
                        candidate.errors[cid] = scan.issues[0]
                    elif candidate.sources[cid].phase != "idle":
                        candidate.remaining.append(cid)
                except BudgetExhausted:
                    candidate.remaining.insert(0, cid)
                    break
                except ToolError as exc:
                    if exc.retryable:
                        candidate.errors[cid] = ctx.temporary_issue(exc, cid)
                    else:
                        candidate.errors[cid] = exc.issue(cid)
                # An account-wide FloodWait must stop subsequent requests now.
                if cid in candidate.errors and candidate.errors[cid].code == "RATE_LIMITED":
                    candidate.remaining.insert(0, cid)
                    break
            result.complete = not candidate.remaining and not candidate.errors
            if result.complete:
                candidate.completed_at = candidate.cycle_started_at
            issues.extend(candidate.errors.values())
            result.issues, result.issue_count = issues[:20], len(issues)
            result.progress = WatchProgress(
                source_count=len(candidate.members),
                pending_source_count=len(set(candidate.remaining) | candidate.errors.keys()),
                sources_this_call=attempted,
                rpc_pages_this_call=budget.calls,
            )
            if result.returned_count:
                result.batch_id = secrets.token_urlsafe(32)
                state.pending = PendingDelivery(
                    result=result, position=candidate, source_ids=sorted(delivered)
                )
            else:
                state.position = candidate
            self.store.save(state, binding)
            return result

    async def acknowledge(self, name: str, batch_id: str) -> WatchAcknowledgement:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", batch_id):
            raise ToolError("INVALID_ARGUMENT", "Use the batch_id returned by get_watch_changes.")
        async with self._request(name):
            await self.context.chats.check_authorization()
            binding = await self.context.backend.checkpoint_binding()
            state = self._get(name, binding)
            if state.last_acknowledged == batch_id:
                return WatchAcknowledgement(name=name, batch_id=batch_id)
            if state.pending is None or state.pending.result.batch_id != batch_id:
                raise ToolError(
                    "WATCH_BATCH_MISMATCH",
                    "This batch is not awaiting acknowledgement; no position changed.",
                )
            state.position = state.pending.position
            state.pending = None
            state.last_acknowledged = batch_id
            self.store.save(state, binding)
            return WatchAcknowledgement(name=name, batch_id=batch_id)

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
