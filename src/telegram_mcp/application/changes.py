"""Poll new IDs and a bounded set of previously observed current revisions."""

from telegram_mcp.application.change_ports import MAX_EXPORT_BYTES, Observation
from telegram_mcp.application.change_reader import advance_changes
from telegram_mcp.application.incremental import IncrementalContext
from telegram_mcp.domain.archiving import ArchiveSource
from telegram_mcp.domain.changes import (
    Change,
    ChangeOutput,
    ChangesResult,
)
from telegram_mcp.domain.exporting import ExportPart
from telegram_mcp.domain.models import Issue, ToolError
from telegram_mcp.domain.text import clip


class ChangesService:
    def __init__(self, context: IncrementalContext):
        self.context = context

    async def get_changes(
        self,
        chat_ref: str | None = None,
        checkpoint: str | None = None,
        limit: int = 30,
        output: ChangeOutput = "inline",
        save: bool = False,
    ) -> ChangesResult:
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or output not in ("inline", "jsonl", "markdown")
        ):
            raise ToolError(
                "INVALID_ARGUMENT", "Use limit 1..100 and output inline, jsonl or markdown."
            )
        ctx = self.context
        if save:
            if output == "inline":
                raise ToolError("INVALID_ARGUMENT", "save=true requires output=jsonl or markdown.")
            ctx.archive.require_enabled()
        async with ctx.request():
            prepared = await ctx.prepare("changes", chat_ref, checkpoint)
            state = prepared.state
            result = ChangesResult(chat_id=state.chat_id)
            if output != "inline":
                await ctx.backend.check_export_source(state.chat_id)
            budget = ctx.budget()
            part: ExportPart | None = None
            text_left = ctx.limits.text_budget

            def export_part() -> ExportPart:
                nonlocal part
                if part is None:
                    assert output != "inline"
                    part = ExportPart(
                        output,
                        prepared.chat,
                        kind="changes",
                        generated_at=ctx.clock.utc_now(),
                        upper_id=state.upper_id,
                        date_from=None,
                        date_to=state.date_to,
                        max_bytes=MAX_EXPORT_BYTES,
                    )
                return part

            def emit(change: Change, observation: Observation | None) -> bool:
                nonlocal text_left
                if result.returned_count >= limit or output == "inline" and text_left <= 0:
                    return False
                if output != "inline":
                    part = export_part()
                    if observation is not None and not observation.exportable:
                        omission = Issue(
                            code="EXPORT_PROTECTED",
                            message="Telegram restricts saving this message.",
                            message_ref=change.message_ref,
                        )
                        if not part.add(omission):
                            return False
                        result.omitted_count += 1
                    elif not part.add(change):
                        return False
                else:
                    if change.message is not None:
                        message = clip(change.message, min(text_left, ctx.limits.text_limit))
                        change = change.model_copy(update={"message": message})
                        text_left -= len(message.text)
                    result.changes.append(change)
                result.returned_count += 1
                return True

            scan = await advance_changes(ctx, state, budget, emit, limit=limit)
            result.baseline_created = scan.baseline_created
            result.rechecked_message_ids = scan.rechecked
            result.issues = scan.issues
            if result.omitted_count:
                result.issues.append(
                    Issue(
                        code="EXPORT_PROTECTED",
                        message=f"{result.omitted_count} protected records omitted; see export.",
                    )
                )
            result.complete = state.phase == "idle" and not any(
                issue.retry_after_seconds is not None or issue.code != "EXPORT_PROTECTED"
                for issue in result.issues
            )
            result.upper_message_id = state.upper_id
            result.tracked_count = len(state.seen)
            receipt = ctx.checkpoints.put(state, prepared.binding, retain=checkpoint, pending=True)
            result.next_checkpoint, result.checkpoint_expires_at = receipt.token, receipt.expires_at
            try:
                if output != "inline" and state.upper_id is not None:
                    part = export_part()
                    data = part.finish(
                        result.model_dump(mode="json", exclude={"changes", "export"})
                    )
                    result.export = await ctx.exports.save(
                        data, part.format, state.chat_id, prepared.binding
                    )
                    if save:
                        result.export.saved_file = await ctx.archive.save(
                            data,
                            result.export.name,
                            result.export.sha256,
                            mime_type=result.export.mime_type,
                            source=ArchiveSource(
                                tool="get_changes",
                                chat_id=state.chat_id,
                                title=prepared.chat.title,
                                username=prepared.chat.username,
                                format=part.format,
                                date_to=state.date_to,
                                upper_message_id=state.upper_id,
                            ),
                        )
                ctx.checkpoints.confirm(receipt.token)
            except BaseException:
                ctx.checkpoints.discard(receipt.token)
                if result.export is not None:
                    if result.export.saved_file is not None:
                        await ctx.archive.discard(result.export.saved_file)
                    await ctx.exports.discard(result.export.resource_uri)
                raise
            return result
