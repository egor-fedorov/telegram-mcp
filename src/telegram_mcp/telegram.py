"""Telethon boundary. Generated TL objects are dynamic; the rest of the app is typed."""

import asyncio
import fcntl
import os
import re
import sqlite3
from collections.abc import Awaitable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient, errors, functions, types, utils

from telegram_mcp.backend import Batch
from telegram_mcp.config import Settings
from telegram_mcp.models import (
    Attachment,
    Chat,
    ContentType,
    Entity,
    Folder,
    ForwardOrigin,
    Message,
    MessageRef,
    Sender,
    ToolError,
)

T = TypeVar("T")


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def peer_id(peer: Any) -> str:
    return str(utils.get_peer_id(peer))


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
        username=getattr(entity, "username", None),
        is_contact=bool(getattr(entity, "contact", False)),
        is_muted=muted,
        is_archived=bool(dialog and dialog.archived),
        is_supergroup=bool(getattr(entity, "megagroup", False)),
        is_community=type(entity).__name__ in ("Community", "CommunityForbidden"),
        unread_count=int(dialog.unread_count if dialog else 0),
        unread_mentions_count=int(getattr(dialog, "unread_mentions_count", 0)),
        marked_unread=bool(dialog and getattr(dialog.dialog, "unread_mark", False)),
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
    fwd = getattr(raw, "fwd_from", None)
    origin: ForwardOrigin | None = None
    if fwd:
        from_peer = getattr(fwd, "from_id", None)
        cid = peer_id(from_peer) if from_peer else None
        mid = getattr(fwd, "channel_post", None)
        origin = ForwardOrigin(
            sender_id=cid,
            name=getattr(fwd, "from_name", None),
            message_ref=MessageRef(chat_id=cid, message_id=mid) if cid and mid else None,
            sent_at=utc(fwd.date) if getattr(fwd, "date", None) else None,
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
        thread_ref=MessageRef(chat_id=reply_chat, message_id=top_mid) if top_mid else None,
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
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
            raise ToolError(
                "AUTH_REQUIRED", "Telegram session is no longer authorized. Run login."
            ) from None
        except errors.RPCError as exc:
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
                        device_model="Telegram Context MCP",
                        app_version="0.1.0",
                    )
                except (sqlite3.Error, OSError):
                    raise ToolError(
                        "SESSION_ERROR", "Cannot open the local Telegram session database."
                    ) from None
                await self.call(client.connect())
                if not await self.call(client.is_user_authorized()):
                    raise ToolError(
                        "AUTH_REQUIRED", "Session is not authorized. Run telegram-mcp login."
                    )
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
            await self.ensure()
            return {"authorized": True, "connected": self.client.is_connected()}
        except ToolError as exc:
            return {
                "authorized": False,
                "connected": False,
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

    async def resolve_chat(self, ref: str) -> Chat:
        if ref in self.chats:
            return self.chats[ref]
        client = await self.ensure()
        value = ref.strip()
        if value.startswith(("https://", "http://")):
            url = urlparse(value)
            if url.hostname not in ("t.me", "telegram.me", "www.t.me") or url.username:
                raise ToolError(
                    "INVALID_REFERENCE", "Use a Telegram chat ID, username or t.me link."
                )
            value = url.path.strip("/")
            if "/" in value or value.startswith("+"):
                raise ToolError(
                    "INVALID_REFERENCE", "Invite links and message links are not chat references."
                )
        target: int | str = int(value) if re.fullmatch(r"-?[1-9]\d*", value) else value
        if isinstance(target, str) and not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}|me", target):
            raise ToolError("INVALID_REFERENCE", "Use a numeric chat ID or exact username.")
        entity = await self.call(client.get_entity(target))
        chat = chat_from_entity(entity)
        self.chats[chat.id] = chat
        return chat

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
        client = await self.ensure()
        chat = await self.resolve_chat(ref.chat_id)
        raw = await self.call(client.get_messages(int(ref.chat_id), ids=ref.message_id))
        if not raw or isinstance(raw, types.MessageEmpty):
            return None
        return normalize_message(raw, chat)

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
    ) -> Batch:
        client = await self.ensure()
        chat = await self.resolve_chat(chat_id)
        kwargs: dict[str, Any] = {"limit": limit, "offset_id": offset, "reverse": oldest_first}
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
        next_offset = usable[-1].id if usable else offset
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
