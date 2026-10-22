"""Message reads with distinct history, forum, and bounded album algorithms."""

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from telethon import functions, types, utils

from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import (
    MAX_TOPIC_ID,
    chat_from_entity,
    message_topic_id,
    normalize_message,
    peer_id,
    utc,
    valid_topic_id,
)
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import (
    Batch,
    GlobalBatch,
    GlobalOffset,
    HistoryWindow,
    MessageOffset,
    SearchAnchor,
)
from telegram_mcp.application.requests import (
    HistoryRead,
    MessageRead,
    PinnedRead,
    ReadFilters,
    SearchRead,
    ThreadRead,
    TopicRead,
)
from telegram_mcp.domain.models import Issue, Message, MessageRef, ToolError


def native_filter(filters: ReadFilters) -> Any:
    """Push down only filters that cover the corresponding local predicate."""
    if filters.has_link is True:
        return types.InputMessagesFilterUrl()
    constructors = {
        "photo": types.InputMessagesFilterPhotos,
        "voice": types.InputMessagesFilterVoice,
        "audio": types.InputMessagesFilterMusic,
    }
    return constructors.get(filters.content_type, types.InputMessagesFilterEmpty)()


def matches_filters(message: Message, filters: ReadFilters) -> bool:
    return (
        (filters.content_type is None or message.content_type == filters.content_type)
        and (filters.has_link is None or message.has_link is filters.has_link)
        and (filters.date_from is None or message.sent_at >= filters.date_from)
        and (filters.date_to is None or message.sent_at < filters.date_to)
    )


def response_entities(response: Any) -> dict[int, Any]:
    try:
        return {utils.get_peer_id(value): value for value in [*response.users, *response.chats]}
    except (AttributeError, TypeError, ValueError):
        raise ToolError(
            "PAGINATION_INCOMPLETE", "Telegram returned unusable search entities."
        ) from None


def search_peer_id(value: Any) -> str:
    if not isinstance(value, (types.PeerUser, types.PeerChat, types.PeerChannel)):
        raise ToolError("PAGINATION_INCOMPLETE", "Telegram returned an unusable search peer.")
    result = peer_id(value)
    native_id, _ = utils.resolve_id(int(result))
    if native_id <= 0:
        raise ToolError("PAGINATION_INCOMPLETE", "Telegram returned an unusable search peer.")
    return result


def validate_search_response(response: Any, limit: int, *, global_search: bool = False) -> None:
    accepted = (types.messages.Messages, types.messages.MessagesSlice)
    if not global_search:
        accepted += (types.messages.ChannelMessages,)
    if not isinstance(response, accepted) or not all(
        isinstance(getattr(response, name, None), list) for name in ("messages", "users", "chats")
    ):
        raise ToolError("PAGINATION_INCOMPLETE", "Telegram returned an unusable search page.")
    if len(response.messages) > limit:
        raise ToolError(
            "PAGINATION_INCOMPLETE", "Telegram exceeded the requested search page size."
        )


class MessageGateway:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats

    async def read(
        self, chat_id: str, request: MessageRead, *, offset: MessageOffset = 0, limit: int = 30
    ) -> Batch:
        if isinstance(request, SearchRead):
            return await self._read_search(chat_id, request, offset=offset, limit=limit)
        if not isinstance(offset, int):
            raise ToolError("INVALID_CURSOR", "Search anchor cannot be used for this read.")
        if isinstance(request, TopicRead):
            if not valid_topic_id(request.topic_id):
                raise ToolError("INVALID_ARGUMENT", "topic_id must be a positive 32-bit integer.")
            return await self._read_topic(chat_id, request, offset=offset, limit=limit)
        return await self._read_history(chat_id, request, offset=offset, limit=limit)

    async def _read_search(
        self, chat_id: str, request: SearchRead, *, offset: MessageOffset, limit: int
    ) -> Batch:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "limit must be an integer from 1 to 100.")
        position = offset.offset_id if isinstance(offset, SearchAnchor) else offset
        if type(position) is not int or not 0 <= position <= MAX_TOPIC_ID:
            raise ToolError("INVALID_CURSOR", "Invalid search offset.")
        filters = request.filters
        oldest = filters.oldest_first
        if not isinstance(offset, SearchAnchor) and oldest and position == MAX_TOPIC_ID:
            return Batch([], offset, True)
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(chat_id)
        entity = await self.runtime.call(client.get_input_entity(int(chat_id)))
        # Telegram does not reliably support media filters combined with from_id.
        # The local predicate still applies to every returned sender-search row.
        selected_filter = (
            native_filter(filters)
            if request.sender_id is None
            else types.InputMessagesFilterEmpty()
        )
        boundary = filters.date_from if oldest else filters.date_to
        needs_anchor = (
            offset == 0
            and boundary is not None
            and (
                oldest
                or not request.query
                and not isinstance(selected_filter, types.InputMessagesFilterEmpty)
            )
        )
        if needs_anchor:
            # Filter-only Search may ignore max_date. Spend this page's budget on
            # establishing an ID boundary, then issue Search in the next page.
            response = await self.runtime.call(
                client(
                    functions.messages.GetHistoryRequest(
                        peer=entity,
                        offset_id=0,
                        offset_date=boundary - timedelta(seconds=1)
                        if oldest
                        else boundary + timedelta(seconds=1),
                        add_offset=0,
                        limit=1,
                        max_id=0,
                        min_id=0,
                        hash=0,
                    )
                )
            )
            validate_search_response(response, 1)
            if getattr(response, "inexact", False):
                raise ToolError(
                    "PAGINATION_INCOMPLETE", "Telegram returned an inexact date boundary."
                )
            if not response.messages:
                return Batch([], SearchAnchor(1) if oldest else offset, not oldest)
            raw = response.messages[0]
            mid = getattr(raw, "id", None)
            if (
                not valid_topic_id(mid)
                or getattr(raw, "peer_id", None) is None
                or search_peer_id(raw.peer_id) != chat_id
            ):
                raise ToolError(
                    "PAGINATION_INCOMPLETE", "Telegram returned an unusable search boundary."
                )
            if mid == MAX_TOPIC_ID:
                return Batch([], offset if oldest else SearchAnchor(0), oldest)
            return Batch([], SearchAnchor(mid + 1), False)
        offset_id = (
            position if isinstance(offset, SearchAnchor) else position + 1 if oldest else position
        )
        sender = None
        if request.sender_id is not None and chat.type not in ("private", "bot", "saved"):
            sender = await self.runtime.call(client.get_input_entity(int(request.sender_id)))
        response = await self.runtime.call(
            client(
                functions.messages.SearchRequest(
                    peer=entity,
                    q=request.query,
                    from_id=sender,
                    filter=selected_filter,
                    min_date=None,
                    max_date=filters.date_to + timedelta(seconds=1)
                    if filters.date_to is not None and not oldest
                    else None,
                    offset_id=offset_id,
                    add_offset=-limit if oldest else 0,
                    limit=limit,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            )
        )
        validate_search_response(response, limit)
        issues = []
        if getattr(response, "inexact", False):
            issues.append(
                ToolError("SEARCH_INEXACT", "Telegram reports inexact search results.").issue(
                    chat_id
                )
            )
        if not response.messages:
            return Batch([], offset, True, issues)
        entities = response_entities(response)
        positioned: dict[int, Any] = {}
        for raw in response.messages:
            mid = getattr(raw, "id", None)
            raw_peer = getattr(raw, "peer_id", None)
            if (
                not valid_topic_id(mid)
                or (raw_peer is None and not isinstance(raw, types.MessageEmpty))
                or (raw_peer is not None and search_peer_id(raw_peer) != chat_id)
            ):
                raise ToolError(
                    "PAGINATION_INCOMPLETE",
                    "Telegram returned a search row outside its source or without a position.",
                )
            if isinstance(offset, int) and offset and (mid <= offset if oldest else mid >= offset):
                continue
            if (
                isinstance(offset, SearchAnchor)
                and position
                and (mid < position if oldest else mid >= position)
            ):
                continue
            positioned.setdefault(mid, raw)
        if not positioned:
            raise ToolError("PAGINATION_STALLED", "Telegram returned a non-advancing search page.")
        ordered = [positioned[mid] for mid in sorted(positioned, reverse=not oldest)]
        exhausted = isinstance(response, types.messages.Messages)
        output = []
        for raw in ordered:
            if isinstance(raw, types.MessageEmpty):
                continue
            if not isinstance(raw, (types.Message, types.MessageService)) or not isinstance(
                getattr(raw, "date", None), datetime
            ):
                raise ToolError(
                    "PAGINATION_INCOMPLETE", "Telegram returned incomplete search metadata."
                )
            message = normalize_message(raw, chat, entities=entities)
            if (oldest and filters.date_to is not None and message.sent_at >= filters.date_to) or (
                not oldest and filters.date_from is not None and message.sent_at < filters.date_from
            ):
                exhausted = True
            if matches_filters(message, filters) and (
                request.sender_id is None or message.sender.id == request.sender_id
            ):
                output.append(message)
        return Batch(output, ordered[-1].id, exhausted, issues)

    async def search_global(
        self, request: SearchRead, *, offset: GlobalOffset, limit: int = 30, chat_ids: list[str]
    ) -> GlobalBatch:
        """Read one native global page; source authorization belongs to the application."""
        if request.filters.oldest_first or request.sender_id is not None:
            raise ToolError(
                "INVALID_ARGUMENT", "Global search requires newest_first without a sender selector."
            )
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "limit must be an integer from 1 to 100.")
        if (
            type(offset.rate) is not int
            or not 0 <= offset.rate <= MAX_TOPIC_ID
            or type(offset.message_id) is not int
            or not 0 <= offset.message_id <= MAX_TOPIC_ID
        ):
            raise ToolError("INVALID_CURSOR", "Invalid global search position.")
        offset_peer: Any = types.InputPeerEmpty()
        if offset.peer_id is not None:
            if not re.fullmatch(r"-?[1-9]\d*", offset.peer_id):
                raise ToolError("INVALID_CURSOR", "Invalid global search peer.")
            native_id, kind = utils.resolve_id(int(offset.peer_id))
            cached = self.chats.cache.get(offset.peer_id)
            if cached is not None and cached.type == "saved":
                offset_peer = types.InputPeerSelf()
            elif kind is types.PeerChat:
                offset_peer = types.InputPeerChat(native_id)
            elif offset.access_hash is not None:
                offset_peer = (
                    types.InputPeerChannel(native_id, offset.access_hash)
                    if kind is types.PeerChannel
                    else types.InputPeerUser(native_id, offset.access_hash)
                )
            else:
                raise ToolError(
                    "PAGINATION_INCOMPLETE",
                    "The global search continuation has no usable peer metadata.",
                )
        filters = request.filters
        client = await self.runtime.ensure()
        response = await self.runtime.call(
            client(
                functions.messages.SearchGlobalRequest(
                    q=request.query,
                    filter=native_filter(filters),
                    min_date=filters.date_from - timedelta(seconds=1)
                    if filters.date_from is not None
                    else None,
                    max_date=filters.date_to + timedelta(seconds=1)
                    if filters.date_to is not None
                    else None,
                    offset_rate=offset.rate,
                    offset_peer=offset_peer,
                    offset_id=offset.message_id,
                    limit=limit,
                )
            )
        )
        validate_search_response(response, limit, global_search=True)
        issues = []
        if getattr(response, "inexact", False):
            issues.append(
                ToolError("SEARCH_INEXACT", "Telegram reports inexact search results.").issue()
            )
        if not response.messages:
            return GlobalBatch([], offset, True, issues)
        entities = response_entities(response)
        allowed = set(chat_ids)
        output = []
        seen = set()
        last_date = None
        next_offset = offset
        for raw in response.messages:
            mid, raw_peer, date = (
                getattr(raw, "id", None),
                getattr(raw, "peer_id", None),
                getattr(raw, "date", None),
            )
            if (
                not isinstance(raw, (types.Message, types.MessageService))
                or not valid_topic_id(mid)
                or raw_peer is None
                or not isinstance(date, datetime)
            ):
                raise ToolError(
                    "PAGINATION_INCOMPLETE",
                    "Telegram returned a global search row without a usable position.",
                )
            cid = search_peer_id(raw_peer)
            if last_date is not None and utc(date) > last_date:
                raise ToolError(
                    "PAGINATION_INCOMPLETE", "Telegram returned an unordered global search page."
                )
            last_date = utc(date)
            author = entities.get(int(cid))
            access_hash = getattr(author, "access_hash", None)
            if getattr(author, "is_self", False):
                access_hash = 0
            next_offset = GlobalOffset(int(utc(date).timestamp()), cid, mid, access_hash)
            if (cid, mid) in seen:
                continue
            seen.add((cid, mid))
            if cid not in allowed:
                continue
            chat = self.chats.cache.get(cid)
            if chat is None and author is not None:
                chat = chat_from_entity(author, now=self.chats.clock.utc_now())
            if chat is None:
                raise ToolError(
                    "PAGINATION_INCOMPLETE",
                    "Telegram returned a selected source without usable metadata.",
                )
            message = normalize_message(raw, chat, entities=entities)
            if matches_filters(message, filters):
                output.append(message)
        rate = getattr(response, "next_rate", None)
        if rate is not None:
            if type(rate) is not int or not 0 <= rate <= MAX_TOPIC_ID:
                raise ToolError(
                    "PAGINATION_INCOMPLETE",
                    "Telegram returned an invalid global search continuation.",
                )
            next_offset = GlobalOffset(
                rate, next_offset.peer_id, next_offset.message_id, next_offset.access_hash
            )
        if (next_offset.rate, next_offset.peer_id, next_offset.message_id) == (
            offset.rate,
            offset.peer_id,
            offset.message_id,
        ):
            raise ToolError(
                "PAGINATION_STALLED", "Telegram returned a non-advancing global search page."
            )
        exhausted = isinstance(response, types.messages.Messages)
        if not exhausted and next_offset.peer_id is not None:
            _, kind = utils.resolve_id(int(next_offset.peer_id))
            if kind is not types.PeerChat and next_offset.access_hash is None:
                issues.append(
                    ToolError(
                        "PAGINATION_INCOMPLETE",
                        "Telegram returned no usable continuation peer metadata.",
                    ).issue()
                )
                exhausted = True
        return GlobalBatch(output, next_offset, exhausted, issues)

    async def resolve_message(self, ref: str | MessageRef) -> MessageRef:
        if isinstance(ref, MessageRef):
            return ref
        if match := re.fullmatch(r"(-?[1-9]\d*):([1-9]\d*)", ref):
            return MessageRef(chat_id=match[1], message_id=int(match[2]))
        url = urlparse(ref)
        if (
            url.scheme not in ("https", "http")
            or url.hostname not in ("t.me", "telegram.me", "www.t.me")
            or url.username
        ):
            raise ToolError(
                "INVALID_REFERENCE",
                "Expected a t.me message link, chat_id:message_id, or MessageRef.",
            )
        parts = url.path.strip("/").split("/")
        if parts and parts[0] == "s":
            parts = parts[1:]
        if len(parts) in (3, 4) and parts[0] == "c" and all(p.isdigit() for p in parts[1:]):
            if int(parts[1]) <= 0:
                raise ToolError("INVALID_REFERENCE", "Channel IDs must be positive.")
            cid, mid = peer_id(types.PeerChannel(int(parts[1]))), int(parts[-1])
        elif len(parts) in (2, 3) and all(p.isdigit() for p in parts[1:]):
            chat = await self.chats.resolve_chat(parts[0])
            cid, mid = chat.id, int(parts[-1])
        else:
            raise ToolError("INVALID_REFERENCE", "Unsupported Telegram message link.")
        params = parse_qs(url.query)
        if "comment" in params:
            values = params["comment"]
            if len(values) != 1 or not values[0].isdigit() or int(values[0]) <= 0:
                raise ToolError("INVALID_REFERENCE", "Invalid comment ID.")
            cid = await self.chats.discussion_chat(await self.chats.resolve_chat(cid))
            mid = int(values[0])
        if mid <= 0:
            raise ToolError("INVALID_REFERENCE", "Message IDs must be positive.")
        return MessageRef(chat_id=cid, message_id=mid)

    async def message(self, ref: MessageRef) -> Message | None:
        return (await self.messages(ref.chat_id, [ref.message_id]))[ref.message_id]

    async def messages(self, chat_id: str, message_ids: list[int]) -> dict[int, Message | None]:
        unique_ids = list(dict.fromkeys(message_ids))
        result: dict[int, Message | None] = dict.fromkeys(unique_ids)
        if not unique_ids:
            return result
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(chat_id)
        raw_messages = await self.runtime.call(client.get_messages(int(chat_id), ids=unique_ids))
        for raw in raw_messages:
            if raw and not isinstance(raw, types.MessageEmpty) and raw.id in result:
                result[raw.id] = normalize_message(raw, chat)
        return result

    async def history_window(
        self, chat_id: str, *, anchor_id: int, limit: int, newer: bool
    ) -> HistoryWindow:
        """Read one bounded history response without filling short or empty pages."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "limit must be an integer from 1 to 100.")
        if not valid_topic_id(anchor_id):
            raise ToolError("INVALID_ARGUMENT", "anchor_id must be a positive 32-bit integer.")
        if type(newer) is not bool:
            raise ToolError("INVALID_ARGUMENT", "newer must be a boolean.")
        if (newer and anchor_id == MAX_TOPIC_ID) or (not newer and anchor_id == 1):
            return HistoryWindow([], 0, True)
        chat = await self.chats.resolve_chat(chat_id)
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_input_entity(int(chat_id)))
        response = await self.runtime.call(
            client(
                functions.messages.GetHistoryRequest(
                    peer=entity,
                    offset_id=anchor_id + 1 if newer else anchor_id,
                    offset_date=None,
                    add_offset=-limit if newer else 0,
                    limit=limit,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            )
        )
        if not isinstance(
            response,
            (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages),
        ) or not all(
            isinstance(getattr(response, name, None), list)
            for name in ("messages", "users", "chats")
        ):
            raise ToolError(
                "ALBUM_SCAN_INCOMPLETE", "Telegram returned an unusable history window."
            )
        issues: list[Issue] = []

        def incomplete(detail: str) -> None:
            issue = ToolError("ALBUM_SCAN_INCOMPLETE", detail).issue(chat_id)
            if issue not in issues:
                issues.append(issue)

        raw_messages = response.messages
        if len(raw_messages) > limit:
            incomplete("Telegram returned more messages than the requested history window.")
        entities: dict[int, Any] = {}
        for value in [*response.users, *response.chats]:
            try:
                entities[utils.get_peer_id(value)] = value
            except (TypeError, ValueError):
                incomplete("Telegram returned incomplete sender metadata.")
        positioned: dict[int, Any] = {}
        for raw in raw_messages[:limit]:
            mid = getattr(raw, "id", None)
            if not valid_topic_id(mid):
                incomplete("Telegram returned a message without a usable position.")
                continue
            raw_peer = getattr(raw, "peer_id", None)
            if raw_peer is not None:
                try:
                    same_chat = peer_id(raw_peer) == chat_id
                except (TypeError, ValueError):
                    same_chat = False
                if not same_chat:
                    incomplete("Telegram returned a message outside the requested chat.")
                    continue
            elif not isinstance(raw, types.MessageEmpty):
                incomplete("Telegram returned a message without a usable chat.")
                continue
            if mid <= anchor_id if newer else mid >= anchor_id:
                continue
            if not isinstance(raw, (types.Message, types.MessageService, types.MessageEmpty)):
                incomplete("Telegram returned incomplete message metadata.")
                continue
            if mid not in positioned or isinstance(positioned[mid], types.MessageEmpty):
                positioned[mid] = raw
        output: list[Message] = []
        for mid in sorted(positioned, reverse=not newer):
            raw = positioned[mid]
            if isinstance(raw, types.MessageEmpty):
                issues.append(
                    ToolError(
                        "MESSAGE_UNAVAILABLE",
                        "A scanned message is unavailable; its album membership is unknown.",
                    ).issue(chat_id, MessageRef(chat_id=chat_id, message_id=mid))
                )
                continue
            if not isinstance(getattr(raw, "date", None), datetime):
                incomplete("Telegram returned incomplete message metadata.")
                continue
            try:
                output.append(normalize_message(raw, chat, entities=entities))
            except (AttributeError, TypeError, ValueError):
                incomplete("Telegram returned incomplete message metadata.")
        # A short slice can omit restricted messages; it does not prove a history boundary.
        exhausted = not issues and (
            not raw_messages
            or isinstance(response, types.messages.Messages)
            and len(raw_messages) < limit
        )
        return HistoryWindow(output, min(len(raw_messages), limit), exhausted, issues)

    async def thread_root(self, ref: MessageRef, discussion_chat_id: str) -> MessageRef:
        if ref.chat_id == discussion_chat_id:
            return ref
        client = await self.runtime.ensure()
        response = await self.runtime.call(
            client(
                functions.messages.GetDiscussionMessageRequest(
                    peer=await self.runtime.call(client.get_input_entity(int(ref.chat_id))),
                    msg_id=ref.message_id,
                )
            )
        )
        for raw in response.messages:
            if peer_id(raw.peer_id) == discussion_chat_id:
                return MessageRef(chat_id=discussion_chat_id, message_id=raw.id)
        raise ToolError("THREAD_UNAVAILABLE", "No discussion root is available for this post.")

    async def _read_history(
        self,
        chat_id: str,
        request: HistoryRead | SearchRead | PinnedRead | ThreadRead,
        *,
        offset: int,
        limit: int,
    ) -> Batch:
        filters = request.filters
        date_from, date_to = filters.date_from, filters.date_to
        content_type, oldest_first = filters.content_type, filters.oldest_first
        pinned_only = isinstance(request, PinnedRead)
        query = request.query if isinstance(request, SearchRead) else None
        sender_id = request.sender_id if isinstance(request, SearchRead) else None
        thread_id = request.thread_id if isinstance(request, ThreadRead) else None
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(chat_id)
        kwargs: dict[str, Any] = {"limit": limit, "offset_id": offset, "reverse": oldest_first}
        if pinned_only:
            kwargs["filter"] = types.InputMessagesFilterPinned()
        if query is not None:
            kwargs["search"] = query
        if sender_id is not None:
            kwargs["from_user"] = int(sender_id)
        if thread_id is not None:
            kwargs["reply_to"] = thread_id
        if offset == 0:
            if oldest_first and date_from:
                kwargs["offset_date"] = date_from - timedelta(seconds=1)
            elif not oldest_first and date_to:
                # Telegram timestamps are integer seconds; include then filter the boundary.
                kwargs["offset_date"] = date_to + timedelta(seconds=1)
        raw_messages = await self.runtime.call(client.get_messages(int(chat_id), **kwargs))
        usable = [m for m in raw_messages if m and not isinstance(m, types.MessageEmpty)]
        positioned = [m for m in raw_messages if m and getattr(m, "id", 0) > 0]
        next_offset = positioned[-1].id if positioned else offset
        exhausted = len(raw_messages) < limit
        output: list[Message] = []
        for raw in usable:
            sent_at = utc(raw.date)
            if date_from and sent_at < date_from:
                if not oldest_first:
                    exhausted = True
                continue
            if date_to and sent_at >= date_to:
                if oldest_first:
                    exhausted = True
                continue
            message = normalize_message(raw, chat)
            if content_type is None or message.content_type == content_type:
                output.append(message)
        return Batch(output, next_offset, exhausted)

    async def _read_topic(
        self, chat_id: str, request: TopicRead, *, offset: int, limit: int
    ) -> Batch:
        topic_id = request.topic_id
        pinned_only = request.pinned_only
        filters = request.filters
        date_from, date_to = filters.date_from, filters.date_to
        oldest_first = filters.oldest_first
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "limit must be between 1 and 100.")
        if type(offset) is not int or not 0 <= offset <= MAX_TOPIC_ID:
            raise ToolError("INVALID_ARGUMENT", "Invalid message offset.")
        chat = await self.chats.resolve_chat(chat_id)
        if not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum.")
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_input_entity(int(chat_id)))
        offset_date = None
        offset_id = offset
        if oldest_first:
            if offset == MAX_TOPIC_ID:
                return Batch([], offset, True)
            offset_id = offset + 1
            if not offset and date_from is not None:
                offset_id = 0
                offset_date = date_from - timedelta(seconds=1)
        elif not offset and date_to is not None:
            offset_date = date_to + timedelta(seconds=1)
        if pinned_only:
            rpc_request = functions.messages.SearchRequest(
                peer=entity,
                q="",
                top_msg_id=topic_id,
                filter=types.InputMessagesFilterPinned(),
                min_date=None,
                max_date=date_to + timedelta(seconds=1) if date_to is not None else None,
                offset_id=offset + 1 if oldest_first else offset,
                add_offset=-limit if oldest_first else 0,
                limit=limit,
                max_id=0,
                min_id=0,
                hash=0,
            )
        else:
            rpc_request = functions.messages.GetRepliesRequest(
                peer=entity,
                msg_id=topic_id,
                offset_id=offset_id,
                offset_date=offset_date,
                add_offset=-limit if oldest_first else 0,
                limit=limit,
                max_id=0,
                min_id=0,
                hash=0,
            )
        response = await self.runtime.call(client(rpc_request))
        issues: list[Issue] = []

        def incomplete(message: str) -> None:
            issue = ToolError("PAGINATION_INCOMPLETE", message).issue(chat_id)
            if issue not in issues:
                issues.append(issue)

        if not isinstance(
            response,
            (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages),
        ):
            incomplete("Telegram returned no usable message page.")
            return Batch([], offset, True, issues)
        raw_messages = response.messages
        if not raw_messages:
            return Batch([], offset, True)
        entities = {utils.get_peer_id(value): value for value in [*response.users, *response.chats]}
        positioned: dict[int, Any] = {}
        for raw in raw_messages:
            mid = getattr(raw, "id", None)
            if not valid_topic_id(mid):
                incomplete("Telegram returned a message without a usable position.")
                continue
            raw_peer = getattr(raw, "peer_id", None)
            if raw_peer is not None and peer_id(raw_peer) != chat_id:
                incomplete("Telegram returned a message outside the requested chat.")
                continue
            if raw_peer is None and not isinstance(raw, types.MessageEmpty):
                incomplete("Telegram returned a message without a usable chat.")
                continue
            if offset and (mid <= offset if oldest_first else mid >= offset):
                continue
            positioned.setdefault(mid, raw)
        if not positioned:
            incomplete("Telegram returned a message page without an advancing position.")
            return Batch([], offset, True, issues)
        ordered = [positioned[mid] for mid in sorted(positioned, reverse=not oldest_first)]
        next_offset = ordered[-1].id
        exhausted = False
        output: list[Message] = []
        for raw in ordered:
            if isinstance(raw, types.MessageEmpty):
                continue
            if not isinstance(raw, (types.Message, types.MessageService)) or not isinstance(
                getattr(raw, "date", None), datetime
            ):
                incomplete("Telegram returned incomplete message metadata.")
                continue
            if message_topic_id(raw, chat) != topic_id:
                incomplete("Telegram returned a message outside the requested topic.")
                continue
            sent_at = utc(raw.date)
            if date_from is not None and sent_at < date_from:
                if not oldest_first:
                    exhausted = True
                continue
            if date_to is not None and sent_at >= date_to:
                if oldest_first:
                    exhausted = True
                continue
            message = normalize_message(raw, chat, entities=entities)
            if matches_filters(message, filters):
                output.append(message)
        return Batch(output, next_offset, exhausted, issues)
