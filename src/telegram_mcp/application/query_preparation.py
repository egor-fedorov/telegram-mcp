"""Validate selectors and recheck access before any saved message buffer is read."""

from dataclasses import dataclass, field

from telegram_mcp.application.access import AccessFactory
from telegram_mcp.application.common import fingerprint
from telegram_mcp.application.global_search import GlobalQueryState
from telegram_mcp.application.message_pager import QueryState, Stream
from telegram_mcp.application.ports import ChatPort, Clock, CursorPort, ForumPort
from telegram_mcp.application.requests import (
    DEFAULT_MAX_MESSAGES,
    BudgetRequest,
    HistoryRead,
    MessageRead,
    PinnedRead,
    SearchRead,
    ThreadRead,
    TopicRead,
)
from telegram_mcp.domain.folders import select_chats
from telegram_mcp.domain.models import Scope, ToolError, TraversalBudget


@dataclass(frozen=True)
class SourceSelection:
    chat_ids: list[str] | None = None
    folder_ids: list[int] | None = None
    source_ref: str | None = None


@dataclass(frozen=True)
class QuerySpec:
    read: MessageRead
    selection: SourceSelection = field(default_factory=SourceSelection)
    budget: BudgetRequest | None = None

    @property
    def kind(self) -> str:
        match self.read:
            case SearchRead():
                return "search"
            case ThreadRead():
                return "thread"
            case PinnedRead() | TopicRead(pinned_only=True):
                return "pinned"
            case _:
                return "history"

    @property
    def allow_public(self) -> bool:
        return not isinstance(self.read, (ThreadRead, TopicRead))

    def fingerprint(self, chat_ids: list[str] | None) -> str:
        read = self.read
        return fingerprint(
            [
                read.query if isinstance(read, SearchRead) else None,
                read.ordering if isinstance(read, (SearchRead, HistoryRead)) else None,
                chat_ids,
                self.selection.folder_ids,
                read.filters.date_from,
                read.filters.date_to,
                read.sender_id if isinstance(read, SearchRead) else None,
                read.filters.content_type,
                read.filters.has_link,
                "oldest_first" if read.filters.oldest_first else "newest_first",
                read.thread_id if isinstance(read, ThreadRead) else None,
                self.selection.source_ref,
                self.allow_public,
                isinstance(read, PinnedRead) or isinstance(read, TopicRead) and read.pinned_only,
                read.topic_id if isinstance(read, TopicRead) else None,
                # max_messages may grow on a cursor; every other budget input is fixed.
                self.budget.max_sources if self.budget is not None else None,
            ]
        )


class QueryPreparation:
    def __init__(
        self,
        chats: ChatPort,
        forums: ForumPort,
        access: AccessFactory,
        cursors: CursorPort,
        clock: Clock,
    ):
        self.chats = chats
        self.forums = forums
        self.access = access
        self.cursors = cursors
        self.clock = clock

    async def prepare(
        self, spec: QuerySpec, cursor: str | None
    ) -> tuple[QueryState | GlobalQueryState, str]:
        filters = spec.read.filters
        for date in (filters.date_from, filters.date_to):
            if date is not None and date.utcoffset() is None:
                raise ToolError("INVALID_ARGUMENT", "Dates must include a timezone.")
        if filters.date_from and filters.date_to and filters.date_from >= filters.date_to:
            raise ToolError("INVALID_ARGUMENT", "date_from must be earlier than date_to.")
        selection = spec.selection
        if (
            not cursor
            and spec.budget is not None
            and selection.chat_ids is not None
            and len(set(selection.chat_ids)) > spec.budget.max_sources
        ):
            raise source_limit(len(set(selection.chat_ids)), spec.budget.max_sources)
        access = await self.access.load(
            explicit=(
                selection.folder_ids is None
                and (selection.chat_ids is not None or selection.source_ref is not None)
            )
        )
        chats, folders, allowed = access.chats, access.folders, access.allowed
        chat_ids = selection.chat_ids
        source_chat = None
        if selection.source_ref is not None:
            source_chat = await self.chats.resolve_chat(selection.source_ref, refresh=True)
            chat_ids = [source_chat.id]
        elif isinstance(spec.read, TopicRead) and chat_ids:
            source_chat = await self.chats.resolve_chat(chat_ids[0], refresh=True)
            chat_ids = [source_chat.id]
        fp = spec.fingerprint(chat_ids)
        if cursor:
            state = self.cursors.get(cursor, spec.kind, fp)
            if isinstance(state, GlobalQueryState):
                if not access.policy.allow_all_chats:
                    raise ToolError(
                        "ACCESS_DENIED",
                        "Global search requires allow_all_chats; restart the search.",
                    )
                return state, fp
            if not isinstance(state, QueryState):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
            apply_budget_increase(state, spec.budget)
            explicit_ids = state.explicit_chat_ids
        else:
            explicit_ids = list(dict.fromkeys(chat_ids or [])) if spec.allow_public else []
        for chat_id in explicit_ids:
            await access.allow_explicit(chat_id)
        effective_allowed = allowed | access.public.keys()
        if isinstance(spec.read, TopicRead) and source_chat is not None:
            access.require_forum(source_chat)
            # A failed fresh topic check must not consume the original cursor.
            topic = await self.forums.get_topic(source_chat.id, spec.read.topic_id)
            if (
                topic.id != spec.read.topic_id
                or topic.chat_id != source_chat.id
                or topic.is_deleted
            ):
                raise ToolError("TOPIC_UNAVAILABLE", "The requested forum topic is unavailable.")
        if not cursor:
            known = {chat.id for chat in chats}
            for chat_id in chat_ids or []:
                access.require_selected(chat_id)
                if chat_id not in known:
                    chats.append(
                        access.public[chat_id]
                        if chat_id in access.public
                        else await self.chats.resolve_chat(chat_id)
                    )
                    known.add(chat_id)
            if access.policy.allow_all_chats:
                effective_allowed = {chat.id for chat in chats}
            selected = select_chats(
                chats, folders, effective_allowed, selection.folder_ids, chat_ids
            )
            now = self.clock.utc_now()
            scope = Scope(
                folder_ids=selection.folder_ids or [],
                chat_ids=[chat.id for chat in selected],
                resolved_at=now,
            )
            date_to = min(filters.date_to, now) if filters.date_to else now
            if (
                isinstance(spec.read, SearchRead)
                and access.policy.allow_all_chats
                and selection.chat_ids is None
                and selection.folder_ids is None
                and selection.source_ref is None
                and spec.read.sender_id is None
                and not filters.oldest_first
            ):
                return GlobalQueryState(scope, date_to, exhausted=not selected), fp
            budget = None
            if spec.budget is not None:
                if len(selected) > spec.budget.max_sources:
                    raise source_limit(len(selected), spec.budget.max_sources)
                budget = TraversalBudget(
                    max_sources=spec.budget.max_sources,
                    max_messages=spec.budget.max_messages or DEFAULT_MAX_MESSAGES,
                )
            state = QueryState(
                [Stream(chat.id) for chat in selected],
                scope,
                date_to,
                explicit_chat_ids=explicit_ids,
                budget=budget,
            )
        for stream in state.streams:
            access.require_selected(stream.chat_id)
        return state, fp


def apply_budget_increase(state: QueryState, request: BudgetRequest | None) -> None:
    """Allow only an explicit larger message budget on an existing cursor."""
    if (state.budget is None) != (request is None):
        raise ToolError(
            "CURSOR_MISMATCH", "Repeat the original budget parameters with this cursor."
        )
    if state.budget is None or request is None or request.max_messages is None:
        return
    if request.max_messages < state.budget.max_messages:
        raise ToolError(
            "INVALID_ARGUMENT",
            "max_messages cannot decrease on a cursor; omit it to keep the current budget.",
        )
    state.budget.max_messages = request.max_messages


def source_limit(count: int, maximum: int) -> ToolError:
    return ToolError(
        "SOURCE_LIMIT_EXCEEDED",
        f"The permitted selection has {count} sources; max_sources is {maximum}. "
        "Narrow the selection or explicitly raise max_sources (up to 1000).",
    )
