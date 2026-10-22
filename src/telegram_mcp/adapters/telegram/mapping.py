"""Pure conversion of Telegram payloads to application contracts."""

from datetime import UTC, datetime
from typing import Any, TypeGuard

from telethon import types, utils

from telegram_mcp.adapters.telegram.metadata import (
    normalize_entities,
    normalize_poll,
    normalize_reactions,
)
from telegram_mcp.domain.models import (
    Attachment,
    Chat,
    ContentType,
    Folder,
    ForumTopic,
    ForwardOrigin,
    Message,
    MessageRef,
    PublicChannel,
    Sender,
    ToolError,
    WebPage,
)

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


def chat_from_entity(
    entity: Any, dialog: Any = None, default_muted: bool = False, *, now: datetime
) -> Chat:
    if isinstance(entity, types.User):
        kind = "saved" if entity.is_self else "bot" if entity.bot else "private"
    else:
        kind = "channel" if getattr(entity, "broadcast", False) else "group"
    muted = default_muted
    if dialog is not None:
        notify = getattr(dialog.dialog, "notify_settings", None)
        until = getattr(notify, "mute_until", None)
        if until is not None:
            muted = utc(until) > now
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


def normalize_web_page(media: Any) -> WebPage | None:
    if not isinstance(media, types.MessageMediaWebPage):
        return None
    raw = media.webpage
    statuses = {
        types.WebPage: "available",
        types.WebPagePending: "pending",
        types.WebPageEmpty: "empty",
        types.WebPageNotModified: "not_modified",
    }
    status = statuses.get(type(raw))
    if status is None:
        return None
    return WebPage.model_validate(
        {
            "status": status,
            "id": str(raw.id) if getattr(raw, "id", None) is not None else None,
            **{
                name: getattr(raw, name, None)
                for name in (
                    "url",
                    "display_url",
                    "type",
                    "site_name",
                    "title",
                    "description",
                    "author",
                )
            },
            "pending_until": utc(raw.date)
            if isinstance(raw, types.WebPagePending) and isinstance(raw.date, datetime)
            else None,
        }
    )


def normalize_message(raw: Any, chat: Chat, *, entities: dict[int, Any] | None = None) -> Message:
    ref = MessageRef(chat_id=chat.id, message_id=raw.id)
    author = getattr(raw, "sender", None)
    sender_id = getattr(raw, "sender_id", None)
    if sender_id is None and getattr(raw, "from_id", None) is not None:
        sender_id = utils.get_peer_id(raw.from_id)
    if author is None and entities is not None and sender_id is not None:
        author = entities.get(sender_id)
    sender = Sender(
        id=str(sender_id) if sender_id else None,
        name=utils.get_display_name(author) if author else getattr(raw, "post_author", None),
        type="user" if isinstance(author, types.User) else "chat" if author else "unknown",
    )
    content: ContentType = "text"
    attachments: list[Attachment] = []
    media = getattr(raw, "media", None)
    document = media.document if isinstance(media, types.MessageMediaDocument) else None
    photo = media.photo if isinstance(media, types.MessageMediaPhoto) else None
    if isinstance(document, types.Document):
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
    elif isinstance(photo, types.Photo):
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
        has_link=isinstance(media, types.MessageMediaWebPage)
        or any(
            isinstance(entity, (types.MessageEntityUrl, types.MessageEntityTextUrl))
            for entity in getattr(raw, "entities", None) or []
        ),
        web_page=normalize_web_page(media),
        entities=normalize_entities(getattr(raw, "entities", None)),
        reply_to=MessageRef(chat_id=reply_chat, message_id=reply_mid) if reply_mid else None,
        thread_ref=MessageRef(chat_id=thread_chat, message_id=top_mid) if top_mid else None,
        topic_id=topic_id,
        forward_origin=origin,
        attachments=attachments,
        album_id=str(raw.grouped_id) if getattr(raw, "grouped_id", None) else None,
        url=link,
        view_count=getattr(raw, "views", None),
        forward_count=getattr(raw, "forwards", None),
        reply_count=getattr(getattr(raw, "replies", None), "replies", None),
        reactions=normalize_reactions(getattr(raw, "reactions", None)),
        poll=normalize_poll(getattr(raw, "media", None)),
    )
