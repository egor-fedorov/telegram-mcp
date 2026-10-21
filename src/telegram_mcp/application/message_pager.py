"""Ordered message stream pagination, independent of source selection and access."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from telegram_mcp.application.common import check_limit
from telegram_mcp.application.ports import Clock, CursorPort, ExecutionLimits, MessagePort
from telegram_mcp.application.requests import MessageRead
from telegram_mcp.application.retry import RetryState
from telegram_mcp.domain.models import Issue, Message, Page, Scope, ToolError
from telegram_mcp.domain.text import clip


@dataclass
class Stream:
    chat_id: str
    offset: int = 0
    buffer: list[Message] = field(default_factory=list)
    exhausted: bool = False
    retry: RetryState = field(default_factory=RetryState)


@dataclass
class QueryState:
    streams: list[Stream]
    scope: Scope
    date_to: datetime
    explicit_chat_ids: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


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
            for stream in state.streams:
                while not stream.buffer and not stream.exhausted:
                    if (
                        remaining <= 0
                        or self.clock.monotonic() >= deadline
                        or stream.chat_id in failed_this_call
                        or not stream.retry.ready(self.clock)
                    ):
                        break
                    remaining -= 1
                    try:
                        batch = await self.messages.read(
                            stream.chat_id,
                            request,
                            offset=stream.offset,
                            limit=self.limits.page_size,
                        )
                        if not batch.exhausted and batch.next_offset == stream.offset:
                            raise ToolError(
                                "PAGINATION_STALLED", "Telegram returned a non-advancing page."
                            )
                        stream.offset = batch.next_offset
                        state.issues.extend(batch.issues)
                        stream.buffer = batch.messages
                        stream.exhausted = batch.exhausted
                        stream.retry.clear()
                    except ToolError as exc:
                        if exc.retryable:
                            stream.retry.record(exc, stream.chat_id, self.clock)
                            failed_this_call.add(stream.chat_id)
                            break
                        stream.retry.clear()
                        state.issues.append(exc.issue(stream.chat_id))
                        stream.exhausted = True
            if any(not stream.buffer and not stream.exhausted for stream in state.streams):
                break
            candidates = [stream for stream in state.streams if stream.buffer]
            if not candidates:
                break

            def key(stream: Stream) -> tuple[datetime, str, int]:
                head = stream.buffer[0]
                return head.sent_at, stream.chat_id, head.message_ref.message_id

            selected = min(candidates, key=key) if oldest else max(candidates, key=key)
            output.append(
                clip(
                    selected.buffer.pop(0),
                    min(self.limits.text_limit, self.limits.text_budget // limit),
                )
            )
        pending = [
            stream.chat_id for stream in state.streams if stream.buffer or not stream.exhausted
        ]
        issues = list(state.issues)
        for stream in state.streams:
            issue = stream.retry.current_issue(self.clock)
            if issue is not None:
                issues.append(issue)
        token = self.cursors.put(kind, fingerprint, state) if pending else None
        scope = state.scope.model_copy(
            update={"pending_chat_ids": pending, "complete": not pending and not issues}
        )
        order: Literal["newest_first", "oldest_first"] = (
            "oldest_first" if oldest else "newest_first"
        )
        return Page(
            messages=output,
            next_cursor=token,
            scope=scope,
            issues=issues,
            order=order,
            topic_id=topic_id,
        )
