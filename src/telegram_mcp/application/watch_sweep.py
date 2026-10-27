"""One bounded pass over a watch's remaining sources, collected into a candidate batch."""

from collections.abc import Awaitable, Callable

from telegram_mcp.application.change_ports import Observation
from telegram_mcp.application.change_reader import advance_changes
from telegram_mcp.application.incremental import BudgetExhausted, IncrementalContext
from telegram_mcp.application.watch_probe import WatchProbe
from telegram_mcp.domain.changes import Change
from telegram_mcp.domain.exporting import protected_record
from telegram_mcp.domain.models import Issue, ToolError
from telegram_mcp.domain.text import clip
from telegram_mcp.domain.watches import WatchChangesResult, WatchPosition, WatchProgress


class WatchSweep:
    """Visit each remaining source at most once, within delivery, text and RPC budgets.

    The sweep mutates only its private candidate position; the caller decides
    whether it becomes a pending batch or the committed position.
    """

    def __init__(
        self,
        ctx: IncrementalContext,
        name: str,
        candidate: WatchPosition,
        limit: int,
        authorize: Callable[[str], Awaitable[None]],
    ):
        self.ctx, self.candidate, self.limit, self.authorize = ctx, candidate, limit, authorize
        self.result = WatchChangesResult(name=name)
        self.budget = ctx.budget()
        self.probe = WatchProbe(ctx, candidate, self.budget, authorize)
        self.text_left = ctx.limits.text_budget
        self.delivered: set[str] = set()
        self.issues: list[Issue] = []
        self.attempted: set[str] = set()

    async def run(self) -> WatchChangesResult:
        # A partial source rotates behind its peers; a failed source is retried
        # in the next sweep.
        for cid in list(self.candidate.remaining):
            if cid not in self.candidate.remaining:
                continue
            if self._exhausted() or not await self._visit(cid):
                break
        return self._finish()

    def emit(self, change: Change, observed: Observation | None) -> bool:
        result = self.result
        if result.returned_count >= self.limit or self.text_left <= 0:
            return False
        self.delivered.add(change.message_ref.chat_id)
        if observed is not None and not observed.exportable:
            result.omitted_count += 1
            self.issues.append(protected_record(change.message_ref, pending_batch=True))
        else:
            if change.message is not None:
                message = clip(change.message, min(self.text_left, self.ctx.limits.text_limit))
                self.text_left -= len(message.text)
                change = change.model_copy(update={"message": message})
            result.changes.append(change)
        result.returned_count += 1
        return True

    def _exhausted(self) -> bool:
        return (
            self.result.returned_count >= self.limit
            or self.text_left <= 0
            or self.budget.calls >= self.ctx.limits.rpc_pages_per_call
            or self.ctx.clock.monotonic() >= self.budget.deadline
        )

    async def _visit(self, cid: str) -> bool:
        """Read one source; False when the whole sweep must stop."""
        candidate = self.candidate
        try:
            await self.probe.prepare(cid)
            if cid not in candidate.remaining:
                return True
            candidate.remaining.remove(cid)
            candidate.errors.pop(cid, None)
            self.attempted.add(cid)
            await self.authorize(cid)
            # Metadata shares the deadline, but cannot consume the only
            # data page when rpc_pages_per_call is configured to one.
            await self.probe.check_source(cid)
            scan = await advance_changes(
                self.ctx,
                candidate.sources[cid],
                self.budget,
                self.emit,
                limit=self.limit - self.result.returned_count,
                isolate_errors=True,
            )
            if scan.issues:
                candidate.errors[cid] = scan.issues[0]
            elif candidate.sources[cid].phase != "idle":
                candidate.remaining.append(cid)
            else:
                self.probe.complete(cid)
        except BudgetExhausted:
            if cid not in candidate.remaining:
                candidate.remaining.insert(0, cid)
            return False
        except ToolError as exc:
            candidate.errors[cid] = (
                self.ctx.temporary_issue(exc, cid) if exc.retryable else exc.issue(cid)
            )
            if exc.retryable and cid in candidate.remaining:
                # A batch probe failed before this source was consumed.
                return False
        # An account-wide FloodWait must stop subsequent requests now.
        if cid in candidate.errors and candidate.errors[cid].code == "RATE_LIMITED":
            if cid not in candidate.remaining:
                candidate.remaining.insert(0, cid)
            return False
        return True

    def _finish(self) -> WatchChangesResult:
        result, candidate = self.result, self.candidate
        result.complete = not candidate.remaining and not candidate.errors
        if result.complete:
            candidate.completed_at = candidate.cycle_started_at
        self.issues.extend(candidate.errors.values())
        result.issues, result.issue_count = self.issues[:20], len(self.issues)
        result.progress = WatchProgress(
            source_count=len(candidate.members),
            pending_source_count=len(set(candidate.remaining) | candidate.errors.keys()),
            sources_this_call=len(self.attempted | self.probe.visited),
            rpc_pages_this_call=self.budget.calls,
        )
        return result
