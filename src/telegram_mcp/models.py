"""Public data contracts, independent of Telegram's generated classes."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

ChatType = Literal["private", "group", "channel", "bot", "saved"]
ContentType = Literal["text", "photo", "video", "voice", "audio", "document", "other"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Chat(Model):
    id: str
    title: str
    type: ChatType
    username: str | None = None
    is_contact: bool = False
    is_muted: bool = False
    is_archived: bool = False
    is_supergroup: bool = False
    is_forum: bool = False
    is_community: bool = False
    unread_count: int = 0
    unread_mentions_count: int = 0
    marked_unread: bool = False
    matched_folder_ids: list[int] = Field(default_factory=list)
    pinned_in_folder_ids: list[int] = Field(default_factory=list)


class PublicChannel(Model):
    id: str
    title: str
    username: str
    url: str
    participants_count: int | None = Field(default=None, ge=0)


class DiscoveryResult(Model):
    channels: list[PublicChannel]
    returned_count: int = Field(ge=0)
    telegram_total_count: int | None = Field(default=None, ge=0)
    locally_truncated: bool = False
    exhaustive: Literal[False] = False


class Folder(Model):
    id: int
    title: str
    position: int
    included_chat_ids: list[str] = Field(default_factory=list)
    pinned_chat_ids: list[str] = Field(default_factory=list)
    excluded_chat_ids: list[str] = Field(default_factory=list)
    include_contacts: bool = False
    include_non_contacts: bool = False
    include_bots: bool = False
    include_groups: bool = False
    include_channels: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = False


class MessageRef(Model):
    chat_id: str = Field(pattern=r"^-?[1-9]\d*$")
    message_id: int = Field(gt=0)

    @property
    def key(self) -> str:
        return f"{self.chat_id}:{self.message_id}"


class ForumTopic(Model):
    id: int = Field(gt=0)
    chat_id: str
    title: str | None = None
    created_at: AwareDatetime | None = None
    is_general: bool = False
    is_closed: bool | None = None
    is_hidden: bool | None = None
    is_pinned: bool | None = None
    is_deleted: bool = False
    is_short: bool = False
    unread_count: int | None = Field(default=None, ge=0)
    icon_color: int | None = None
    icon_emoji_id: str | None = None
    root_message_ref: MessageRef | None = None
    top_message_ref: MessageRef | None = None
    url: str | None = None


class Sender(Model):
    id: str | None = None
    name: str | None = None
    type: Literal["user", "chat", "unknown"] = "unknown"


class Entity(Model):
    type: str
    offset: int
    length: int
    url: str | None = None


class Attachment(Model):
    id: str
    type: ContentType
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None


class ForwardOrigin(Model):
    sender_id: str | None = None
    name: str | None = None
    message_ref: MessageRef | None = None
    sent_at: datetime | None = None
    post_author: str | None = None
    saved_from_ref: MessageRef | None = None
    saved_from_sender_id: str | None = None
    saved_from_name: str | None = None
    saved_at: datetime | None = None


class Message(Model):
    message_ref: MessageRef
    chat: Chat
    sender: Sender = Field(default_factory=Sender)
    sent_at: AwareDatetime
    edited_at: AwareDatetime | None = None
    text: str = ""
    content_type: ContentType = "text"
    entities: list[Entity] = Field(default_factory=list)
    reply_to: MessageRef | None = None
    thread_ref: MessageRef | None = None
    topic_id: int | None = Field(default=None, gt=0, le=2**31 - 1)
    forward_origin: ForwardOrigin | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    album_id: str | None = None
    url: str | None = None
    text_offset: int = 0
    text_total_length: int = 0
    next_text_offset: int | None = None


class Issue(Model):
    code: str
    message: str
    chat_id: str | None = None
    message_ref: MessageRef | None = None
    retry_after_seconds: int | None = None
    input_index: int | None = Field(default=None, ge=0)


class Scope(Model):
    folder_ids: list[int] = Field(default_factory=list)
    chat_ids: list[str] = Field(default_factory=list)
    resolved_at: AwareDatetime
    pending_chat_ids: list[str] = Field(default_factory=list)
    complete: bool = True


class Page(Model):
    messages: list[Message] = Field(default_factory=list)
    next_cursor: str | None = None
    scope: Scope
    issues: list[Issue] = Field(default_factory=list)
    order: Literal["newest_first", "oldest_first"] = "newest_first"
    thread_root: MessageRef | None = None
    topic_id: int | None = Field(default=None, gt=0, le=2**31 - 1)


class TopicPage(Model):
    topics: list[ForumTopic]
    next_cursor: str | None = None
    scope: Scope
    issues: list[Issue] = Field(default_factory=list)
    telegram_total_count: int | None = None
    order: Literal["creation_date", "last_message_date"] | None = None


class ChatPage(Model):
    chats: list[Chat]
    next_cursor: str | None = None
    scope: Scope


class MessageResult(Model):
    messages: list[Message]
    issues: list[Issue] = Field(default_factory=list)


class ContextResult(Model):
    anchor: Message | None = None
    before: list[Message] = Field(default_factory=list)
    after: list[Message] = Field(default_factory=list)
    reply_chain: list[Message] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class ToolError(Exception):
    def __init__(self, code: str, message: str, retry_after: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.code in {"RATE_LIMITED", "CONNECTION_ERROR", "TELEGRAM_UNAVAILABLE"}

    def issue(
        self,
        chat_id: str | None = None,
        ref: MessageRef | None = None,
        input_index: int | None = None,
    ) -> Issue:
        return Issue(
            code=self.code,
            message=self.detail,
            chat_id=chat_id,
            message_ref=ref,
            retry_after_seconds=self.retry_after,
            input_index=input_index,
        )
