"""Validate selectors and recheck access before any saved message buffer is read."""

from dataclasses import dataclass, field

from telegram_mcp.application.access import AccessContext, AccessFactory
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
    ReadFilters,
    SearchRead,
    ThreadRead,
    TopicRead,
)
from telegram_mcp.domain.folders import select_chats
from telegram_mcp.domain.models import Chat, Scope, ToolError, TraversalBudget
from telegram_mcp.domain.policy import SERVICE_NOTIFICATIONS_ID


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
        check_dates(spec.read.filters)
        if not cursor:
            check_explicit_sources(spec)
        selection = spec.selection
        access = await self.access.load(
            explicit=(
                selection.folder_ids is None
                and (selection.chat_ids is not None or selection.source_ref is not None)
            )
        )
        chat_ids, source_chat = await self._resolve_source(spec)
        fp = spec.fingerprint(chat_ids)
        if cursor:
            state = self._resume(spec, cursor, fp, access)
            if isinstance(state, GlobalQueryState):
                return state, fp
            explicit_ids = state.explicit_chat_ids
        else:
            explicit_ids = list(dict.fromkeys(chat_ids or [])) if spec.allow_public else []
        for chat_id in explicit_ids:
            await access.allow_explicit(chat_id)
        await self._check_topic(spec, source_chat, access)
        if not cursor:
            state = await self._start(spec, access, chat_ids, explicit_ids)
            if isinstance(state, GlobalQueryState):
                return state, fp
        for stream in state.streams:
            access.require_selected(stream.chat_id)
        return state, fp

    async def _resolve_source(self, spec: QuerySpec) -> tuple[list[str] | None, Chat | None]:
        """Resolve a single named source freshly; plural selectors stay as given."""
        selection = spec.selection
        if selection.source_ref is not None:
            chat = await self.chats.resolve_chat(selection.source_ref, refresh=True)
            return [chat.id], chat
        if isinstance(spec.read, TopicRead) and selection.chat_ids:
            chat = await self.chats.resolve_chat(selection.chat_ids[0], refresh=True)
            return [chat.id], chat
        return selection.chat_ids, None

    def _resume(
        self, spec: QuerySpec, cursor: str, fp: str, access: AccessContext
    ) -> QueryState | GlobalQueryState:
        state = self.cursors.get(cursor, spec.kind, fp)
        if isinstance(state, GlobalQueryState):
            if not access.policy.allow_all_chats:
                raise ToolError(
                    "ACCESS_DENIED",
                    "Global search requires allow_all_chats; restart the search.",
                )
            return state
        if not isinstance(state, QueryState):
            raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
        apply_budget_increase(state, spec.budget)
        return state

    async def _check_topic(
        self, spec: QuerySpec, source_chat: Chat | None, access: AccessContext
    ) -> None:
        if not isinstance(spec.read, TopicRead) or source_chat is None:
            return
        access.require_forum(source_chat)
        # A failed fresh topic check must not consume the original cursor.
        topic = await self.forums.get_topic(source_chat.id, spec.read.topic_id)
        if topic.id != spec.read.topic_id or topic.chat_id != source_chat.id or topic.is_deleted:
            raise ToolError("TOPIC_UNAVAILABLE", "The requested forum topic is unavailable.")

    async def _start(
        self,
        spec: QuerySpec,
        access: AccessContext,
        chat_ids: list[str] | None,
        explicit_ids: list[str],
    ) -> QueryState | GlobalQueryState:
        """Freeze the selected sources and the upper date of a new traversal."""
        chats = access.chats
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
        allowed = (
            {chat.id for chat in chats} - {SERVICE_NOTIFICATIONS_ID}
            if access.policy.allow_all_chats
            else access.allowed | access.public.keys()
        )
        selected = select_chats(chats, access.folders, allowed, spec.selection.folder_ids, chat_ids)
        now = self.clock.utc_now()
        scope = Scope(
            folder_ids=spec.selection.folder_ids or [],
            chat_ids=[chat.id for chat in selected],
            resolved_at=now,
        )
        filters = spec.read.filters
        date_to = min(filters.date_to, now) if filters.date_to else now
        if is_global_search(spec, access):
            return GlobalQueryState(scope, date_to, exhausted=not selected)
        return QueryState(
            [Stream(chat.id) for chat in selected],
            scope,
            date_to,
            explicit_chat_ids=explicit_ids,
            budget=start_budget(spec.budget, len(selected)),
        )


def check_dates(filters: ReadFilters) -> None:
    for date in (filters.date_from, filters.date_to):
        if date is not None and date.utcoffset() is None:
            raise ToolError("INVALID_ARGUMENT", "Dates must include a timezone.")
    if filters.date_from and filters.date_to and filters.date_from >= filters.date_to:
        raise ToolError("INVALID_ARGUMENT", "date_from must be earlier than date_to.")


def check_explicit_sources(spec: QuerySpec) -> None:
    """Reject too many explicit chat IDs before loading access or resolving any source."""
    chat_ids = spec.selection.chat_ids
    if spec.budget is not None and chat_ids is not None:
        count = len(set(chat_ids))
        if count > spec.budget.max_sources:
            raise source_limit(count, spec.budget.max_sources)


def is_global_search(spec: QuerySpec, access: AccessContext) -> bool:
    """An unrestricted newest-first search may use Telegram's global index."""
    read, selection = spec.read, spec.selection
    return (
        isinstance(read, SearchRead)
        and access.policy.allow_all_chats
        and selection.chat_ids is None
        and selection.folder_ids is None
        and selection.source_ref is None
        and read.sender_id is None
        and not read.filters.oldest_first
    )


def start_budget(request: BudgetRequest | None, sources: int) -> TraversalBudget | None:
    if request is None:
        return None
    if sources > request.max_sources:
        raise source_limit(sources, request.max_sources)
    return TraversalBudget(
        max_sources=request.max_sources,
        max_messages=request.max_messages or DEFAULT_MAX_MESSAGES,
    )


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
