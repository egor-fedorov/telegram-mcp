"""Telethon boundary. Generated TL objects are dynamic; the rest of the app is typed."""

import asyncio
import fcntl
import os
import re
import sqlite3
from collections.abc import Awaitable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard, TypeVar
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient, errors, functions, types, utils

from telegram_mcp.backend import Batch, DiscoveryBatch, TopicBatch, TopicOffset
from telegram_mcp.config import Settings
from telegram_mcp.models import (
    Attachment,
    Chat,
    ContentType,
    Entity,
    Folder,
    ForumTopic,
    ForwardOrigin,
    Issue,
    Message,
    MessageRef,
    PublicChannel,
    Sender,
    ToolError,
)

T = TypeVar("T")
MAX_TOPIC_ID = 2**31 - 1


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def peer_id(peer: Any) -> str:
    return str(utils.get_peer_id(peer))


def public_username(entity: Any) -> str | None:
    return getattr(entity, "username", None) or next(
        (
            username.username
            for username in getattr(entity, "usernames", None) or []
            if username.active and username.username
        ),
        None,
    )


def public_channel_from_entity(entity: Any) -> PublicChannel | None:
    if not isinstance(entity, types.Channel) or not entity.broadcast or entity.megagroup:
        return None
    if entity.banned_rights and entity.banned_rights.view_messages:
        return None
    username = public_username(entity)
    if not username:
        return None
    return PublicChannel(
        id=peer_id(entity),
        title=entity.title,
        username=username,
        url=f"https://t.me/{username}",
        participants_count=entity.participants_count,
    )


def is_supported_forum(entity: Any) -> bool:
    return bool(
        isinstance(entity, types.Channel)
        and entity.megagroup
        and entity.forum
        and not entity.broadcast
        and not entity.monoforum
        and not (entity.banned_rights and entity.banned_rights.view_messages)
    )


def valid_topic_id(value: Any) -> TypeGuard[int]:
    return type(value) is int and 0 < value <= MAX_TOPIC_ID


def message_topic_id(raw: Any, chat: Chat) -> int | None:
    if not chat.is_forum:
        return None
    if isinstance(getattr(raw, "action", None), types.MessageActionTopicCreate):
        return raw.id if valid_topic_id(raw.id) else None
    reply = getattr(raw, "reply_to", None)
    if not getattr(reply, "forum_topic", False):
        # General can contain ordinary message threads with their own top IDs.
        return 1
    top_id = getattr(reply, "reply_to_top_id", None)
    if top_id is not None:
        return top_id if valid_topic_id(top_id) else None
    reply_peer = getattr(reply, "reply_to_peer_id", None)
    if reply_peer is not None and peer_id(reply_peer) != chat.id:
        return None
    reply_id = getattr(reply, "reply_to_msg_id", None)
    return reply_id if valid_topic_id(reply_id) else None


def chat_from_entity(entity: Any, dialog: Any = None, default_muted: bool = False) -> Chat:
    if isinstance(entity, types.User):
        kind = "saved" if entity.is_self else "bot" if entity.bot else "private"
    else:
        kind = "channel" if getattr(entity, "broadcast", False) else "group"
    muted = default_muted
    if dialog is not None:
        notify = getattr(dialog.dialog, "notify_settings", None)
        until = getattr(notify, "mute_until", None)
        if until is not None:
            muted = utc(until) > datetime.now(UTC)
    return Chat(
        id=peer_id(entity),
        title=utils.get_display_name(entity) or "Unknown",
        type=kind,
        username=public_username(entity),
        is_contact=bool(getattr(entity, "contact", False)),
        is_muted=muted,
        is_archived=bool(dialog and dialog.archived),
        is_supergroup=bool(getattr(entity, "megagroup", False)),
        is_forum=is_supported_forum(entity),
        is_community=type(entity).__name__ in ("Community", "CommunityForbidden"),
        unread_count=int(dialog.unread_count if dialog else 0),
        unread_mentions_count=int(getattr(dialog, "unread_mentions_count", 0)),
        marked_unread=bool(dialog and getattr(dialog.dialog, "unread_mark", False)),
    )


def forum_topic_from_tl(raw: types.ForumTopic | types.ForumTopicDeleted, chat: Chat) -> ForumTopic:
    if isinstance(raw, types.ForumTopicDeleted):
        return ForumTopic(id=raw.id, chat_id=chat.id, is_deleted=True)
    short = bool(raw.short)
    if raw.id == 1:
        url = None
    elif chat.username:
        url = f"https://t.me/{chat.username}/{raw.id}"
    else:
        native_id, _ = utils.resolve_id(int(chat.id))
        url = f"https://t.me/c/{native_id}/{raw.id}"
    return ForumTopic(
        id=raw.id,
        chat_id=chat.id,
        title=raw.title,
        created_at=utc(raw.date) if raw.date is not None else None,
        is_general=raw.id == 1,
        is_closed=bool(raw.closed),
        is_hidden=None if short else bool(raw.hidden),
        is_pinned=None if short else bool(raw.pinned),
        is_short=short,
        unread_count=None if short else raw.unread_count,
        icon_color=raw.icon_color,
        icon_emoji_id=str(raw.icon_emoji_id) if raw.icon_emoji_id is not None else None,
        root_message_ref=MessageRef(chat_id=chat.id, message_id=raw.id) if raw.id != 1 else None,
        top_message_ref=MessageRef(chat_id=chat.id, message_id=raw.top_message)
        if not short and raw.top_message > 0
        else None,
        url=url,
    )


def folder_from_tl(raw: Any, position: int, self_id: str | None = None) -> Folder:
    def folder_peer_id(peer: Any) -> str:
        if isinstance(peer, types.InputPeerSelf):
            if self_id is None:
                raise ToolError("CATALOG_ERROR", "Could not resolve Saved Messages in a folder.")
            return self_id
        return peer_id(peer)

    title = raw.title if isinstance(raw.title, str) else raw.title.text
    names = ("exclude_muted", "exclude_read", "exclude_archived")
    flags = {name: bool(getattr(raw, name, False)) for name in names}
    for own, native in (
        ("include_contacts", "contacts"),
        ("include_non_contacts", "non_contacts"),
        ("include_bots", "bots"),
        ("include_groups", "groups"),
        ("include_channels", "broadcasts"),
    ):
        flags[own] = bool(getattr(raw, native, False))
    return Folder(
        id=raw.id,
        title=title,
        position=position,
        included_chat_ids=[folder_peer_id(p) for p in getattr(raw, "include_peers", [])],
        pinned_chat_ids=[folder_peer_id(p) for p in getattr(raw, "pinned_peers", [])],
        excluded_chat_ids=[folder_peer_id(p) for p in getattr(raw, "exclude_peers", [])],
        **flags,
    )


def normalize_message(raw: Any, chat: Chat) -> Message:
    ref = MessageRef(chat_id=chat.id, message_id=raw.id)
    author = getattr(raw, "sender", None)
    sender_id = getattr(raw, "sender_id", None)
    sender = Sender(
        id=str(sender_id) if sender_id else None,
        name=utils.get_display_name(author) if author else getattr(raw, "post_author", None),
        type="user" if isinstance(author, types.User) else "chat" if author else "unknown",
    )
    content: ContentType = "text"
    attachments: list[Attachment] = []
    document = getattr(raw, "document", None)
    photo = getattr(raw, "photo", None)
    if document:
        content = (
            "voice" if raw.voice else "video" if raw.video else "audio" if raw.audio else "document"
        )
        file = raw.file
        attachments.append(
            Attachment(
                id=f"{ref.key}:document",
                type=content,
                name=getattr(file, "name", None),
                mime_type=document.mime_type,
                size=document.size,
            )
        )
    elif photo:
        content = "photo"
        attachments.append(Attachment(id=f"{ref.key}:photo", type="photo", mime_type="image/jpeg"))
    elif getattr(raw, "media", None) and not isinstance(raw.media, types.MessageMediaWebPage):
        content = "other"
    elif getattr(raw, "action", None):
        content = "other"
    reply = getattr(raw, "reply_to", None)
    reply_peer = getattr(reply, "reply_to_peer_id", None)
    reply_chat = peer_id(reply_peer) if reply_peer else chat.id
    reply_mid = getattr(reply, "reply_to_msg_id", None)
    top_mid = getattr(reply, "reply_to_top_id", None)
    topic_id = message_topic_id(raw, chat)
    thread_chat = chat.id if chat.is_forum and getattr(reply, "forum_topic", False) else reply_chat
    if chat.is_forum and getattr(reply, "forum_topic", False) and top_mid == 1:
        top_mid = None
    fwd = getattr(raw, "fwd_from", None)
    origin: ForwardOrigin | None = None
    if fwd:
        from_peer = getattr(fwd, "from_id", None)
        cid = peer_id(from_peer) if from_peer else None
        mid = getattr(fwd, "channel_post", None)
        saved_peer = getattr(fwd, "saved_from_peer", None)
        saved_mid = getattr(fwd, "saved_from_msg_id", None)
        saved_sender = getattr(fwd, "saved_from_id", None)
        origin = ForwardOrigin(
            sender_id=cid,
            name=getattr(fwd, "from_name", None),
            message_ref=MessageRef(chat_id=cid, message_id=mid) if cid and mid else None,
            sent_at=utc(fwd.date) if getattr(fwd, "date", None) else None,
            post_author=getattr(fwd, "post_author", None),
            saved_from_ref=MessageRef(chat_id=peer_id(saved_peer), message_id=saved_mid)
            if saved_peer and saved_mid
            else None,
            saved_from_sender_id=peer_id(saved_sender) if saved_sender else None,
            saved_from_name=getattr(fwd, "saved_from_name", None),
            saved_at=utc(fwd.saved_date) if getattr(fwd, "saved_date", None) else None,
        )
    link = None
    if chat.type == "channel" or chat.is_supergroup:
        if chat.username:
            link = f"https://t.me/{chat.username}/{raw.id}"
        else:
            native_id, _ = utils.resolve_id(int(chat.id))
            link = f"https://t.me/c/{native_id}/{raw.id}"
    text = getattr(raw, "message", None) or ""
    return Message(
        message_ref=ref,
        chat=chat,
        sender=sender,
        sent_at=utc(raw.date),
        edited_at=utc(raw.edit_date) if getattr(raw, "edit_date", None) else None,
        text=text,
        text_total_length=len(text),
        content_type=content,
        entities=[
            Entity(
                type=type(e).__name__, offset=e.offset, length=e.length, url=getattr(e, "url", None)
            )
            for e in getattr(raw, "entities", None) or []
        ],
        reply_to=MessageRef(chat_id=reply_chat, message_id=reply_mid) if reply_mid else None,
        thread_ref=MessageRef(chat_id=thread_chat, message_id=top_mid) if top_mid else None,
        topic_id=topic_id,
        forward_origin=origin,
        attachments=attachments,
        album_id=str(raw.grouped_id) if getattr(raw, "grouped_id", None) else None,
        url=link,
    )


class TelegramBackend:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Any = None
        self.lock_fd: int | None = None
        self.connect_lock = asyncio.Lock()
        self.chats: dict[str, Chat] = {}

    async def call(self, request: Awaitable[T]) -> T:
        try:
            return await asyncio.wait_for(request, self.settings.rpc_timeout_seconds)
        except errors.FloodWaitError as exc:
            raise ToolError(
                "RATE_LIMITED", f"Telegram requests a wait of {exc.seconds} seconds.", exc.seconds
            ) from None
        except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError):
            raise ToolError(
                "AUTH_REQUIRED", "Telegram session is no longer authorized. Run login."
            ) from None
        except (errors.ServerError, errors.TimedOutError):
            raise ToolError(
                "TELEGRAM_UNAVAILABLE", "Telegram is temporarily unavailable. Retry this request."
            ) from None
        except (errors.QueryTooShortError, errors.SearchQueryEmptyError):
            raise ToolError(
                "INVALID_ARGUMENT", "Telegram rejected an empty or too-short search query."
            ) from None
        except errors.TopicDeletedError:
            raise ToolError(
                "TOPIC_UNAVAILABLE", "The requested forum topic is unavailable."
            ) from None
        except errors.ChannelForumMissingError:
            raise ToolError(
                "FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum."
            ) from None
        except errors.RPCError as exc:
            if exc.message == "TOPIC_ID_INVALID":
                raise ToolError(
                    "TOPIC_UNAVAILABLE", "The requested forum topic is unavailable."
                ) from None
            raise ToolError("TELEGRAM_ERROR", type(exc).__name__) from None
        except (TimeoutError, ConnectionError, OSError):
            raise ToolError(
                "CONNECTION_ERROR", "Telegram request timed out or connection failed."
            ) from None
        except (ValueError, TypeError):
            raise ToolError(
                "INVALID_REFERENCE", "Telegram could not resolve the requested entity."
            ) from None

    async def ensure(self) -> Any:
        async with self.connect_lock:
            if self.client is not None:
                return self.client
            api_id, api_hash = self.settings.credentials()
            path = self.settings.session_path
            if not path.is_file():
                raise ToolError(
                    "AUTH_REQUIRED", "No saved Telegram session. Run telegram-mcp login."
                )
            fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise ToolError(
                    "SESSION_IN_USE", "Another process owns this session. Stop it before retrying."
                ) from None
            self.lock_fd = fd
            client: Any = None
            try:
                try:
                    client = TelegramClient(
                        str(path),
                        api_id,
                        api_hash,
                        flood_sleep_threshold=0,
                        request_retries=1,
                        connection_retries=1,
                        retry_delay=1,
                        raise_last_call_error=True,
                        device_model="Telegram Context MCP",
                        app_version="0.1.0",
                    )
                except (sqlite3.Error, OSError):
                    raise ToolError(
                        "SESSION_ERROR", "Cannot open the local Telegram session database."
                    ) from None
                await self.call(client.connect())
                await self.call(client(functions.updates.GetStateRequest()))
                self.client = client
            except BaseException:
                try:
                    if client is not None:
                        with suppress(Exception):
                            await self.call(client.disconnect())
                finally:
                    os.close(fd)
                    self.lock_fd = None
                raise
            return client

    async def close(self) -> None:
        try:
            if self.client is not None:
                await self.call(self.client.disconnect())
        finally:
            self.client = None
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None

    async def status(self) -> dict[str, object]:
        try:
            existing_client = self.client
            client = await self.ensure()
            if existing_client is not None:
                await self.call(client(functions.updates.GetStateRequest()))
            return {"authorized": True, "connected": bool(client.is_connected())}
        except ToolError as exc:
            return {
                "authorized": False if exc.code == "AUTH_REQUIRED" else None,
                "connected": bool(self.client is not None and self.client.is_connected()),
                "error": exc.issue().model_dump(mode="json"),
            }

    async def catalog(self) -> tuple[list[Chat], list[Folder]]:
        client = await self.ensure()
        filters = await self.call(client(functions.messages.GetDialogFiltersRequest()))
        raw_folders = filters.filters if hasattr(filters, "filters") else filters
        dialogs = await self.call(
            client.get_dialogs(limit=self.settings.max_dialogs + 1, ignore_migrated=True)
        )
        if len(dialogs) > self.settings.max_dialogs:
            raise ToolError(
                "CATALOG_LIMIT", "Dialog catalog exceeds max_dialogs; increase it in local config."
            )
        self_id = next(
            (peer_id(d.entity) for d in dialogs if getattr(d.entity, "is_self", False)), None
        )
        if self_id is None and any(
            isinstance(peer, types.InputPeerSelf)
            for raw in raw_folders
            for field in ("include_peers", "pinned_peers", "exclude_peers")
            for peer in getattr(raw, field, [])
        ):
            self_id = peer_id(await self.call(client.get_me(input_peer=True)))
        folders = [
            folder_from_tl(f, i, self_id)
            for i, f in enumerate(raw_folders)
            if isinstance(f, (types.DialogFilter, types.DialogFilterChatlist))
        ]
        defaults: dict[str, bool] = {}
        if any(f.exclude_muted for f in folders):
            for name, peer in (
                ("channel", types.InputNotifyBroadcasts()),
                ("group", types.InputNotifyChats()),
                ("private", types.InputNotifyUsers()),
            ):
                notify = await self.call(client(functions.account.GetNotifySettingsRequest(peer)))
                until = getattr(notify, "mute_until", None)
                defaults[name] = until is not None and utc(until) > datetime.now(UTC)
        chats: list[Chat] = []
        for dialog in dialogs:
            entity = dialog.entity
            category = (
                "private"
                if isinstance(entity, types.User)
                else "channel"
                if getattr(entity, "broadcast", False)
                else "group"
            )
            chats.append(chat_from_entity(entity, dialog, defaults.get(category, False)))
        self.chats = {c.id: c for c in chats}
        return chats, folders

    async def _resolve_entity(self, ref: str) -> Any:
        value = ref.strip()
        if value.startswith(("t.me/", "telegram.me/", "www.t.me/")):
            value = "https://" + value
        if value.startswith(("https://", "http://")):
            url = urlparse(value)
            if url.hostname not in ("t.me", "telegram.me", "www.t.me") or url.username:
                raise ToolError(
                    "INVALID_REFERENCE", "Use a Telegram chat ID, username or t.me link."
                )
            value = url.path.strip("/")
            if value.startswith("s/"):
                value = value[2:]
            if "/" in value or value.startswith("+"):
                raise ToolError(
                    "INVALID_REFERENCE", "Invite links and message links are not chat references."
                )
        target: int | str = int(value) if re.fullmatch(r"-?[1-9]\d*", value) else value
        if isinstance(target, str) and not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}|me", target):
            raise ToolError("INVALID_REFERENCE", "Use a numeric chat ID or exact username.")
        client = await self.ensure()
        try:
            return await self.call(client.get_entity(target))
        except ToolError as exc:
            if isinstance(target, int) and exc.code == "INVALID_REFERENCE":
                raise ToolError(
                    "INVALID_REFERENCE",
                    "Telegram has no entity for this numeric chat ID. Use its username or t.me "
                    "link first.",
                ) from None
            raise

    async def resolve_chat(self, ref: str, *, refresh: bool = False) -> Chat:
        if not refresh and ref.strip() in self.chats:
            return self.chats[ref.strip()]
        entity = await self._resolve_entity(ref)
        chat = chat_from_entity(entity)
        self.chats[chat.id] = chat
        return chat

    async def public_channel(self, chat_id: str) -> Chat:
        # get_entity performs a fresh GetChannels RPC for a known numeric entity;
        # the access hash may be cached, but public visibility must never be cached.
        entity = await self._resolve_entity(chat_id)
        if public_channel_from_entity(entity) is None:
            raise ToolError("ACCESS_DENIED", "This chat is not a public broadcast channel.")
        chat = chat_from_entity(entity)
        if chat.id != chat_id:
            raise ToolError("ACCESS_DENIED", "Telegram returned a different channel identity.")
        self.chats[chat.id] = chat
        return chat

    async def search_public_channels(self, query: str, limit: int) -> DiscoveryBatch:
        client = await self.ensure()
        response = await self.call(
            client(functions.contacts.SearchRequest(q=query, limit=limit, broadcasts=True))
        )
        entities = {peer_id(entity): entity for entity in response.chats}
        channels: list[PublicChannel] = []
        for peer in [*response.my_results, *response.results]:
            channel = public_channel_from_entity(entities.get(peer_id(peer)))
            if channel is not None:
                channels.append(channel)
        return DiscoveryBatch(channels)

    async def get_similar_channels(self, chat_ref: str) -> DiscoveryBatch:
        # Resolve fresh under discovery permission; no dialog or message access is needed.
        entity = await self._resolve_entity(chat_ref)
        if public_channel_from_entity(entity) is None:
            raise ToolError("ACCESS_DENIED", "The source must be a public broadcast channel.")
        try:
            source = utils.get_input_channel(utils.get_input_peer(entity))
        except (TypeError, ValueError):
            raise ToolError(
                "INVALID_REFERENCE", "The source channel is unavailable. Use its username or link."
            ) from None
        client = await self.ensure()
        response = await self.call(
            client(functions.channels.GetChannelRecommendationsRequest(channel=source))
        )
        channels = [
            channel
            for entity in response.chats
            if (channel := public_channel_from_entity(entity)) is not None
        ]
        return DiscoveryBatch(channels, telegram_total_count=getattr(response, "count", None))

    async def fetch_topics(
        self,
        chat_id: str,
        *,
        offset: TopicOffset,
        limit: int = 30,
        query: str | None = None,
    ) -> TopicBatch:
        chat = await self.resolve_chat(chat_id)
        if not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum.")
        client = await self.ensure()
        entity = await self.call(client.get_input_entity(int(chat_id)))
        response = await self.call(
            client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    q=query,
                    offset_date=offset.date,
                    offset_id=offset.message_id,
                    offset_topic=offset.topic_id,
                    limit=limit,
                )
            )
        )
        topics: list[ForumTopic] = []
        issues: list[Issue] = []
        for raw in response.topics:
            if (
                not isinstance(raw, (types.ForumTopic, types.ForumTopicDeleted))
                or raw.id <= 0
                or isinstance(raw, types.ForumTopic)
                and not raw.short
                and peer_id(raw.peer) != chat_id
            ):
                issues.append(
                    ToolError(
                        "PAGINATION_INCOMPLETE",
                        "Telegram returned an unrelated topic or invalid topic data.",
                    ).issue(chat_id)
                )
                continue
            topics.append(forum_topic_from_tl(raw, chat))
        order_by_create_date = bool(response.order_by_create_date)
        if not response.topics:
            return TopicBatch(topics, None, True, order_by_create_date, response.count, issues)
        tail = response.topics[-1]
        date = None
        if (
            isinstance(tail, types.ForumTopic)
            and tail.id > 0
            and not tail.short
            and peer_id(tail.peer) == chat_id
        ):
            if order_by_create_date:
                date = tail.date
            else:
                date = next(
                    (
                        message.date
                        for message in response.messages
                        if not isinstance(message, types.MessageEmpty)
                        and getattr(message, "id", None) == tail.top_message
                        and getattr(message, "peer_id", None) is not None
                        and peer_id(message.peer_id) == chat_id
                        and isinstance(getattr(message, "date", None), datetime)
                    ),
                    None,
                )
        if date is None:
            issues.append(
                ToolError(
                    "PAGINATION_INCOMPLETE",
                    "The final topic has insufficient metadata to continue safely.",
                ).issue(chat_id)
            )
            return TopicBatch(topics, None, True, order_by_create_date, response.count, issues)
        next_offset = TopicOffset(utc(date), max(0, tail.top_message), tail.id)
        return TopicBatch(topics, next_offset, False, order_by_create_date, response.count, issues)

    async def get_topic(self, chat_id: str, topic_id: int) -> ForumTopic:
        if not valid_topic_id(topic_id):
            raise ToolError("INVALID_ARGUMENT", "topic_id must be a positive 32-bit integer.")
        chat = await self.resolve_chat(chat_id)
        if not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum.")
        client = await self.ensure()
        entity = await self.call(client.get_input_entity(int(chat_id)))
        try:
            response = await self.call(
                client(functions.messages.GetForumTopicsByIDRequest(peer=entity, topics=[topic_id]))
            )
        except ToolError as exc:
            if exc.code == "TELEGRAM_ERROR" and exc.detail == "MsgIdInvalidError":
                raise ToolError(
                    "TOPIC_UNAVAILABLE", "The requested forum topic is unavailable."
                ) from None
            raise
        matches = [
            raw for raw in getattr(response, "topics", []) if getattr(raw, "id", None) == topic_id
        ]
        if len(matches) == 1:
            raw = matches[0]
            if isinstance(raw, types.ForumTopic) and (raw.short or peer_id(raw.peer) == chat_id):
                return forum_topic_from_tl(raw, chat)
        raise ToolError("TOPIC_UNAVAILABLE", "The requested forum topic is unavailable.")

    async def chat_details(self, chat: Chat) -> dict[str, object]:
        client = await self.ensure()
        entity = await self.call(client.get_input_entity(int(chat.id)))
        if chat.type in ("private", "bot", "saved"):
            full = await self.call(client(functions.users.GetFullUserRequest(entity)))
            about = getattr(full.full_user, "about", None)
            linked = None
        elif isinstance(entity, types.InputPeerChannel):
            full = await self.call(client(functions.channels.GetFullChannelRequest(entity)))
            about = full.full_chat.about
            linked = getattr(full.full_chat, "linked_chat_id", None)
        else:
            full = await self.call(client(functions.messages.GetFullChatRequest(abs(int(chat.id)))))
            about = full.full_chat.about
            linked = None
        return {
            "description": about,
            "linked_chat_id": peer_id(types.PeerChannel(linked)) if linked else None,
            "server_can_send": False,
        }

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
            chat = await self.resolve_chat(parts[0])
            cid, mid = chat.id, int(parts[-1])
        else:
            raise ToolError("INVALID_REFERENCE", "Unsupported Telegram message link.")
        params = parse_qs(url.query)
        if "comment" in params:
            values = params["comment"]
            if len(values) != 1 or not values[0].isdigit() or int(values[0]) <= 0:
                raise ToolError("INVALID_REFERENCE", "Invalid comment ID.")
            cid = await self.discussion_chat(await self.resolve_chat(cid))
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
        client = await self.ensure()
        chat = await self.resolve_chat(chat_id)
        raw_messages = await self.call(client.get_messages(int(chat_id), ids=unique_ids))
        for raw in raw_messages:
            if raw and not isinstance(raw, types.MessageEmpty) and raw.id in result:
                result[raw.id] = normalize_message(raw, chat)
        return result

    async def discussion_chat(self, chat: Chat) -> str:
        if chat.type == "group":
            return chat.id
        if chat.type != "channel":
            raise ToolError(
                "THREAD_UNAVAILABLE",
                "Telegram threads are available in supported groups and channels.",
            )
        details = await self.chat_details(chat)
        linked = details["linked_chat_id"]
        if not isinstance(linked, str):
            raise ToolError("THREAD_UNAVAILABLE", "Channel has no linked discussion group.")
        return linked

    async def thread_root(self, ref: MessageRef, discussion_chat_id: str) -> MessageRef:
        if ref.chat_id == discussion_chat_id:
            return ref
        client = await self.ensure()
        response = await self.call(
            client(
                functions.messages.GetDiscussionMessageRequest(
                    peer=await self.call(client.get_input_entity(int(ref.chat_id))),
                    msg_id=ref.message_id,
                )
            )
        )
        for raw in response.messages:
            if peer_id(raw.peer_id) == discussion_chat_id:
                return MessageRef(chat_id=discussion_chat_id, message_id=raw.id)
        raise ToolError("THREAD_UNAVAILABLE", "No discussion root is available for this post.")

    async def fetch(
        self,
        chat_id: str,
        *,
        offset: int = 0,
        limit: int = 30,
        query: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sender_id: str | None = None,
        content_type: ContentType | None = None,
        oldest_first: bool = False,
        thread_id: int | None = None,
        pinned_only: bool = False,
        topic_id: int | None = None,
    ) -> Batch:
        if topic_id is not None:
            if not valid_topic_id(topic_id):
                raise ToolError("INVALID_ARGUMENT", "topic_id must be a positive 32-bit integer.")
            if thread_id is not None or query is not None or sender_id is not None:
                raise ToolError(
                    "INVALID_ARGUMENT",
                    "Topic history cannot be combined with thread, search, or sender selectors.",
                )
            return await self.fetch_topic_messages(
                chat_id,
                topic_id,
                offset=offset,
                limit=limit,
                date_from=date_from,
                date_to=date_to,
                content_type=content_type,
                oldest_first=oldest_first,
                pinned_only=pinned_only,
            )
        if pinned_only and thread_id is not None:
            raise ToolError("INVALID_ARGUMENT", "Pinned-message retrieval is chat-wide.")
        client = await self.ensure()
        chat = await self.resolve_chat(chat_id)
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
        raw_messages = await self.call(client.get_messages(int(chat_id), **kwargs))
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

    async def fetch_topic_messages(
        self,
        chat_id: str,
        topic_id: int,
        *,
        offset: int,
        limit: int,
        date_from: datetime | None,
        date_to: datetime | None,
        content_type: ContentType | None,
        oldest_first: bool,
        pinned_only: bool,
    ) -> Batch:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "limit must be between 1 and 100.")
        if type(offset) is not int or not 0 <= offset <= MAX_TOPIC_ID:
            raise ToolError("INVALID_ARGUMENT", "Invalid message offset.")
        chat = await self.resolve_chat(chat_id)
        if not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum.")
        client = await self.ensure()
        entity = await self.call(client.get_input_entity(int(chat_id)))
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
            request = functions.messages.SearchRequest(
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
            request = functions.messages.GetRepliesRequest(
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
        response = await self.call(client(request))
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
            raw._finish_init(client, entities, entity)
            message = normalize_message(raw, chat)
            if content_type is None or message.content_type == content_type:
                output.append(message)
        return Batch(output, next_offset, exhausted, issues)
