"""Poll new IDs and a bounded set of previously observed current revisions."""

from telegram_mcp.application.change_ports import MAX_EXPORT_BYTES, RECHECK_LIMIT, Observation
from telegram_mcp.application.incremental import BudgetExhausted, IncrementalContext, ReadBudget
from telegram_mcp.domain.changes import (
    Change,
    ChangeOutput,
    ChangesResult,
    CheckpointState,
    SeenMessage,
)
from telegram_mcp.domain.exporting import ExportPart
from telegram_mcp.domain.models import Issue, MessageRef, ToolError
from telegram_mcp.domain.text import clip


def remember(state: CheckpointState, mid: int, revision: str, available: bool = True) -> None:
    state.seen[mid] = SeenMessage(revision=revision, available=available)
    while len(state.seen) > RECHECK_LIMIT:
        del state.seen[min(state.seen)]


class ChangesService:
    def __init__(self, context: IncrementalContext):
        self.context = context

    async def get_changes(
        self,
        chat_ref: str | None = None,
        checkpoint: str | None = None,
        limit: int = 30,
        output: ChangeOutput = "inline",
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
        async with ctx.request():
            prepared = await ctx.prepare("changes", chat_ref, checkpoint)
            state = prepared.state
            result = ChangesResult(chat_id=state.chat_id)
            if output != "inline":
                await ctx.backend.check_export_source(state.chat_id)
            budget = ReadBudget(ctx.clock, ctx.limits)
            part: ExportPart | None = None
            text_left = ctx.limits.text_budget

            def emit(change: Change, observation: Observation | None) -> bool:
                nonlocal text_left
                if result.returned_count >= limit or output == "inline" and text_left <= 0:
                    return False
                if part is not None:
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

            try:
                if state.phase == "baseline":
                    recent = await budget.call(
                        lambda: ctx.backend.recent_observations(state.chat_id, RECHECK_LIMIT)
                    )
                    for observed in recent:
                        remember(state, observed.message.message_ref.message_id, observed.revision)
                    state.position = max(state.seen, default=0)
                    state.upper_id = state.position
                    state.phase = "idle"
                    baseline = True
                else:
                    baseline = False
                if state.phase == "idle" and not baseline:
                    recent = await budget.call(
                        lambda: ctx.backend.recent_observations(state.chat_id, 1)
                    )
                    state.upper_id = max(
                        [state.position, *(o.message.message_ref.message_id for o in recent)]
                    )
                    state.date_to = ctx.clock.utc_now()
                    state.recheck_ids = sorted(state.seen)
                    state.recheck_index = 0
                    state.phase = "recheck"
                if output != "inline":
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
                if state.phase == "recheck":
                    mids = state.recheck_ids[state.recheck_index :]
                    observations = (
                        await budget.call(lambda: ctx.backend.observe_messages(state.chat_id, mids))
                        if mids
                        else {}
                    )
                    for mid in mids:
                        old, observed = state.seen[mid], observations.get(mid)
                        change = None
                        if observed is None and old.available:
                            change = Change(
                                kind="unavailable",
                                message_ref=MessageRef(chat_id=state.chat_id, message_id=mid),
                                revision="unavailable:" + old.revision,
                                observed_at=ctx.clock.utc_now(),
                            )
                        elif observed is not None and (
                            not old.available or old.revision != observed.revision
                        ):
                            change = Change(
                                kind="updated",
                                message_ref=observed.message.message_ref,
                                revision=observed.revision,
                                observed_at=ctx.clock.utc_now(),
                                message=observed.message,
                            )
                        if change is not None and not emit(change, observed):
                            break
                        remember(
                            state,
                            mid,
                            observed.revision if observed else old.revision,
                            observed is not None,
                        )
                        state.recheck_index += 1
                        result.rechecked_message_ids.append(mid)
                    if state.recheck_index == len(state.recheck_ids):
                        state.phase = "new"
                        state.recheck_ids = []
                        state.recheck_index = 0
                while (
                    state.phase == "new"
                    and result.returned_count < limit
                    and (output != "inline" or text_left > 0)
                ):
                    assert state.upper_id is not None
                    if state.position >= state.upper_id:
                        state.phase = "idle"
                        break
                    batch = await budget.call(
                        lambda: ctx.backend.read_incremental(
                            state.chat_id,
                            after_id=state.position,
                            through_id=state.upper_id or 0,
                            limit=min(100, limit - result.returned_count),
                        )
                    )
                    accepted = True
                    for observed in batch.observations:
                        message = observed.message
                        change = Change(
                            kind="new",
                            message_ref=message.message_ref,
                            revision=observed.revision,
                            observed_at=ctx.clock.utc_now(),
                            message=message,
                        )
                        if not emit(change, observed):
                            accepted = False
                            break
                        state.position = message.message_ref.message_id
                        remember(state, state.position, observed.revision)
                    if not accepted:
                        break
                    if batch.exhausted:
                        state.position = state.upper_id
                        state.phase = "idle"
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
            result.complete = state.phase == "idle" and not any(
                issue.retry_after_seconds is not None or issue.code != "EXPORT_PROTECTED"
                for issue in result.issues
            )
            result.upper_message_id = state.upper_id
            result.tracked_count = len(state.seen)
            receipt = ctx.checkpoints.put(state, prepared.binding, retain=checkpoint)
            result.next_checkpoint, result.checkpoint_expires_at = receipt.token, receipt.expires_at
            if part is not None:
                data = part.finish(result.model_dump(mode="json", exclude={"changes", "export"}))
                result.export = await ctx.exports.save(
                    data, part.format, state.chat_id, prepared.binding
                )
            return result
