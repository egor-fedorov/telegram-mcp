"""Native per-chat and global search with explicit continuation positions."""

import re
from datetime import datetime, timedelta
from typing import Any

from telethon import functions, types, utils

from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import (
    MAX_TOPIC_ID,
    chat_from_entity,
    normalize_message,
    peer_id,
    utc,
    valid_topic_id,
)
from telegram_mcp.adapters.telegram.message_filters import matches_filters
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import (
    Batch,
    GlobalBatch,
    GlobalOffset,
    MessageOffset,
    SearchAnchor,
)
from telegram_mcp.application.requests import (
    ReadFilters,
    SearchRead,
)
from telegram_mcp.domain.models import Chat, ToolError


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


class MessageSearch:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats

    async def read(
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
        if (
            offset == 0
            and boundary is not None
            and (
                oldest
                or not request.query
                and not isinstance(selected_filter, types.InputMessagesFilterEmpty)
            )
        ):
            return await self._read_anchor(client, entity, chat_id, boundary, offset, oldest)
        offset_id = (
            position if isinstance(offset, SearchAnchor) else position + 1 if oldest else position
        )
        sender = None
        if request.sender_id is not None and chat.type not in ("private", "bot", "saved"):
            sender = await self.runtime.call(client.get_input_entity(int(request.sender_id)))
        rpc = self._chat_request(request, entity, selected_filter, sender, offset_id, limit)
        response = await self.runtime.call(client(rpc))
        return self._chat_page(response, chat_id, chat, request, offset, limit)

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
        offset_peer = self._global_peer(offset)
        client = await self.runtime.ensure()
        rpc = self._global_request(request, offset, offset_peer, limit)
        response = await self.runtime.call(client(rpc))
        return self._global_page(response, request, offset, limit, chat_ids)

    async def _read_anchor(
        self,
        client: Any,
        entity: Any,
        chat_id: str,
        boundary: datetime,
        offset: MessageOffset,
        oldest: bool,
    ) -> Batch:
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
            raise ToolError("PAGINATION_INCOMPLETE", "Telegram returned an inexact date boundary.")
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

    @staticmethod
    def _chat_request(
        request: SearchRead,
        entity: Any,
        selected_filter: Any,
        sender: Any,
        offset_id: int,
        limit: int,
    ) -> functions.messages.SearchRequest:
        filters = request.filters
        oldest = filters.oldest_first
        return functions.messages.SearchRequest(
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

    @staticmethod
    def _chat_page(
        response: Any,
        chat_id: str,
        chat: Chat,
        request: SearchRead,
        offset: MessageOffset,
        limit: int,
    ) -> Batch:
        filters = request.filters
        oldest = filters.oldest_first
        position = offset.offset_id if isinstance(offset, SearchAnchor) else offset
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

    def _global_peer(self, offset: GlobalOffset) -> Any:
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
        return offset_peer

    @staticmethod
    def _global_request(
        request: SearchRead,
        offset: GlobalOffset,
        offset_peer: Any,
        limit: int,
    ) -> functions.messages.SearchGlobalRequest:
        filters = request.filters
        return functions.messages.SearchGlobalRequest(
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

    def _global_page(  # noqa: C901
        self,
        response: Any,
        request: SearchRead,
        offset: GlobalOffset,
        limit: int,
        chat_ids: list[str],
    ) -> GlobalBatch:
        filters = request.filters
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
