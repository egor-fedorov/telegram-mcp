"""Export a frozen single-source history range in bounded, restartable parts."""

from datetime import datetime

from telegram_mcp.application.change_ports import (
    MAX_EXPORT_BYTES,
    MAX_EXPORT_RECORDS,
    Observation,
)
from telegram_mcp.application.change_reader import read_forward
from telegram_mcp.application.incremental import BudgetExhausted, IncrementalContext, ReadBudget
from telegram_mcp.domain.archiving import ArchiveSource
from telegram_mcp.domain.changes import CheckpointState, ExportFormat, ExportHistoryResult
from telegram_mcp.domain.exporting import ExportPart, omitted_summary, protected_record
from telegram_mcp.domain.models import Chat, ToolError


class HistoryExportService:
    def __init__(self, context: IncrementalContext):
        self.context = context

    async def export_history(
        self,
        chat_ref: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        checkpoint: str | None = None,
        format: ExportFormat = "jsonl",
        save: bool = False,
    ) -> ExportHistoryResult:
        if format not in ("jsonl", "markdown"):
            raise ToolError("INVALID_ARGUMENT", "Use format jsonl or markdown.")
        ctx = self.context
        if save:
            ctx.archive.require_enabled()
        async with ctx.request():
            prepared = await ctx.prepare("history", chat_ref, checkpoint, date_from, date_to)
            state = prepared.state
            assert state.date_to is not None
            await ctx.backend.check_export_source(state.chat_id)
            result = ExportHistoryResult(
                chat_id=state.chat_id, date_from=state.date_from, date_to=state.date_to
            )
            budget = ctx.budget()
            part: ExportPart | None = None
            try:
                await self._freeze_upper_id(state, budget)
                part = ExportPart(
                    format,
                    prepared.chat,
                    kind="history",
                    generated_at=ctx.clock.utc_now(),
                    upper_id=state.upper_id,
                    date_from=state.date_from,
                    date_to=state.date_to,
                    max_bytes=MAX_EXPORT_BYTES,
                )
                await self._fill(part, state, budget, result)
            except BudgetExhausted:
                pass
            except ToolError as exc:
                result.issues.append(ctx.temporary_issue(exc, state.chat_id))
            if result.omitted_count:
                result.issues.append(omitted_summary(result.omitted_count))
            result.complete = state.phase == "done"
            result.upper_message_id = state.upper_id
            receipt = None
            if not result.complete:
                receipt = ctx.checkpoints.put(
                    state, prepared.binding, retain=checkpoint, pending=True
                )
                result.next_checkpoint, result.checkpoint_expires_at = (
                    receipt.token,
                    receipt.expires_at,
                )
            await ctx.publish(
                result,
                prepared,
                receipt=receipt,
                part=part,
                exclude={"export"},
                archive_source=archive_source(state, prepared.chat, format)
                if save and part is not None
                else None,
            )
            return result

    async def _freeze_upper_id(self, state: CheckpointState, budget: ReadBudget) -> None:
        """The first part fixes the newest message ID below the upper date."""
        if state.upper_id is not None:
            return
        ctx = self.context
        head = await budget.call(
            lambda: ctx.backend.recent_observations(state.chat_id, 1, before=state.date_to)
        )
        state.upper_id = max((o.message.message_ref.message_id for o in head), default=0)

    async def _fill(
        self,
        part: ExportPart,
        state: CheckpointState,
        budget: ReadBudget,
        result: ExportHistoryResult,
    ) -> None:
        date_to = state.date_to
        assert date_to is not None

        def accept(observed: Observation) -> bool:
            message = observed.message
            if (
                state.date_from is not None and message.sent_at < state.date_from
            ) or message.sent_at >= date_to:
                return True
            record = message if observed.exportable else protected_record(message.message_ref)
            if not part.add(record):
                return False
            if observed.exportable:
                result.exported_count += 1
            else:
                result.omitted_count += 1
            return True

        while state.phase != "done" and part.records < MAX_EXPORT_RECORDS:
            batch_limit = min(100, MAX_EXPORT_RECORDS - part.records)
            if not await read_forward(
                self.context,
                state,
                budget,
                limit=batch_limit,
                accept=accept,
                done="done",
                retry="checkpoint",
            ):
                return


def archive_source(state: CheckpointState, chat: Chat, format: ExportFormat) -> ArchiveSource:
    return ArchiveSource(
        tool="export_history",
        chat_id=state.chat_id,
        title=chat.title,
        username=chat.username,
        format=format,
        date_from=state.date_from,
        date_to=state.date_to,
        upper_message_id=state.upper_id,
    )
