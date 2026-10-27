"""Poll new IDs and a bounded set of previously observed current revisions."""

from telegram_mcp.application.change_ports import MAX_EXPORT_BYTES, Observation
from telegram_mcp.application.change_reader import advance_changes
from telegram_mcp.application.incremental import IncrementalContext, PreparedRead
from telegram_mcp.domain.archiving import ArchiveSource
from telegram_mcp.domain.changes import (
    Change,
    ChangeOutput,
    ChangesResult,
)
from telegram_mcp.domain.exporting import ExportPart, omitted_summary, protected_record
from telegram_mcp.domain.models import ToolError
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
            delivery = ChangeDelivery(ctx, prepared, result, output, limit)
            scan = await advance_changes(ctx, state, budget, delivery.emit, limit=limit)
            result.baseline_created = scan.baseline_created
            result.rechecked_message_ids = scan.rechecked
            result.issues = scan.issues
            if result.omitted_count:
                result.issues.append(omitted_summary(result.omitted_count))
            result.complete = state.phase == "idle" and not any(
                issue.retry_after_seconds is not None or issue.code != "EXPORT_PROTECTED"
                for issue in result.issues
            )
            result.upper_message_id = state.upper_id
            result.tracked_count = len(state.seen)
            receipt = ctx.checkpoints.put(state, prepared.binding, retain=checkpoint, pending=True)
            result.next_checkpoint, result.checkpoint_expires_at = receipt.token, receipt.expires_at
            part = (
                delivery.export_part()
                if output != "inline" and state.upper_id is not None
                else None
            )
            await ctx.publish(
                result,
                prepared,
                receipt=receipt,
                part=part,
                exclude={"changes", "export"},
                archive_source=archive_source(prepared, part)
                if save and part is not None
                else None,
            )
            return result


class ChangeDelivery:
    """Collect inline changes within text budgets, or export records with explicit omissions."""

    def __init__(
        self,
        ctx: IncrementalContext,
        prepared: PreparedRead,
        result: ChangesResult,
        output: ChangeOutput,
        limit: int,
    ):
        self.ctx, self.prepared, self.result = ctx, prepared, result
        self.output, self.limit = output, limit
        self.part: ExportPart | None = None
        self.text_left = ctx.limits.text_budget

    def export_part(self) -> ExportPart:
        """Create the part lazily, once the pass has fixed its upper ID and date."""
        if self.part is None:
            output = self.output
            assert output != "inline"
            state = self.prepared.state
            self.part = ExportPart(
                output,
                self.prepared.chat,
                kind="changes",
                generated_at=self.ctx.clock.utc_now(),
                upper_id=state.upper_id,
                date_from=None,
                date_to=state.date_to,
                max_bytes=MAX_EXPORT_BYTES,
            )
        return self.part

    def emit(self, change: Change, observation: Observation | None) -> bool:
        inline = self.output == "inline"
        if self.result.returned_count >= self.limit or inline and self.text_left <= 0:
            return False
        if inline:
            self._append(change)
        elif not self._export(change, observation):
            return False
        self.result.returned_count += 1
        return True

    def _append(self, change: Change) -> None:
        if change.message is not None:
            message = clip(change.message, min(self.text_left, self.ctx.limits.text_limit))
            change = change.model_copy(update={"message": message})
            self.text_left -= len(message.text)
        self.result.changes.append(change)

    def _export(self, change: Change, observation: Observation | None) -> bool:
        part = self.export_part()
        if observation is None or observation.exportable:
            return part.add(change)
        if not part.add(protected_record(change.message_ref)):
            return False
        self.result.omitted_count += 1
        return True


def archive_source(prepared: PreparedRead, part: ExportPart) -> ArchiveSource:
    state = prepared.state
    return ArchiveSource(
        tool="get_changes",
        chat_id=state.chat_id,
        title=prepared.chat.title,
        username=prepared.chat.username,
        format=part.format,
        date_to=state.date_to,
        upper_message_id=state.upper_id,
    )
