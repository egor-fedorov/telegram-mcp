"""A single Telegram search stream with a compound, retryable checkpoint."""

from dataclasses import dataclass, field, replace
from datetime import datetime

from telegram_mcp.application.ports import (
    Clock,
    CursorPort,
    ExecutionLimits,
    GlobalOffset,
    MessagePort,
)
from telegram_mcp.application.requests import SearchRead
from telegram_mcp.application.retry import RetryState
from telegram_mcp.domain.models import Issue, Message, Page, Scope, ToolError
from telegram_mcp.domain.text import clip


@dataclass
class GlobalQueryState:
    scope: Scope
    date_to: datetime
    offset: GlobalOffset = field(default_factory=GlobalOffset)
    seen_offsets: set[GlobalOffset] = field(default_factory=lambda: {GlobalOffset()})
    seen_refs: set[str] = field(default_factory=set)
    last_sent_at: datetime | None = None
    buffer: list[Message] = field(default_factory=list)
    exhausted: bool = False
    issues: list[Issue] = field(default_factory=list)
    retry: RetryState = field(default_factory=RetryState)


class GlobalSearchPager:
    def __init__(
        self, messages: MessagePort, cursors: CursorPort, clock: Clock, limits: ExecutionLimits
    ):
        self.messages = messages
        self.cursors = cursors
        self.clock = clock
        self.limits = limits

    async def page(
        self, state: GlobalQueryState, request: SearchRead, *, fingerprint: str, limit: int
    ) -> Page:
        request = replace(request, filters=replace(request.filters, date_to=state.date_to))
        output: list[Message] = []
        remaining = self.limits.rpc_pages_per_call
        deadline = self.clock.monotonic() + self.limits.page_deadline_seconds
        failed = False
        while len(output) < limit:
            if state.buffer:
                output.append(
                    clip(
                        state.buffer.pop(0),
                        min(self.limits.text_limit, self.limits.text_budget // limit),
                    )
                )
                continue
            if (
                state.exhausted
                or failed
                or remaining <= 0
                or self.clock.monotonic() >= deadline
                or not state.retry.ready(self.clock)
            ):
                break
            remaining -= 1
            try:
                batch = await self.messages.search_global(
                    request,
                    offset=state.offset,
                    limit=self.limits.page_size,
                    chat_ids=state.scope.chat_ids,
                )
                if not batch.exhausted and batch.next_offset in state.seen_offsets:
                    raise ToolError("PAGINATION_STALLED", "Telegram returned a non-advancing page.")
                buffered: list[Message] = []
                seen = set(state.seen_refs)
                last_sent_at = state.last_sent_at
                for message in batch.messages:
                    if message.message_ref.key in seen:
                        continue
                    if last_sent_at is not None and message.sent_at > last_sent_at:
                        raise ToolError(
                            "PAGINATION_INCOMPLETE",
                            "Telegram changed global search ordering. Restart the search.",
                        )
                    buffered.append(message)
                    seen.add(message.message_ref.key)
                    last_sent_at = message.sent_at
                state.offset = batch.next_offset
                state.seen_offsets.add(batch.next_offset)
                state.seen_refs = seen
                state.last_sent_at = last_sent_at
                state.buffer = buffered
                state.exhausted = batch.exhausted
                for issue in batch.issues:
                    if issue not in state.issues:
                        state.issues.append(issue)
                state.retry.clear()
            except ToolError as exc:
                failed = True
                if exc.retryable:
                    state.retry.record(exc, None, self.clock)
                else:
                    state.retry.clear()
                    state.issues.append(exc.issue())
                    state.exhausted = True
        pending = bool(state.buffer) or not state.exhausted
        issues = list(state.issues)
        retry_issue = state.retry.current_issue(self.clock)
        if retry_issue is not None:
            issues.append(retry_issue)
        return Page(
            messages=output,
            next_cursor=self.cursors.put("search", fingerprint, state) if pending else None,
            scope=state.scope.model_copy(
                update={
                    "pending_chat_ids": list(state.scope.chat_ids) if pending else [],
                    "complete": not pending and not issues,
                }
            ),
            issues=issues,
        )
