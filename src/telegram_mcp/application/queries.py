"""Distinct history, search, pin and discussion scenarios sharing a message pager."""

from datetime import datetime
from typing import Literal

from telegram_mcp.application.common import check_limit, check_topic_id
from telegram_mcp.application.global_search import GlobalQueryState, GlobalSearchPager
from telegram_mcp.application.message_pager import MessagePager
from telegram_mcp.application.progressive_search import ProgressivePager
from telegram_mcp.application.query_preparation import QueryPreparation, QuerySpec, SourceSelection
from telegram_mcp.application.requests import (
    DEFAULT_MAX_SOURCES,
    BudgetRequest,
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
        self,
        preparation: QueryPreparation,
        pager: MessagePager,
        global_pager: GlobalSearchPager,
        progressive_pager: ProgressivePager,
    ):
        self.preparation = preparation
        self.pager = pager
        self.global_pager = global_pager
        self.progressive_pager = progressive_pager

    async def execute(self, spec: QuerySpec, limit: int, cursor: str | None) -> Page:
        check_limit(limit)
        state, fingerprint = await self.preparation.prepare(spec, cursor)
        if isinstance(state, GlobalQueryState):
            if not isinstance(spec.read, SearchRead):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
            return await self.global_pager.page(
                state, spec.read, fingerprint=fingerprint, limit=limit
            )
        if isinstance(spec.read, (SearchRead, HistoryRead)) and spec.read.ordering == "page":
            return await self.progressive_pager.page(
                state, spec.read, kind=spec.kind, fingerprint=fingerprint, limit=limit
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
        chat_ids: list[str] | None = None,
        folder_ids: list[int] | None = None,
        ordering: Literal["page", "global"] | None = None,
        max_sources: int | None = None,
        max_messages: int | None = None,
    ) -> Page:
        selectors = [chat_id, chat_ref, chat_ids, folder_ids]
        if sum(selector is not None for selector in selectors) != 1:
            raise ToolError(
                "INVALID_ARGUMENT",
                "Provide exactly one of chat_id, chat_ref, chat_ids or folder_ids.",
            )
        if chat_ids == [] or folder_ids == []:
            raise ToolError("INVALID_ARGUMENT", "chat_ids and folder_ids must not be empty.")
        check_limit(limit)
        check_topic_id(topic_id)
        if ordering not in (None, "page", "global"):
            raise ToolError("INVALID_ARGUMENT", "Use ordering page or global.")
        for value in (max_sources, max_messages):
            if value is not None and (type(value) is not int or not 1 <= value <= 1000):
                raise ToolError("INVALID_ARGUMENT", "max_sources and max_messages must be 1..1000.")
        plural = chat_ids is not None or folder_ids is not None
        if plural:
            if topic_id is not None:
                raise ToolError(
                    "INVALID_ARGUMENT", "topic_id requires a single chat_id or chat_ref."
                )
            if date_from is None or date_to is None:
                raise ToolError(
                    "INVALID_ARGUMENT",
                    "chat_ids and folder_ids require explicit date_from and date_to.",
                )
        budget = (
            BudgetRequest(max_sources or DEFAULT_MAX_SOURCES, max_messages)
            if plural or max_sources is not None or max_messages is not None
            else None
        )
        filters = ReadFilters(
            date_from=date_from, date_to=date_to, oldest_first=order == "oldest_first"
        )
        read: MessageRead = (
            HistoryRead(filters, ordering or ("page" if plural else "global"))
            if topic_id is None
            else TopicRead(topic_id, filters)
        )
        return await self.execute(
            QuerySpec(
                read,
                SourceSelection(
                    chat_ids=[chat_id] if chat_id is not None else chat_ids,
                    folder_ids=folder_ids,
                    source_ref=chat_ref,
                ),
                budget,
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
        ordering: Literal["page", "global"] = "page",
    ) -> Page:
        check_limit(limit)
        if ordering not in ("page", "global") or order not in ("newest_first", "oldest_first"):
            raise ToolError("INVALID_ARGUMENT", "Use ordering page/global and a valid order.")
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
            ordering,
        )
        return await self.execute(
            QuerySpec(read, SourceSelection(chat_ids, folder_ids)), limit, cursor
        )

    async def thread(self, chat_id: str, thread_id: int, limit: int, cursor: str | None) -> Page:
        return await self.execute(
            QuerySpec(ThreadRead(thread_id), SourceSelection(chat_ids=[chat_id])), limit, cursor
        )
