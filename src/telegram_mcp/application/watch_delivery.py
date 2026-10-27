"""Replay, archive and acknowledge a watch's one pending batch.

A batch without save=true is acknowledged directly. With save=true its copy is first
reserved under a persisted key, then published and recorded as saved; only a saved
copy that still verifies allows acknowledgement.
"""

import hashlib
import secrets
from collections.abc import Awaitable, Callable
from functools import partial

from telegram_mcp.application.incremental import BudgetExhausted, IncrementalContext
from telegram_mcp.application.watch_ports import WatchStorePort
from telegram_mcp.domain.archiving import ArchiveSource
from telegram_mcp.domain.models import Issue, SavedFile, ToolError
from telegram_mcp.domain.watches import (
    BatchArchive,
    PendingDelivery,
    WatchAcknowledgement,
    WatchChangesResult,
    WatchState,
    batch_jsonl,
)


class WatchDelivery:
    def __init__(self, context: IncrementalContext, store: WatchStorePort):
        self.context, self.store = context, store

    async def check_pending(
        self, state: WatchState, authorize: Callable[[str], Awaitable[None]]
    ) -> None:
        """Recheck access and saving restrictions before replaying or saving a batch."""
        assert state.pending is not None
        budget = self.context.budget()
        try:
            for cid in state.pending.source_ids:
                await authorize(cid)
                await budget.call(
                    partial(self.context.backend.check_export_source, cid), page=False
                )
        except BudgetExhausted:
            raise ToolError(
                "CONNECTION_ERROR", "Pending batch checks timed out; retry this watch."
            ) from None

    async def delivered(self, state: WatchState, *, verified: bool) -> WatchChangesResult:
        """Return the pending batch; never present an unverified copy as saved."""
        pending = state.pending
        assert pending is not None
        saved = pending.saved_copy
        result = pending.result.model_copy(update={"saved_file": saved})
        if saved is None or verified:
            return result
        if await self.context.archive.verify(saved, batch_source(state.name, pending)):
            return result
        issue = Issue(
            code="ARCHIVE_COPY_INVALID",
            message="This batch's permanent copy is missing or damaged. Repeat "
            "get_watch_changes with save=true to rebuild it before acknowledging.",
        )
        return result.model_copy(
            update={
                "saved_file": None,
                "issues": [issue, *result.issues][:20],
                "issue_count": result.issue_count + 1,
            }
        )

    async def ensure_archive(self, state: WatchState, binding: str) -> None:
        """Publish one permanent copy of the pending batch and record its receipt."""
        pending, archive = state.pending, self.context.archive
        assert pending is not None
        source = batch_source(state.name, pending)
        saved = pending.saved_copy
        if saved is not None:
            if await archive.verify(saved, source):
                return
            pending.archive = None  # Rebuild a missing or damaged copy from the batch.
        if pending.archive is None:
            pending.archive = BatchArchive(key=secrets.token_urlsafe(32))
            self.store.save(state, binding)
        data = batch_jsonl(state.name, pending)
        digest = hashlib.sha256(data).hexdigest()

        async def publish(key: str) -> SavedFile:
            return await archive.save(
                data,
                f"{state.name}.jsonl",
                digest,
                mime_type="application/x-ndjson",
                source=source,
                key=key,
            )

        try:
            saved = await publish(pending.archive.key)
        except ToolError as exc:
            if exc.code != "ARCHIVE_COPY_INVALID":
                raise
            # Never reuse a reserved location that holds different data.
            pending.archive = BatchArchive(key=secrets.token_urlsafe(32))
            self.store.save(state, binding)
            saved = await publish(pending.archive.key)
        pending.archive.saved_file = saved
        self.store.save(state, binding)

    async def acknowledge(
        self, state: WatchState, binding: str, batch_id: str
    ) -> WatchAcknowledgement:
        if state.last_acknowledged == batch_id:
            return WatchAcknowledgement(name=state.name, batch_id=batch_id)
        pending = state.pending
        if pending is None or pending.result.batch_id != batch_id:
            raise ToolError(
                "WATCH_BATCH_MISMATCH",
                "This batch is not awaiting acknowledgement; no position changed.",
            )
        if pending.archive_status == "reserved":
            raise ToolError(
                "WATCH_ARCHIVE_PENDING",
                "This batch requested a permanent copy that is not saved yet. Repeat "
                "get_watch_changes with save=true before acknowledging; no position changed.",
            )
        saved = pending.saved_copy
        if saved is not None and not await self.context.archive.verify(
            saved, batch_source(state.name, pending)
        ):
            raise ToolError(
                "ARCHIVE_COPY_INVALID",
                "The batch's permanent copy is missing or damaged. Repeat "
                "get_watch_changes with save=true to rebuild it; no position changed.",
            )
        state.position = pending.position
        state.pending = None
        state.last_acknowledged = batch_id
        self.store.save(state, binding)
        return WatchAcknowledgement(name=state.name, batch_id=batch_id)


def batch_source(name: str, pending: PendingDelivery) -> ArchiveSource:
    """Provenance recorded in, and required from, a saved batch's metadata.json."""
    return ArchiveSource(
        tool="get_watch_changes",
        watch_name=name,
        batch_id=pending.result.batch_id,
        source_ids=pending.source_ids,
        format="jsonl",
    )
