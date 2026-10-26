"""Fair source traversal with ordering confined to each returned page."""

from dataclasses import replace

from telegram_mcp.application.common import check_limit
from telegram_mcp.application.message_pager import QueryState, finish_page, load_stream
from telegram_mcp.application.ports import Clock, CursorPort, ExecutionLimits, MessagePort
from telegram_mcp.application.requests import SearchRead
from telegram_mcp.domain.models import Message, Page
from telegram_mcp.domain.text import clip


class ProgressiveSearchPager:
    def __init__(
        self, messages: MessagePort, cursors: CursorPort, clock: Clock, limits: ExecutionLimits
    ):
        self.messages, self.cursors, self.clock, self.limits = messages, cursors, clock, limits

    async def page(
        self, state: QueryState, request: SearchRead, *, fingerprint: str, limit: int
    ) -> Page:
        check_limit(limit)
        request = replace(request, filters=replace(request.filters, date_to=state.date_to))
        deadline = self.clock.monotonic() + self.limits.page_deadline_seconds
        calls = 0
        failed: set[str] = set()
        output: list[Message] = []
        while len(output) < limit:
            selected = None
            for _ in state.streams:
                stream = state.streams[state.next_source_index]
                state.next_source_index = (state.next_source_index + 1) % len(state.streams)
                if stream.buffer or (
                    not stream.exhausted
                    and stream.chat_id not in failed
                    and stream.retry.ready(self.clock)
                    and calls < self.limits.rpc_pages_per_call
                    and self.clock.monotonic() < deadline
                ):
                    selected = stream
                    break
            if selected is None:
                break
            if not selected.buffer:
                calls += 1
                if not await load_stream(
                    self.messages,
                    state,
                    selected,
                    request,
                    min(self.limits.page_size, limit - len(output)),
                    self.clock,
                ):
                    failed.add(selected.chat_id)
                    continue
            count = min(len(selected.buffer), limit - len(output))
            output.extend(
                clip(message, min(self.limits.text_limit, self.limits.text_budget // limit))
                for message in selected.buffer[:count]
            )
            del selected.buffer[:count]
        output.sort(
            key=lambda m: (m.sent_at, int(m.message_ref.chat_id), m.message_ref.message_id),
            reverse=not request.filters.oldest_first,
        )
        return finish_page(
            state,
            output,
            self.cursors,
            self.clock,
            kind="search",
            fingerprint=fingerprint,
            request=request,
            rpc_pages=calls,
            ordering="page",
        )
