"""Ordered message stream pagination, independent of source selection and access."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from telegram_mcp.application.common import check_limit
from telegram_mcp.application.ports import (
    Clock,
    CursorPort,
    ExecutionLimits,
    MessageOffset,
    MessagePort,
)
from telegram_mcp.application.requests import MessageRead, SearchRead
from telegram_mcp.application.retry import RetryState
from telegram_mcp.domain.models import Issue, Message, Page, Scope, SearchProgress, ToolError
from telegram_mcp.domain.text import clip


@dataclass
class Stream:
    chat_id: str
    offset: MessageOffset = 0
    buffer: list[Message] = field(default_factory=list)
    exhausted: bool = False
    started: bool = False
    retry: RetryState = field(default_factory=RetryState)


@dataclass
class QueryState:
    streams: list[Stream]
    scope: Scope
    date_to: datetime
    explicit_chat_ids: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    next_source_index: int = 0


class MessagePager:
    def __init__(
        self, messages: MessagePort, cursors: CursorPort, clock: Clock, limits: ExecutionLimits
    ):
        self.messages = messages
        self.cursors = cursors
        self.clock = clock
        self.limits = limits

    async def page(
        self,
        state: QueryState,
        request: MessageRead,
        *,
        kind: str,
        fingerprint: str,
        limit: int,
        topic_id: int | None = None,
    ) -> Page:
        check_limit(limit)
        remaining = self.limits.rpc_pages_per_call
        deadline = self.clock.monotonic() + self.limits.page_deadline_seconds
        output: list[Message] = []
        oldest = request.filters.oldest_first
        request = replace(request, filters=replace(request.filters, date_to=state.date_to))
        failed_this_call: set[str] = set()
        while len(output) < limit:
            # Prime every stream before choosing the next globally ordered message.
            while remaining > 0 and self.clock.monotonic() < deadline:
                stream = None
                for _ in state.streams:
                    candidate = state.streams[state.next_source_index]
                    state.next_source_index = (state.next_source_index + 1) % len(state.streams)
                    if (
                        not candidate.buffer
                        and not candidate.exhausted
                        and candidate.chat_id not in failed_this_call
                        and candidate.retry.ready(self.clock)
                    ):
                        stream = candidate
                        break
                if stream is None:
                    break
                remaining -= 1
                if not await load_stream(
                    self.messages, state, stream, request, self.limits.page_size, self.clock
                ):
                    failed_this_call.add(stream.chat_id)
            if any(not stream.buffer and not stream.exhausted for stream in state.streams):
                break
            candidates = [stream for stream in state.streams if stream.buffer]
            if not candidates:
                break

            def key(stream: Stream) -> tuple[datetime, int, int]:
                head = stream.buffer[0]
                return head.sent_at, int(stream.chat_id), head.message_ref.message_id

            selected = min(candidates, key=key) if oldest else max(candidates, key=key)
            output.append(
                clip(
                    selected.buffer.pop(0),
                    min(self.limits.text_limit, self.limits.text_budget // limit),
                )
            )
        return finish_page(
            state,
            output,
            self.cursors,
            self.clock,
            kind=kind,
            fingerprint=fingerprint,
            request=request,
            rpc_pages=self.limits.rpc_pages_per_call - remaining,
            topic_id=topic_id,
        )


async def load_stream(
    messages: MessagePort,
    state: QueryState,
    stream: Stream,
    request: MessageRead,
    limit: int,
    clock: Clock,
) -> bool:
    """Commit a validated batch, or retain the position and record its failure."""
    try:
        batch = await messages.read(stream.chat_id, request, offset=stream.offset, limit=limit)
        if not batch.exhausted and batch.next_offset == stream.offset:
            raise ToolError("PAGINATION_STALLED", "Telegram returned a non-advancing page.")
        stream.offset = batch.next_offset
        for issue in batch.issues:
            if issue not in state.issues:
                state.issues.append(issue)
        stream.buffer = batch.messages
        stream.exhausted = batch.exhausted
        stream.started = True
        stream.retry.clear()
        return True
    except ToolError as exc:
        if exc.retryable:
            stream.retry.record(exc, stream.chat_id, clock)
        else:
            stream.retry.clear()
            state.issues.append(exc.issue(stream.chat_id))
            stream.exhausted = True
        return False


def finish_page(
    state: QueryState,
    output: list[Message],
    cursors: CursorPort,
    clock: Clock,
    *,
    kind: str,
    fingerprint: str,
    request: MessageRead,
    rpc_pages: int,
    ordering: Literal["page", "global"] = "global",
    topic_id: int | None = None,
) -> Page:
    pending = [stream.chat_id for stream in state.streams if stream.buffer or not stream.exhausted]
    issues = list(state.issues)
    for stream in state.streams:
        issue = stream.retry.current_issue(clock)
        if issue is not None:
            issues.append(issue)
    token = cursors.put(kind, fingerprint, state) if pending else None
    scope = state.scope.model_copy(
        update={"pending_chat_ids": pending, "complete": not pending and not issues}
    )
    order: Literal["newest_first", "oldest_first"] = (
        "oldest_first" if request.filters.oldest_first else "newest_first"
    )
    return Page(
        messages=output,
        next_cursor=token,
        scope=scope,
        issues=issues,
        order=order,
        ordering=ordering,
        topic_id=topic_id,
        progress=SearchProgress(
            sources_total=len(state.streams),
            sources_started=sum(s.started for s in state.streams),
            sources_ready=sum(bool(s.buffer) or s.exhausted for s in state.streams),
            sources_exhausted=sum(s.exhausted and not s.buffer for s in state.streams),
            rpc_pages_this_call=rpc_pages,
        )
        if isinstance(request, SearchRead)
        else None,
    )
