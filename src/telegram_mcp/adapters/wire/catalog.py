"""Chats, folders and forum topics."""

from typing import Annotated, Literal, NotRequired

from pydantic import AwareDatetime, Field, with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.common import CONFIG, Chat, ChatID, Count, Issue, MessageKey, Scope


@with_config(CONFIG)
class ChatDetails(TypedDict):
    chat: Chat
    description: NotRequired[str]
    linked_chat_id: NotRequired[ChatID]
    server_can_send: Literal[False]


@with_config(CONFIG)
class Folder(TypedDict):
    id: int
    title: str
    position: int
    included_chat_ids: list[ChatID]
    pinned_chat_ids: list[ChatID]
    excluded_chat_ids: list[ChatID]
    include_contacts: bool
    include_non_contacts: bool
    include_bots: bool
    include_groups: bool
    include_channels: bool
    exclude_muted: bool
    exclude_read: bool
    exclude_archived: bool


@with_config(CONFIG)
class FolderResult(TypedDict):
    folders: list[Folder]


@with_config(CONFIG)
class ChatPage(TypedDict):
    chats: list[Chat]
    next_cursor: str | None
    scope: Scope


@with_config(CONFIG)
class ForumTopic(TypedDict):
    id: Annotated[int, Field(gt=0)]
    chat_id: ChatID
    title: NotRequired[str]
    created_at: NotRequired[AwareDatetime]
    is_general: bool
    is_closed: NotRequired[bool]
    is_hidden: NotRequired[bool]
    is_pinned: NotRequired[bool]
    is_deleted: bool
    is_short: bool
    unread_count: NotRequired[Count]
    icon_color: NotRequired[int]
    icon_emoji_id: NotRequired[str]
    root_message_ref: NotRequired[MessageKey]
    top_message_ref: NotRequired[MessageKey]
    url: NotRequired[str]


@with_config(CONFIG)
class TopicPage(TypedDict):
    topics: list[ForumTopic]
    next_cursor: str | None
    scope: Scope
    issues: list[Issue]
    telegram_total_count: NotRequired[int]
    order: NotRequired[Literal["creation_date", "last_message_date"]]
