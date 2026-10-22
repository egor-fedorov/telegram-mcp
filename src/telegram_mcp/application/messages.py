"""Selected message, context, album and discussion reads with request-local grants."""

from telegram_mcp.application.access import AccessContext, AccessFactory
from telegram_mcp.application.common import check_limit
from telegram_mcp.application.ports import ChatPort, ExecutionLimits, MessagePort
from telegram_mcp.application.queries import QueryService
from telegram_mcp.application.requests import HistoryRead, ReadFilters
from telegram_mcp.domain.models import (
    AlbumResult,
    ContextResult,
    Issue,
    Message,
    MessageRef,
    MessageResult,
    Page,
    ToolError,
    is_retryable,
)
from telegram_mcp.domain.text import clip


class MessageReader:
    def __init__(
        self,
        messages: MessagePort,
        chats: ChatPort,
        access: AccessFactory,
        queries: QueryService,
        limits: ExecutionLimits,
    ):
        self.messages = messages
        self.chats = chats
        self.access = access
        self.queries = queries
        self.limits = limits

    async def resolve(self, ref: str | MessageRef, access: AccessContext) -> MessageRef:
        resolved = await self.messages.resolve_message(ref)
        await access.allow_explicit(resolved.chat_id)
        return resolved

    async def get_messages(
        self, refs: list[str | MessageRef], text_offset: int = 0, text_limit: int = 12000
    ) -> MessageResult:
        check_limit(len(refs))
        if text_offset < 0 or not 1 <= text_limit <= 100000:
            raise ToolError("INVALID_ARGUMENT", "Invalid text offset or limit (maximum 100000).")
        access = await self.access.load(explicit=True)
        messages: dict[int, Message] = {}
        issues: dict[int, Issue] = {}
        groups: dict[str, list[tuple[int, MessageRef]]] = {}
        for index, ref in enumerate(refs):
            try:
                resolved = await self.messages.resolve_message(ref)
                groups.setdefault(resolved.chat_id, []).append((index, resolved))
            except ToolError as exc:
                issues[index] = exc.issue(input_index=index)
        for chat_id, items in groups.items():
            try:
                await access.allow_explicit(chat_id)
                found = await self.messages.messages(
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
                        message, min(text_limit, self.limits.text_budget // len(refs)), text_offset
                    )
        return MessageResult(
            messages=[messages[i] for i in sorted(messages)],
            issues=[issues[i] for i in sorted(issues)],
        )

    async def get_album(self, ref: str | MessageRef, scan_limit: int = 30) -> AlbumResult:
        if type(scan_limit) is not int or not 1 <= scan_limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "scan_limit must be an integer from 1 to 100.")
        access = await self.access.load(explicit=True)
        anchor_ref = await self.resolve(ref, access)
        anchor = await self.messages.message(anchor_ref)
        if (
            anchor is None
            or anchor.message_ref != anchor_ref
            or anchor.chat.id != anchor_ref.chat_id
        ):
            raise ToolError("MESSAGE_UNAVAILABLE", "Anchor message is unavailable.")
        if anchor.album_id is None:
            raise ToolError("NOT_AN_ALBUM", "Anchor message does not belong to an album.")
        result = AlbumResult(anchor_ref=anchor_ref, album_id=anchor.album_id, scan_limit=scan_limit)
        members = {anchor_ref.message_id: anchor}
        for direction in ("before", "after"):
            try:
                batch = await self.messages.history_window(
                    anchor_ref.chat_id,
                    anchor_id=anchor_ref.message_id,
                    limit=scan_limit,
                    newer=direction == "after",
                )
                inspected = batch.messages[:scan_limit]
                setattr(
                    result, f"scanned_{direction}", min(scan_limit, max(0, batch.scanned_count))
                )
                setattr(result, f"{direction}_exhausted", batch.exhausted)
                result.issues.extend(batch.issues)
                for message in inspected:
                    message_ref = message.message_ref
                    if (
                        message_ref.chat_id != anchor_ref.chat_id
                        or message.chat.id != anchor_ref.chat_id
                        or message.album_id != anchor.album_id
                        or (
                            message_ref.message_id >= anchor_ref.message_id
                            if direction == "before"
                            else message_ref.message_id <= anchor_ref.message_id
                        )
                    ):
                        continue
                    members.setdefault(message_ref.message_id, message)
                if any(is_retryable(issue.code) for issue in batch.issues):
                    break
            except ToolError as exc:
                result.issues.append(exc.issue(anchor_ref.chat_id, anchor_ref))
                if exc.retryable:
                    break
        cap = min(self.limits.text_limit, self.limits.text_budget // len(members))
        result.messages = [clip(members[mid], cap) for mid in sorted(members)]
        return result

    async def get_context(
        self, ref: str | MessageRef, before: int = 10, after: int = 10, reply_depth: int = 3
    ) -> ContextResult:
        if not (0 <= before <= 30 and 0 <= after <= 30 and 0 <= reply_depth <= 10):
            raise ToolError("INVALID_ARGUMENT", "before/after: 0..30; reply_depth: 0..10.")
        access = await self.access.load(explicit=True)
        anchor_ref = await self.resolve(ref, access)
        anchor = await self.messages.message(anchor_ref)
        if anchor is None:
            raise ToolError("MESSAGE_UNAVAILABLE", "Anchor message is unavailable.")
        cap = min(
            self.limits.text_limit, self.limits.text_budget // (1 + before + after + reply_depth)
        )
        result = ContextResult(anchor=clip(anchor, cap))
        for direction, count in (("before", before), ("after", after)):
            if not count:
                continue
            try:
                batch = await self.messages.read(
                    anchor_ref.chat_id,
                    HistoryRead(ReadFilters(oldest_first=direction == "after")),
                    offset=anchor_ref.message_id,
                    limit=count,
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
                access.require_access(next_ref.chat_id, same_source=anchor_ref.chat_id)
                parent = await self.messages.message(next_ref)
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
        access = await self.access.load(explicit=True)
        source = await self.resolve(ref, access)
        chat = await self.chats.resolve_chat(source.chat_id)
        discussion_id = await self.messages.discussion_chat(chat)
        access.require_access(discussion_id)
        root = await self.messages.thread_root(source, discussion_id)
        page = await self.queries.thread(root.chat_id, root.message_id, limit, cursor)
        page.thread_root = root
        return page
