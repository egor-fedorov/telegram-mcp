"""Forum topic pagination keeps its native offset and exhaustion semantics."""

from dataclasses import dataclass, field

from telegram_mcp.application.access import AccessFactory
from telegram_mcp.application.common import check_limit, fingerprint
from telegram_mcp.application.ports import ChatPort, Clock, CursorPort, ForumPort, TopicOffset
from telegram_mcp.application.retry import RetryState
from telegram_mcp.domain.models import ForumTopic, Issue, Scope, ToolError, TopicPage


@dataclass
class TopicState:
    scope: Scope
    offset: TopicOffset = field(default_factory=TopicOffset)
    buffer: list[ForumTopic] = field(default_factory=list)
    seen_ids: set[int] = field(default_factory=set)
    seen_offsets: set[TopicOffset] = field(default_factory=lambda: {TopicOffset()})
    exhausted: bool = False
    order_by_create_date: bool | None = None
    telegram_total_count: int | None = None
    issues: list[Issue] = field(default_factory=list)
    retry: RetryState = field(default_factory=RetryState)


class TopicPager:
    def __init__(self, forums: ForumPort, cursors: CursorPort, clock: Clock):
        self.forums = forums
        self.cursors = cursors
        self.clock = clock

    async def page(
        self, chat_id: str, query: str | None, limit: int, state: TopicState, fp: str
    ) -> TopicPage:
        if not state.buffer and not state.exhausted and state.retry.ready(self.clock):
            try:
                batch = await self.forums.fetch_topics(
                    chat_id, offset=state.offset, limit=limit, query=query
                )
                if (
                    state.order_by_create_date is not None
                    and batch.order_by_create_date != state.order_by_create_date
                    and (batch.topics or not batch.exhausted)
                ):
                    raise ToolError(
                        "PAGINATION_CHANGED", "Topic order changed. Restart the topic listing."
                    )
                if not batch.exhausted and (
                    batch.next_offset is None or batch.next_offset in state.seen_offsets
                ):
                    raise ToolError(
                        "PAGINATION_STALLED", "Telegram returned a non-advancing topic page."
                    )
                if state.order_by_create_date is None or batch.topics or not batch.exhausted:
                    state.order_by_create_date = batch.order_by_create_date
                state.telegram_total_count = batch.telegram_total_count
                state.issues.extend(batch.issues)
                state.exhausted = batch.exhausted
                if batch.next_offset is not None:
                    state.offset = batch.next_offset
                    state.seen_offsets.add(batch.next_offset)
                for topic in batch.topics:
                    if topic.id not in state.seen_ids:
                        state.buffer.append(topic)
                        state.seen_ids.add(topic.id)
                state.retry.clear()
            except ToolError as exc:
                if exc.retryable:
                    state.retry.record(exc, chat_id, self.clock)
                else:
                    state.issues.append(exc.issue(chat_id))
                    state.retry.clear()
                    state.exhausted = True
        topics = state.buffer[:limit]
        state.buffer = state.buffer[limit:]
        issues = list(state.issues)
        issue = state.retry.current_issue(self.clock)
        if issue is not None:
            issues.append(issue)
        pending = bool(state.buffer) or not state.exhausted
        token = self.cursors.put("topics", fp, state) if pending else None
        order = (
            None
            if state.order_by_create_date is None
            else "creation_date"
            if state.order_by_create_date
            else "last_message_date"
        )
        return TopicPage(
            topics=topics,
            next_cursor=token,
            scope=state.scope.model_copy(
                update={
                    "pending_chat_ids": [chat_id] if pending else [],
                    "complete": not pending and not issues,
                }
            ),
            issues=issues,
            telegram_total_count=state.telegram_total_count,
            order=order,
        )


class TopicService:
    def __init__(
        self,
        chats: ChatPort,
        access: AccessFactory,
        cursors: CursorPort,
        clock: Clock,
        pager: TopicPager,
    ):
        self.chats = chats
        self.access = access
        self.cursors = cursors
        self.clock = clock
        self.pager = pager

    async def list_topics(
        self,
        chat_ref: str,
        query: str | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> TopicPage:
        check_limit(limit)
        chat_ref = chat_ref.strip()
        if not chat_ref:
            raise ToolError("INVALID_ARGUMENT", "chat_ref must not be empty.")
        if query is not None:
            query = query.strip()
            if not 1 <= len(query) <= 1000:
                raise ToolError(
                    "INVALID_ARGUMENT", "query must contain 1 to 1000 characters after trimming."
                )
        access = await self.access.load()
        chat = await self.chats.resolve_chat(chat_ref, refresh=True)
        fp = fingerprint([chat_ref, chat.id, query])
        if cursor:
            state = self.cursors.get(cursor, "topics", fp)
            if not isinstance(state, TopicState):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
        else:
            state = TopicState(Scope(chat_ids=[chat.id], resolved_at=self.clock.utc_now()))
        access.require_forum(chat)
        return await self.pager.page(chat.id, query, limit, state, fp)
