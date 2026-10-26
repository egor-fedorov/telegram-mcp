"""Export a frozen single-source history range in bounded, restartable parts."""

from datetime import datetime

from telegram_mcp.application.change_ports import MAX_EXPORT_BYTES, MAX_EXPORT_RECORDS
from telegram_mcp.application.incremental import BudgetExhausted, IncrementalContext, ReadBudget
from telegram_mcp.domain.changes import ExportFormat, ExportHistoryResult
from telegram_mcp.domain.exporting import ExportPart
from telegram_mcp.domain.models import Issue, ToolError


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
    ) -> ExportHistoryResult:
        if format not in ("jsonl", "markdown"):
            raise ToolError("INVALID_ARGUMENT", "Use format jsonl or markdown.")
        ctx = self.context
        async with ctx.request():
            prepared = await ctx.prepare("history", chat_ref, checkpoint, date_from, date_to)
            state = prepared.state
            assert state.date_to is not None
            await ctx.backend.check_export_source(state.chat_id)
            result = ExportHistoryResult(
                chat_id=state.chat_id, date_from=state.date_from, date_to=state.date_to
            )
            budget = ReadBudget(ctx.clock, ctx.limits)
            part: ExportPart | None = None
            try:
                if state.upper_id is None:
                    head = await budget.call(
                        lambda: ctx.backend.recent_observations(
                            state.chat_id, 1, before=state.date_to
                        )
                    )
                    state.upper_id = max(
                        (o.message.message_ref.message_id for o in head), default=0
                    )
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
                while state.phase != "done" and part.records < MAX_EXPORT_RECORDS:
                    if state.position >= state.upper_id:
                        state.phase = "done"
                        break
                    batch = await budget.call(
                        lambda: ctx.backend.read_incremental(
                            state.chat_id,
                            after_id=state.position,
                            through_id=state.upper_id or 0,
                            limit=min(100, MAX_EXPORT_RECORDS - part.records),
                            date_from=state.date_from,
                        )
                    )
                    accepted = True
                    for observed in batch.observations:
                        message = observed.message
                        if (
                            state.date_from is None or message.sent_at >= state.date_from
                        ) and message.sent_at < state.date_to:
                            record = (
                                message
                                if observed.exportable
                                else Issue(
                                    code="EXPORT_PROTECTED",
                                    message="Telegram restricts saving this message.",
                                    message_ref=message.message_ref,
                                )
                            )
                            if not part.add(record):
                                accepted = False
                                break
                            if observed.exportable:
                                result.exported_count += 1
                            else:
                                result.omitted_count += 1
                        state.position = message.message_ref.message_id
                    if not accepted:
                        break
                    if batch.exhausted:
                        state.position = state.upper_id
                        state.phase = "done"
                    elif not batch.observations and batch.next_offset <= state.position:
                        raise ToolError(
                            "TELEGRAM_UNAVAILABLE",
                            "History did not advance; retry this checkpoint.",
                        )
                    else:
                        state.position = max(state.position, batch.next_offset)
            except BudgetExhausted:
                pass
            except ToolError as exc:
                result.issues.append(ctx.temporary_issue(exc, state.chat_id))
            if result.omitted_count:
                result.issues.append(
                    Issue(
                        code="EXPORT_PROTECTED",
                        message=f"{result.omitted_count} protected records omitted; see export.",
                    )
                )
            result.complete = state.phase == "done"
            result.upper_message_id = state.upper_id
            if not result.complete:
                receipt = ctx.checkpoints.put(state, prepared.binding, retain=checkpoint)
                result.next_checkpoint, result.checkpoint_expires_at = (
                    receipt.token,
                    receipt.expires_at,
                )
            if part is not None:
                data = part.finish(result.model_dump(mode="json", exclude={"export"}))
                result.export = await ctx.exports.save(
                    data, format, state.chat_id, prepared.binding
                )
            return result
