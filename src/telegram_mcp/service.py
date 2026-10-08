"""Retrieval policy, immutable pagination snapshots and context assembly."""

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from telegram_mcp.backend import Backend
from telegram_mcp.config import Settings
from telegram_mcp.cursors import CursorStore
from telegram_mcp.folders import allowed_ids, select_chats
from telegram_mcp.models import (
    Chat,
    ChatPage,
    ContentType,
    ContextResult,
    Folder,
    Issue,
    Message,
    MessageRef,
    MessageResult,
    Page,
    Scope,
    ToolError,
)


@dataclass
class Stream:
    chat_id: str
    offset: int = 0
    buffer: list[Message] = field(default_factory=list)
    exhausted: bool = False


@dataclass
class QueryState:
    streams: list[Stream]
    scope: Scope
    date_to: datetime
    issues: list[Issue] = field(default_factory=list)


@dataclass
class ChatState:
    chats: list[Chat]
    index: int
    scope: Scope


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
            "allowed_chat_ids": self.settings.allowed_chat_ids,
            "allowed_folder_ids": self.settings.allowed_folder_ids,
            "cursor_ttl_seconds": self.settings.cursor_ttl_seconds,
        }

    async def catalog(self) -> tuple[list[Chat], list[Folder], set[str]]:
        chats, folders = await self.backend.catalog()
        return chats, folders, allowed_ids(self.settings, chats, folders)

    def require_access(self, chat_id: str, allowed: set[str]) -> None:
        if not self.settings.allow_all_chats and chat_id not in allowed:
            raise ToolError(
                "ACCESS_DENIED", "Chat is outside the server's configured access policy."
            )

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
        chat = await self.backend.resolve_chat(chat_ref)
        self.require_access(chat.id, allowed)
        return {"chat": chat.model_dump(mode="json"), **await self.backend.chat_details(chat)}

    async def resolve(self, ref: str | MessageRef, allowed: set[str]) -> MessageRef:
        resolved = await self.backend.resolve_message(ref)
        self.require_access(resolved.chat_id, allowed)
        return resolved

    async def get_messages(
        self, refs: list[str | MessageRef], text_offset: int = 0, text_limit: int = 12000
    ) -> MessageResult:
        check_limit(len(refs))
        if text_offset < 0 or not 1 <= text_limit <= 100000:
            raise ToolError("INVALID_ARGUMENT", "Invalid text offset or limit (maximum 100000).")
        _, _, allowed = await self.catalog()
        messages: list[Message] = []
        issues: list[Issue] = []
        for ref in refs:
            resolved: MessageRef | None = None
            try:
                resolved = await self.resolve(ref, allowed)
                message = await self.backend.message(resolved)
                if message is None:
                    raise ToolError(
                        "MESSAGE_UNAVAILABLE", "Message was not found or is unavailable."
                    )
                messages.append(clip(message, min(text_limit, 100000 // len(refs)), text_offset))
            except ToolError as exc:
                issues.append(exc.issue(ref=resolved))
        return MessageResult(messages=messages, issues=issues)

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
                self.require_access(next_ref.chat_id, allowed)
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
            ]
        )
        chats, folders, allowed = await self.catalog()
        if cursor:
            state = self.cursors.get(cursor, kind, fp)
            if not isinstance(state, QueryState):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
        else:
            # Explicit IDs may refer to an accessible public channel outside the dialog list.
            known = {c.id for c in chats}
            for cid in chat_ids or []:
                self.require_access(cid, allowed)
                if cid not in known:
                    chats.append(await self.backend.resolve_chat(cid))
            if self.settings.allow_all_chats:
                allowed = {c.id for c in chats}
            selected = select_chats(chats, folders, allowed, folder_ids, chat_ids)
            now = datetime.now(UTC)
            scope = Scope(
                folder_ids=folder_ids or [], chat_ids=[c.id for c in selected], resolved_at=now
            )
            state = QueryState(
                [Stream(c.id) for c in selected], scope, min(date_to, now) if date_to else now
            )
        for stream in state.streams:
            self.require_access(stream.chat_id, allowed)
        remaining = self.settings.rpc_pages_per_call
        deadline = time.monotonic() + 35
        output: list[Message] = []
        oldest = order == "oldest_first"
        while len(output) < limit:
            # Prime every stream before choosing the next globally ordered message.
            for stream in state.streams:
                while not stream.buffer and not stream.exhausted:
                    if remaining <= 0 or time.monotonic() >= deadline:
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
                        )
                        if not batch.exhausted and batch.next_offset == stream.offset:
                            raise ToolError(
                                "PAGINATION_STALLED", "Telegram returned a non-advancing page."
                            )
                        stream.offset = batch.next_offset
                        stream.buffer = batch.messages
                        stream.exhausted = batch.exhausted
                    except ToolError as exc:
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
        token = self.cursors.put(kind, fp, state) if pending else None
        scope = state.scope.model_copy(
            update={"pending_chat_ids": pending, "complete": not pending and not state.issues}
        )
        return Page(
            messages=output, next_cursor=token, scope=scope, issues=state.issues, order=order
        )
