"""Discovery and retrieval policy, pagination snapshots and context assembly."""

import json
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from telegram_mcp.backend import Backend, DiscoveryBatch, TopicOffset
from telegram_mcp.config import Settings
from telegram_mcp.cursors import CursorStore
from telegram_mcp.folders import allowed_ids, select_chats
from telegram_mcp.models import (
    Chat,
    ChatPage,
    ContentType,
    ContextResult,
    DiscoveryResult,
    Folder,
    ForumTopic,
    Issue,
    Message,
    MessageRef,
    MessageResult,
    Page,
    Scope,
    ToolError,
    TopicPage,
)


@dataclass
class Stream:
    chat_id: str
    offset: int = 0
    buffer: list[Message] = field(default_factory=list)
    exhausted: bool = False
    transient_issue: Issue | None = None
    retry_at: float = 0


@dataclass
class QueryState:
    streams: list[Stream]
    scope: Scope
    date_to: datetime
    explicit_chat_ids: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


@dataclass
class ChatState:
    chats: list[Chat]
    index: int
    scope: Scope


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
    transient_issue: Issue | None = None
    retry_at: float = 0


def fingerprint(values: object) -> str:
    return json.dumps(values, sort_keys=True, default=str)


def clip(message: Message, maximum: int = 12000, offset: int = 0) -> Message:
    total = len(message.text)
    end = min(offset + maximum, total)
    return message.model_copy(
        update={
            "text": message.text[offset:end],
            "text_offset": offset,
            "text_total_length": total,
            "next_text_offset": end if end < total else None,
        }
    )


def check_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise ToolError("INVALID_ARGUMENT", "limit must be between 1 and 100.")


def discovery_result(batch: DiscoveryBatch, limit: int) -> DiscoveryResult:
    # Keep the first occurrence so native result order and metadata remain stable.
    unique = {}
    for channel in batch.channels:
        unique.setdefault(channel.id, channel)
    channels = list(unique.values())[:limit]
    return DiscoveryResult(
        channels=channels,
        returned_count=len(channels),
        telegram_total_count=batch.telegram_total_count,
        locally_truncated=len(unique) > limit,
    )


class Service:
    def __init__(self, backend: Backend, settings: Settings):
        self.backend = backend
        self.settings = settings
        self.cursors = CursorStore(settings.cursor_ttl_seconds, settings.max_cursors)

    async def get_status(self) -> dict[str, object]:
        status = await self.backend.status()
        return {
            **status,
            "mode": "read_only",
            "allow_all_chats": self.settings.allow_all_chats,
            "allow_public_channel_reads": self.settings.allow_public_channel_reads,
            "allow_public_channel_discovery": self.settings.allow_public_channel_discovery,
            "allowed_chat_ids": self.settings.allowed_chat_ids,
            "allowed_folder_ids": self.settings.allowed_folder_ids,
            "cursor_ttl_seconds": self.settings.cursor_ttl_seconds,
        }

    def require_discovery(self) -> None:
        if not self.settings.allow_public_channel_discovery:
            raise ToolError(
                "FEATURE_DISABLED",
                "Public-channel discovery requires allow_public_channel_discovery.",
            )

    async def search_public_channels(self, query: str, limit: int = 20) -> DiscoveryResult:
        self.require_discovery()
        check_limit(limit)
        query = query.strip()
        if not 1 <= len(query) <= 1000:
            raise ToolError(
                "INVALID_ARGUMENT", "query must contain 1 to 1000 characters after trimming."
            )
        return discovery_result(await self.backend.search_public_channels(query, limit), limit)

    async def get_similar_channels(self, chat_ref: str, limit: int = 20) -> DiscoveryResult:
        self.require_discovery()
        check_limit(limit)
        chat_ref = chat_ref.strip()
        if not chat_ref:
            raise ToolError("INVALID_ARGUMENT", "chat_ref must not be empty.")
        return discovery_result(await self.backend.get_similar_channels(chat_ref), limit)

    async def catalog(self) -> tuple[list[Chat], list[Folder], set[str]]:
        chats, folders = await self.backend.catalog()
        # Explicit public sources are request-local, never added to the dialog catalog.
        return list(chats), folders, allowed_ids(self.settings, chats, folders)

    def require_access(self, chat_id: str, allowed: set[str]) -> None:
        if not self.settings.allow_all_chats and chat_id not in allowed:
            raise ToolError(
                "ACCESS_DENIED", "Chat is outside the server's configured access policy."
            )

    async def allow_explicit(
        self, chat_id: str, allowed: set[str], public: dict[str, Chat]
    ) -> None:
        """Authorize a user-selected source; never use for linked or discovered peers."""
        if self.settings.allow_all_chats or chat_id in allowed or chat_id in public:
            return
        if not self.settings.allow_public_channel_reads:
            self.require_access(chat_id, allowed)
        # The backend must bypass metadata caches and validate the actual channel type.
        public[chat_id] = await self.backend.public_channel(chat_id)

    async def list_folders(self, query: str | None = None) -> list[Folder]:
        _, folders = await self.backend.catalog()
        return [f for f in folders if not query or query.casefold() in f.title.casefold()]

    async def list_chats(
        self,
        query: str | None = None,
        chat_type: str | None = None,
        folder_ids: list[int] | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> ChatPage:
        check_limit(limit)
        chats, folders, allowed = await self.catalog()
        fp = fingerprint([query, chat_type, folder_ids])
        if cursor:
            state = self.cursors.get(cursor, "chats", fp)
            if not isinstance(state, ChatState):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
        else:
            selected = select_chats(
                chats, folders, allowed, folder_ids, query=query, chat_type=chat_type
            )
            state = ChatState(
                selected,
                0,
                Scope(
                    folder_ids=folder_ids or [],
                    chat_ids=[c.id for c in selected],
                    resolved_at=datetime.now(UTC),
                ),
            )
        for chat in state.chats:
            self.require_access(chat.id, allowed)
        page = state.chats[state.index : state.index + limit]
        state.index += len(page)
        more = state.index < len(state.chats)
        token = self.cursors.put("chats", fp, state) if more else None
        return ChatPage(
            chats=page,
            next_cursor=token,
            scope=state.scope.model_copy(update={"complete": not more}),
        )

    async def get_chat(self, chat_ref: str) -> dict[str, object]:
        _, _, allowed = await self.catalog()
        chat = await self.backend.resolve_chat(chat_ref, refresh=True)
        public: dict[str, Chat] = {}
        await self.allow_explicit(chat.id, allowed, public)
        chat = public.get(chat.id, chat)
        return {"chat": chat.model_dump(mode="json"), **await self.backend.chat_details(chat)}

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
        _, _, allowed = await self.catalog()
        chat = await self.backend.resolve_chat(chat_ref, refresh=True)
        fp = fingerprint([chat_ref, chat.id, query])
        if cursor:
            state = self.cursors.get(cursor, "topics", fp)
            if not isinstance(state, TopicState):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
        else:
            state = TopicState(Scope(chat_ids=[chat.id], resolved_at=datetime.now(UTC)))
        # Public-channel permissions cannot authorize forum groups or their buffered metadata.
        self.require_access(chat.id, allowed)
        if chat.type != "group" or not chat.is_supergroup or not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported forum supergroup.")

        if not state.buffer and not state.exhausted and time.monotonic() >= state.retry_at:
            try:
                batch = await self.backend.fetch_topics(
                    chat.id, offset=state.offset, limit=limit, query=query
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
                state.transient_issue = None
                state.retry_at = 0
            except ToolError as exc:
                if exc.retryable:
                    state.transient_issue = exc.issue(chat.id)
                    state.retry_at = time.monotonic() + (exc.retry_after or 0)
                else:
                    state.issues.append(exc.issue(chat.id))
                    state.transient_issue = None
                    state.exhausted = True
        topics = state.buffer[:limit]
        state.buffer = state.buffer[limit:]
        issues = list(state.issues)
        if state.transient_issue is not None:
            issue = state.transient_issue
            if issue.retry_after_seconds is not None:
                issue = issue.model_copy(
                    update={
                        "retry_after_seconds": max(0, math.ceil(state.retry_at - time.monotonic()))
                    }
                )
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
                    "pending_chat_ids": [chat.id] if pending else [],
                    "complete": not pending and not issues,
                }
            ),
            issues=issues,
            telegram_total_count=state.telegram_total_count,
            order=order,
        )

    async def get_pinned_messages(
        self, chat_ref: str, limit: int = 30, cursor: str | None = None
    ) -> Page:
        chat_ref = chat_ref.strip()
        if not chat_ref:
            raise ToolError("INVALID_ARGUMENT", "chat_ref must not be empty.")
        return await self.query(
            source_ref=chat_ref, limit=limit, cursor=cursor, kind="pinned", pinned_only=True
        )

    async def resolve(self, ref: str | MessageRef, allowed: set[str]) -> MessageRef:
        resolved = await self.backend.resolve_message(ref)
        await self.allow_explicit(resolved.chat_id, allowed, {})
        return resolved

    async def get_history(
        self,
        chat_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        order: Literal["newest_first", "oldest_first"] = "newest_first",
        limit: int = 30,
        cursor: str | None = None,
        chat_ref: str | None = None,
    ) -> Page:
        if (chat_id is None) == (chat_ref is None):
            raise ToolError("INVALID_ARGUMENT", "Provide exactly one of chat_id or chat_ref.")
        return await self.query(
            chat_ids=[chat_id] if chat_id is not None else None,
            date_from=date_from,
            date_to=date_to,
            order=order,
            limit=limit,
            cursor=cursor,
            kind="history",
            source_ref=chat_ref,
        )

    async def get_messages(
        self, refs: list[str | MessageRef], text_offset: int = 0, text_limit: int = 12000
    ) -> MessageResult:
        check_limit(len(refs))
        if text_offset < 0 or not 1 <= text_limit <= 100000:
            raise ToolError("INVALID_ARGUMENT", "Invalid text offset or limit (maximum 100000).")
        _, _, allowed = await self.catalog()
        messages: dict[int, Message] = {}
        issues: dict[int, Issue] = {}
        groups: dict[str, list[tuple[int, MessageRef]]] = {}
        for index, ref in enumerate(refs):
            try:
                resolved = await self.backend.resolve_message(ref)
                groups.setdefault(resolved.chat_id, []).append((index, resolved))
            except ToolError as exc:
                issues[index] = exc.issue(input_index=index)
        public: dict[str, Chat] = {}
        for chat_id, items in groups.items():
            try:
                await self.allow_explicit(chat_id, allowed, public)
                found = await self.backend.messages(
                    chat_id, list(dict.fromkeys(ref.message_id for _, ref in items))
                )
            except ToolError as exc:
                for index, ref in items:
                    issues[index] = exc.issue(chat_id, ref, input_index=index)
                continue
            for index, ref in items:
                message = found.get(ref.message_id)
                if message is None:
                    issues[index] = ToolError(
                        "MESSAGE_UNAVAILABLE", "Message was not found or is unavailable."
                    ).issue(chat_id, ref, input_index=index)
                else:
                    messages[index] = clip(
                        message, min(text_limit, 100000 // len(refs)), text_offset
                    )
        return MessageResult(
            messages=[messages[i] for i in sorted(messages)],
            issues=[issues[i] for i in sorted(issues)],
        )

    async def get_context(
        self, ref: str | MessageRef, before: int = 10, after: int = 10, reply_depth: int = 3
    ) -> ContextResult:
        if not (0 <= before <= 30 and 0 <= after <= 30 and 0 <= reply_depth <= 10):
            raise ToolError("INVALID_ARGUMENT", "before/after: 0..30; reply_depth: 0..10.")
        _, _, allowed = await self.catalog()
        anchor_ref = await self.resolve(ref, allowed)
        anchor = await self.backend.message(anchor_ref)
        if anchor is None:
            raise ToolError("MESSAGE_UNAVAILABLE", "Anchor message is unavailable.")
        cap = min(12000, 100000 // (1 + before + after + reply_depth))
        result = ContextResult(anchor=clip(anchor, cap))
        for direction, count in (("before", before), ("after", after)):
            if not count:
                continue
            try:
                batch = await self.backend.fetch(
                    anchor_ref.chat_id,
                    offset=anchor_ref.message_id,
                    limit=count,
                    oldest_first=direction == "after",
                )
                items = sorted(batch.messages, key=lambda m: m.message_ref.message_id)
                setattr(result, direction, [clip(m, cap) for m in items])
            except ToolError as exc:
                result.issues.append(exc.issue(anchor_ref.chat_id))
        next_ref = anchor.reply_to
        visited = {anchor_ref.key}
        for _ in range(reply_depth):
            if next_ref is None or next_ref.key in visited:
                break
            visited.add(next_ref.key)
            try:
                # Same-source parents share the anchor grant; cross-chat parents do not.
                self.require_access(next_ref.chat_id, allowed | {anchor_ref.chat_id})
                parent = await self.backend.message(next_ref)
                if parent is None:
                    raise ToolError("MESSAGE_UNAVAILABLE", "A parent message is unavailable.")
                result.reply_chain.append(clip(parent, cap))
                next_ref = parent.reply_to
            except ToolError as exc:
                result.issues.append(exc.issue(ref=next_ref))
                break
        return result

    async def get_thread(
        self, ref: str | MessageRef, limit: int = 30, cursor: str | None = None
    ) -> Page:
        _, _, allowed = await self.catalog()
        source = await self.resolve(ref, allowed)
        chat = await self.backend.resolve_chat(source.chat_id)
        discussion_id = await self.backend.discussion_chat(chat)
        self.require_access(discussion_id, allowed)
        root = await self.backend.thread_root(source, discussion_id)
        page = await self.query(
            chat_ids=[root.chat_id],
            limit=limit,
            cursor=cursor,
            thread_id=root.message_id,
            kind="thread",
            allow_public=False,
        )
        page.thread_root = root
        return page

    async def query(
        self,
        *,
        query: str | None = None,
        chat_ids: list[str] | None = None,
        folder_ids: list[int] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sender_id: str | None = None,
        content_type: ContentType | None = None,
        limit: int = 30,
        cursor: str | None = None,
        order: Literal["newest_first", "oldest_first"] = "newest_first",
        thread_id: int | None = None,
        kind: str = "search",
        source_ref: str | None = None,
        allow_public: bool = True,
        pinned_only: bool = False,
    ) -> Page:
        check_limit(limit)
        if kind == "search" and (query is None or not query.strip()):
            raise ToolError(
                "INVALID_ARGUMENT", "Search requires non-empty query; use get_history otherwise."
            )
        for date in (date_from, date_to):
            if date is not None and date.utcoffset() is None:
                raise ToolError("INVALID_ARGUMENT", "Dates must include a timezone.")
        if date_from and date_to and date_from >= date_to:
            raise ToolError("INVALID_ARGUMENT", "date_from must be earlier than date_to.")
        chats, folders, allowed = await self.catalog()
        if source_ref is not None:
            # Loading dialogs first primes Telethon's entity/access-hash cache for IDs.
            chat_ids = [(await self.backend.resolve_chat(source_ref, refresh=True)).id]
        fp = fingerprint(
            [
                query,
                chat_ids,
                folder_ids,
                date_from,
                date_to,
                sender_id,
                content_type,
                order,
                thread_id,
                source_ref,
                allow_public,
                pinned_only,
            ]
        )
        if cursor:
            state = self.cursors.get(cursor, kind, fp)
            if not isinstance(state, QueryState):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
            explicit_ids = state.explicit_chat_ids
        else:
            explicit_ids = list(dict.fromkeys(chat_ids or [])) if allow_public else []
        public: dict[str, Chat] = {}
        for cid in explicit_ids:
            await self.allow_explicit(cid, allowed, public)
        effective_allowed = allowed | public.keys()
        if not cursor:
            known = {c.id for c in chats}
            for cid in chat_ids or []:
                self.require_access(cid, effective_allowed)
                if cid not in known:
                    chats.append(
                        public[cid] if cid in public else await self.backend.resolve_chat(cid)
                    )
                    known.add(cid)
            if self.settings.allow_all_chats:
                effective_allowed = {c.id for c in chats}
            selected = select_chats(chats, folders, effective_allowed, folder_ids, chat_ids)
            now = datetime.now(UTC)
            scope = Scope(
                folder_ids=folder_ids or [], chat_ids=[c.id for c in selected], resolved_at=now
            )
            state = QueryState(
                [Stream(c.id) for c in selected],
                scope,
                min(date_to, now) if date_to else now,
                explicit_chat_ids=explicit_ids,
            )
        for stream in state.streams:
            self.require_access(stream.chat_id, effective_allowed)
        remaining = self.settings.rpc_pages_per_call
        deadline = time.monotonic() + 35
        output: list[Message] = []
        oldest = order == "oldest_first"
        failed_this_call: set[str] = set()
        while len(output) < limit:
            # Prime every stream before choosing the next globally ordered message.
            for stream in state.streams:
                while not stream.buffer and not stream.exhausted:
                    if (
                        remaining <= 0
                        or time.monotonic() >= deadline
                        or stream.chat_id in failed_this_call
                        or time.monotonic() < stream.retry_at
                    ):
                        break
                    remaining -= 1
                    try:
                        batch = await self.backend.fetch(
                            stream.chat_id,
                            offset=stream.offset,
                            limit=30,
                            query=query,
                            date_from=date_from,
                            date_to=state.date_to,
                            sender_id=sender_id,
                            content_type=content_type,
                            oldest_first=oldest,
                            thread_id=thread_id,
                            pinned_only=pinned_only,
                        )
                        if not batch.exhausted and batch.next_offset == stream.offset:
                            raise ToolError(
                                "PAGINATION_STALLED", "Telegram returned a non-advancing page."
                            )
                        stream.offset = batch.next_offset
                        stream.buffer = batch.messages
                        stream.exhausted = batch.exhausted
                        stream.transient_issue = None
                        stream.retry_at = 0
                    except ToolError as exc:
                        if exc.retryable:
                            stream.transient_issue = exc.issue(stream.chat_id)
                            stream.retry_at = time.monotonic() + (exc.retry_after or 0)
                            failed_this_call.add(stream.chat_id)
                            break
                        stream.transient_issue = None
                        state.issues.append(exc.issue(stream.chat_id))
                        stream.exhausted = True
            if any(not s.buffer and not s.exhausted for s in state.streams):
                break
            candidates = [s for s in state.streams if s.buffer]
            if not candidates:
                break

            def key(s: Stream) -> tuple[datetime, str, int]:
                head = s.buffer[0]
                return head.sent_at, s.chat_id, head.message_ref.message_id

            selected_stream = min(candidates, key=key) if oldest else max(candidates, key=key)
            output.append(clip(selected_stream.buffer.pop(0), min(12000, 100000 // limit)))
        pending = [s.chat_id for s in state.streams if s.buffer or not s.exhausted]
        issues = list(state.issues)
        for stream in state.streams:
            if stream.transient_issue is not None:
                issue = stream.transient_issue
                if issue.retry_after_seconds is not None:
                    issue = issue.model_copy(
                        update={
                            "retry_after_seconds": max(
                                0, math.ceil(stream.retry_at - time.monotonic())
                            )
                        }
                    )
                issues.append(issue)
        token = self.cursors.put(kind, fp, state) if pending else None
        scope = state.scope.model_copy(
            update={"pending_chat_ids": pending, "complete": not pending and not issues}
        )
        return Page(messages=output, next_cursor=token, scope=scope, issues=issues, order=order)
