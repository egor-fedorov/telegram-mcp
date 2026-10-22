"""Distinct history, search, pin and discussion scenarios sharing a message pager."""

from datetime import datetime
from typing import Literal

from telegram_mcp.application.common import check_limit, check_topic_id
from telegram_mcp.application.global_search import GlobalQueryState, GlobalSearchPager
from telegram_mcp.application.message_pager import MessagePager
from telegram_mcp.application.query_preparation import QueryPreparation, QuerySpec, SourceSelection
from telegram_mcp.application.requests import (
    HistoryRead,
    MessageRead,
    PinnedRead,
    ReadFilters,
    SearchRead,
    ThreadRead,
    TopicRead,
)
from telegram_mcp.domain.models import ContentType, Page, ToolError


class QueryService:
    def __init__(
        self, preparation: QueryPreparation, pager: MessagePager, global_pager: GlobalSearchPager
    ):
        self.preparation = preparation
        self.pager = pager
        self.global_pager = global_pager

    async def execute(self, spec: QuerySpec, limit: int, cursor: str | None) -> Page:
        check_limit(limit)
        state, fingerprint = await self.preparation.prepare(spec, cursor)
        if isinstance(state, GlobalQueryState):
            if not isinstance(spec.read, SearchRead):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
            return await self.global_pager.page(
                state, spec.read, fingerprint=fingerprint, limit=limit
            )
        return await self.pager.page(
            state,
            spec.read,
            kind=spec.kind,
            fingerprint=fingerprint,
            limit=limit,
            topic_id=spec.read.topic_id if isinstance(spec.read, TopicRead) else None,
        )

    async def get_history(
        self,
        chat_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        order: Literal["newest_first", "oldest_first"] = "newest_first",
        limit: int = 30,
        cursor: str | None = None,
        chat_ref: str | None = None,
        topic_id: int | None = None,
    ) -> Page:
        if (chat_id is None) == (chat_ref is None):
            raise ToolError("INVALID_ARGUMENT", "Provide exactly one of chat_id or chat_ref.")
        check_limit(limit)
        check_topic_id(topic_id)
        filters = ReadFilters(
            date_from=date_from, date_to=date_to, oldest_first=order == "oldest_first"
        )
        read: MessageRead = (
            HistoryRead(filters) if topic_id is None else TopicRead(topic_id, filters)
        )
        return await self.execute(
            QuerySpec(
                read,
                SourceSelection(
                    chat_ids=[chat_id] if chat_id is not None else None, source_ref=chat_ref
                ),
            ),
            limit,
            cursor,
        )

    async def get_pinned_messages(
        self,
        chat_ref: str,
        limit: int = 30,
        cursor: str | None = None,
        topic_id: int | None = None,
    ) -> Page:
        chat_ref = chat_ref.strip()
        if not chat_ref:
            raise ToolError("INVALID_ARGUMENT", "chat_ref must not be empty.")
        check_limit(limit)
        check_topic_id(topic_id)
        read: MessageRead = (
            PinnedRead() if topic_id is None else TopicRead(topic_id, pinned_only=True)
        )
        return await self.execute(
            QuerySpec(read, SourceSelection(source_ref=chat_ref)), limit, cursor
        )

    async def search_messages(
        self,
        *,
        query: str,
        chat_ids: list[str] | None = None,
        folder_ids: list[int] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sender_id: str | None = None,
        content_type: ContentType | None = None,
        has_link: bool | None = None,
        limit: int = 30,
        cursor: str | None = None,
        order: Literal["newest_first", "oldest_first"] = "newest_first",
    ) -> Page:
        check_limit(limit)
        if not query.strip() and has_link is not True:
            raise ToolError(
                "INVALID_ARGUMENT",
                "Search requires a query or has_link=true; use get_history otherwise.",
            )
        if not query.strip():
            query = ""
        read = SearchRead(
            query,
            ReadFilters(date_from, date_to, content_type, order == "oldest_first", has_link),
            sender_id,
        )
        return await self.execute(
            QuerySpec(read, SourceSelection(chat_ids, folder_ids)), limit, cursor
        )

    async def thread(self, chat_id: str, thread_id: int, limit: int, cursor: str | None) -> Page:
        return await self.execute(
            QuerySpec(ThreadRead(thread_id), SourceSelection(chat_ids=[chat_id])), limit, cursor
        )
