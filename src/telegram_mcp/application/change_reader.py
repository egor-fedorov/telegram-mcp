"""Advance one source independently of token storage, delivery and export formats."""

from collections.abc import Callable
from dataclasses import dataclass, field

from telegram_mcp.application.change_ports import RECHECK_LIMIT, Observation
from telegram_mcp.application.incremental import BudgetExhausted, IncrementalContext, ReadBudget
from telegram_mcp.domain.changes import Change, CheckpointState, SeenMessage
from telegram_mcp.domain.models import Issue, MessageRef, ToolError


def remember(
    state: CheckpointState,
    mid: int,
    revision: str,
    available: bool = True,
    *,
    requires_recheck: bool = True,
) -> None:
    state.seen[mid] = SeenMessage(
        revision=revision, available=available, requires_recheck=requires_recheck
    )
    while len(state.seen) > RECHECK_LIMIT:
        del state.seen[min(state.seen)]


@dataclass
class ChangeScan:
    baseline_created: bool = False
    returned: int = 0
    rechecked: list[int] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


async def advance_changes(  # noqa: C901
    ctx: IncrementalContext,
    state: CheckpointState,
    budget: ReadBudget,
    emit: Callable[[Change, Observation | None], bool],
    *,
    limit: int,
    isolate_errors: bool = False,
) -> ChangeScan:
    """Mutate a private candidate; callers decide when that candidate is committed."""
    scan = ChangeScan()
    try:
        if state.phase == "baseline":
            recent = await budget.call(
                lambda: ctx.backend.recent_observations(
                    state.chat_id, RECHECK_LIMIT if state.date_from is None else 1
                )
            )
            state.upper_id = max(
                (item.message.message_ref.message_id for item in recent), default=0
            )
            state.date_to = ctx.clock.utc_now()
            if state.date_from is None:
                for item in recent:
                    remember(
                        state,
                        item.message.message_ref.message_id,
                        item.revision,
                        requires_recheck=item.message.poll is not None,
                    )
                state.position = state.upper_id
                state.phase = "idle"
                scan.baseline_created = True
                return scan
            state.phase = "new"
        elif state.phase == "idle":
            recent = await budget.call(lambda: ctx.backend.recent_observations(state.chat_id, 1))
            state.upper_id = max(
                [state.position, *(o.message.message_ref.message_id for o in recent)]
            )
            state.date_to = ctx.clock.utc_now()
            state.recheck_ids = sorted(state.seen)
            state.recheck_index = 0
            state.phase = "recheck"
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
                if change is not None:
                    if scan.returned >= limit or not emit(change, observed):
                        return scan
                    scan.returned += 1
                remember(
                    state,
                    mid,
                    observed.revision if observed else old.revision,
                    observed is not None,
                    requires_recheck=(
                        observed.message.poll is not None if observed else old.requires_recheck
                    ),
                )
                state.recheck_index += 1
                scan.rechecked.append(mid)
            state.phase = "new"
            state.recheck_ids = []
            state.recheck_index = 0
        while state.phase == "new" and scan.returned < limit:
            assert state.upper_id is not None
            if state.position >= state.upper_id:
                state.phase = "idle"
                break
            batch = await budget.call(
                lambda: ctx.backend.read_incremental(
                    state.chat_id,
                    after_id=state.position,
                    through_id=state.upper_id or 0,
                    limit=min(100, limit - scan.returned),
                    date_from=state.date_from,
                )
            )
            for observed in batch.observations:
                message = observed.message
                # Keep the domain boundary inclusive even if a backend returns
                # a date-boundary message outside its requested search range.
                if state.date_from is None or message.sent_at >= state.date_from:
                    change = Change(
                        kind="new",
                        message_ref=message.message_ref,
                        revision=observed.revision,
                        observed_at=ctx.clock.utc_now(),
                        message=message,
                    )
                    if not emit(change, observed):
                        return scan
                    scan.returned += 1
                    remember(
                        state,
                        message.message_ref.message_id,
                        observed.revision,
                        requires_recheck=message.poll is not None,
                    )
                state.position = message.message_ref.message_id
            if batch.exhausted:
                state.position = state.upper_id
                state.phase = "idle"
            elif not batch.observations and batch.next_offset <= state.position:
                raise ToolError(
                    "TELEGRAM_UNAVAILABLE", "History did not advance; retry this position."
                )
            else:
                state.position = max(state.position, batch.next_offset)
    except BudgetExhausted:
        pass
    except ToolError as exc:
        if isolate_errors and not exc.retryable:
            scan.issues.append(exc.issue(state.chat_id))
        else:
            scan.issues.append(ctx.temporary_issue(exc, state.chat_id))
    return scan
