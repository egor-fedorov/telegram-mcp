"""Compact, typed MCP response contracts and lossless message projections.

Unknown optional metadata is absent. Known false/zero values and empty result sets
remain explicit. Pagination and text-continuation controls never depend on omission.
"""

import json
from datetime import datetime
from typing import Annotated, Any, Literal, NotRequired

from mcp_types import CallToolResult, ResourceLink, TextContent
from pydantic import AwareDatetime, ConfigDict, Field, with_config
from pydantic_core import to_json
from typing_extensions import TypedDict

from telegram_mcp.domain import changes, models, watches


def _remove_titles(schema: dict[str, Any]) -> None:
    """Remove presentation labels without changing constraints or local references."""
    schema.pop("title", None)
    for key, value in schema.items():
        if key in {"properties", "$defs", "patternProperties"}:
            for child in value.values():
                _remove_titles(child)
        elif isinstance(value, dict):
            _remove_titles(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _remove_titles(item)


_CONFIG = ConfigDict(extra="forbid", json_schema_extra=_remove_titles)
Count = Annotated[int, Field(ge=0)]
ChatID = Annotated[str, Field(pattern=r"^-?[1-9]\d*$")]
MessageKey = Annotated[str, Field(pattern=r"^-?[1-9]\d*:[1-9]\d*$")]
TopicID = Annotated[int, Field(gt=0, le=2147483647)]


@with_config(_CONFIG)
class ChatSummary(TypedDict):
    title: str
    type: models.ChatType
    username: NotRequired[str]


@with_config(_CONFIG)
class ChatMetadata(ChatSummary):
    is_contact: bool
    is_muted: bool
    is_archived: bool
    is_supergroup: bool
    is_forum: bool
    is_community: bool
    unread_count: int
    unread_mentions_count: int
    marked_unread: bool
    matched_folder_ids: list[int]
    pinned_in_folder_ids: list[int]


@with_config(_CONFIG)
class Chat(ChatMetadata):
    id: ChatID


@with_config(_CONFIG)
class Issue(TypedDict):
    code: str
    message: str
    chat_id: NotRequired[ChatID]
    message_ref: NotRequired[MessageKey]
    retry_after_seconds: NotRequired[int]
    input_index: NotRequired[Count]


@with_config(_CONFIG)
class Status(TypedDict):
    authorized: bool | None
    connected: bool
    error: NotRequired[Issue]
    mode: Literal["read_only"]
    allow_all_chats: bool
    allow_public_channel_reads: bool
    allow_public_channel_discovery: bool
    allow_attachment_downloads: bool
    archive_enabled: bool
    attachment_max_bytes: Annotated[int, Field(gt=0)]
    download_ttl_seconds: Annotated[int, Field(gt=0)]
    download_max_bytes: Annotated[int, Field(gt=0)]
    download_max_files: Annotated[int, Field(gt=0)]
    allowed_chat_ids: list[ChatID]
    allowed_folder_ids: list[int]
    cursor_ttl_seconds: Annotated[int, Field(gt=0)]


@with_config(_CONFIG)
class ChatDetails(TypedDict):
    chat: Chat
    description: NotRequired[str]
    linked_chat_id: NotRequired[ChatID]
    server_can_send: Literal[False]


@with_config(_CONFIG)
class PublicChannel(TypedDict):
    id: ChatID
    title: str
    username: str
    url: str
    participants_count: NotRequired[Count]


@with_config(_CONFIG)
class DiscoveryResult(TypedDict):
    channels: list[PublicChannel]
    returned_count: Count
    telegram_total_count: NotRequired[Count]
    locally_truncated: bool
    exhaustive: Literal[False]


@with_config(_CONFIG)
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


@with_config(_CONFIG)
class FolderResult(TypedDict):
    folders: list[Folder]


@with_config(_CONFIG)
class Scope(TypedDict):
    folder_ids: list[int]
    chat_ids: Annotated[list[ChatID], Field(max_length=20)]
    chat_count: Count
    chat_ids_truncated: bool
    resolved_at: AwareDatetime
    pending_chat_ids: Annotated[list[ChatID], Field(max_length=20)]
    pending_chat_count: Count
    pending_chat_ids_truncated: bool
    complete: bool


@with_config(_CONFIG)
class ChatPage(TypedDict):
    chats: list[Chat]
    next_cursor: str | None
    scope: Scope


@with_config(_CONFIG)
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


@with_config(_CONFIG)
class TopicPage(TypedDict):
    topics: list[ForumTopic]
    next_cursor: str | None
    scope: Scope
    issues: list[Issue]
    telegram_total_count: NotRequired[int]
    order: NotRequired[Literal["creation_date", "last_message_date"]]


@with_config(_CONFIG)
class Sender(TypedDict):
    id: NotRequired[str]
    name: NotRequired[str]
    type: Literal["user", "chat", "unknown"]


@with_config(_CONFIG)
class Entity(TypedDict):
    type: str
    offset: int
    length: int
    url: NotRequired[str]
    language: NotRequired[str]
    user_id: NotRequired[str]
    document_id: NotRequired[str]
    collapsed: NotRequired[bool]
    date: NotRequired[AwareDatetime]
    relative: NotRequired[bool]
    short_time: NotRequired[bool]
    long_time: NotRequired[bool]
    short_date: NotRequired[bool]
    long_date: NotRequired[bool]
    day_of_week: NotRequired[bool]
    old_text: NotRequired[str]


@with_config(_CONFIG)
class FormattedText(TypedDict):
    text: str
    entities: NotRequired[list[Entity]]


@with_config(_CONFIG)
class ReactionCount(TypedDict):
    type: Literal["emoji", "custom_emoji", "paid", "empty", "unknown"]
    emoji: NotRequired[str]
    document_id: NotRequired[str]
    count: Count
    is_chosen: NotRequired[bool]
    chosen_order: NotRequired[Count]


@with_config(_CONFIG)
class ReactionSummary(TypedDict):
    is_min: bool
    reactions_as_tags: bool
    results: list[ReactionCount]


@with_config(_CONFIG)
class PollOption(TypedDict):
    option_id: str
    text: FormattedText


@with_config(_CONFIG)
class PollAnswerResult(TypedDict):
    option_id: str
    voter_count: NotRequired[Count]
    is_chosen: NotRequired[bool]
    is_correct: NotRequired[bool]


@with_config(_CONFIG)
class PollResults(TypedDict):
    is_min: bool
    total_voters: NotRequired[Count]
    answers: NotRequired[list[PollAnswerResult]]
    solution: NotRequired[FormattedText]


@with_config(_CONFIG)
class PollData(TypedDict):
    id: str
    question: FormattedText
    answers: list[PollOption]
    is_closed: bool
    is_quiz: bool
    public_voters: bool
    multiple_choice: bool
    allows_new_answers: bool
    revoting_disabled: bool
    shuffle_answers: bool
    hide_results_until_close: bool
    close_period_seconds: NotRequired[Count]
    closes_at: NotRequired[AwareDatetime]
    results: NotRequired[PollResults]


@with_config(_CONFIG)
class Attachment(TypedDict):
    id: str
    type: models.ContentType
    name: NotRequired[str]
    mime_type: NotRequired[str]
    size: NotRequired[int]


@with_config(_CONFIG)
class SavedFile(TypedDict):
    local_path: str
    size: Count
    sha256: str
    saved_at: AwareDatetime


@with_config(_CONFIG)
class DownloadResult(TypedDict):
    attachment_id: str
    message_ref: MessageKey
    name: str
    mime_type: str
    size: Count
    sha256: str
    local_path: str
    resource_uri: str
    expires_at: AwareDatetime
    saved_file: NotRequired[SavedFile]


@with_config(_CONFIG)
class ForwardOrigin(TypedDict):
    sender_id: NotRequired[str]
    name: NotRequired[str]
    message_ref: NotRequired[MessageKey]
    sent_at: NotRequired[datetime]
    post_author: NotRequired[str]
    saved_from_ref: NotRequired[MessageKey]
    saved_from_sender_id: NotRequired[str]
    saved_from_name: NotRequired[str]
    saved_at: NotRequired[datetime]


@with_config(_CONFIG)
class WebPage(TypedDict):
    status: Literal["available", "pending", "empty", "not_modified"]
    id: NotRequired[str]
    url: NotRequired[str]
    display_url: NotRequired[str]
    type: NotRequired[str]
    site_name: NotRequired[str]
    title: NotRequired[str]
    description: NotRequired[str]
    author: NotRequired[str]
    pending_until: NotRequired[AwareDatetime]


@with_config(_CONFIG)
class Message(TypedDict):
    message_ref: MessageKey
    sender: NotRequired[Sender]
    sent_at: AwareDatetime
    edited_at: NotRequired[AwareDatetime]
    text: str
    content_type: models.ContentType
    has_link: bool
    web_page: NotRequired[WebPage]
    entities: NotRequired[list[Entity]]
    reply_to: NotRequired[MessageKey]
    thread_ref: NotRequired[MessageKey]
    topic_id: NotRequired[TopicID]
    forward_origin: NotRequired[ForwardOrigin]
    attachments: NotRequired[list[Attachment]]
    album_id: NotRequired[str]
    view_count: NotRequired[Count]
    forward_count: NotRequired[Count]
    reply_count: NotRequired[Count]
    reactions: NotRequired[ReactionSummary]
    poll: NotRequired[PollData]
    url: NotRequired[str]
    text_offset: int
    text_total_length: int
    next_text_offset: int | None


@with_config(_CONFIG)
class SearchProgress(TypedDict):
    sources_total: Count
    sources_started: Count
    sources_ready: Count
    sources_exhausted: Count
    rpc_pages_this_call: Count


@with_config(_CONFIG)
class MessageResult(TypedDict):
    chats: dict[ChatID, ChatSummary]
    messages: list[Message]
    issues: list[Issue]


@with_config(_CONFIG)
class Page(MessageResult):
    next_cursor: str | None
    scope: Scope
    order: Literal["newest_first", "oldest_first"]
    ordering: Literal["page", "global"]
    thread_root: NotRequired[MessageKey]
    topic_id: NotRequired[TopicID]
    progress: NotRequired[SearchProgress]


@with_config(_CONFIG)
class AlbumResult(TypedDict):
    chats: dict[ChatID, ChatSummary]
    anchor_ref: MessageKey
    album_id: str
    messages: Annotated[list[Message], Field(max_length=201)]
    order: Literal["oldest_first"]
    completeness: Literal["unknown"]
    scan_limit: Annotated[int, Field(ge=1, le=100)]
    scanned_before: Annotated[int, Field(ge=0, le=100)]
    scanned_after: Annotated[int, Field(ge=0, le=100)]
    before_exhausted: bool | None
    after_exhausted: bool | None
    issues: list[Issue]


@with_config(_CONFIG)
class ContextResult(TypedDict):
    chats: dict[ChatID, ChatSummary]
    anchor: Message | None
    before: list[Message]
    after: list[Message]
    reply_chain: list[Message]
    issues: list[Issue]


@with_config(_CONFIG)
class ExportFile(TypedDict):
    format: changes.ExportFormat
    name: str
    mime_type: str
    size: Count
    sha256: str
    local_path: str
    resource_uri: str
    expires_at: AwareDatetime
    saved_file: NotRequired[SavedFile]


@with_config(_CONFIG)
class Change(TypedDict):
    kind: Literal["new", "updated", "unavailable"]
    message_ref: MessageKey
    revision: str
    observed_at: AwareDatetime
    message: NotRequired[Message]


@with_config(_CONFIG)
class ChangesResult(TypedDict):
    chats: dict[ChatID, ChatSummary]
    chat_id: ChatID
    baseline_created: bool
    changes: list[Change]
    returned_count: Count
    omitted_count: Count
    complete: bool
    upper_message_id: NotRequired[Count]
    recheck_limit: Literal[100]
    rechecked_message_ids: list[int]
    tracked_count: Annotated[int, Field(ge=0, le=100)]
    next_checkpoint: str | None
    checkpoint_expires_at: NotRequired[AwareDatetime]
    issues: list[Issue]
    export: NotRequired[ExportFile]


@with_config(_CONFIG)
class ExportHistoryResult(TypedDict):
    chat_id: ChatID
    exported_count: Count
    omitted_count: Count
    complete: bool
    upper_message_id: NotRequired[Count]
    date_from: NotRequired[AwareDatetime]
    date_to: AwareDatetime
    next_checkpoint: str | None
    checkpoint_expires_at: NotRequired[AwareDatetime]
    issues: list[Issue]
    export: NotRequired[ExportFile]


@with_config(_CONFIG)
class WatchInfo(TypedDict):
    name: str
    selector: Literal["chats", "folders"]
    started_at: AwareDatetime
    source_count: Count
    folder_ids: list[int]
    completed_at: NotRequired[AwareDatetime]
    pending_batch_id: NotRequired[str]
    pending_change_count: Count
    issue_count: Count


@with_config(_CONFIG)
class WatchList(TypedDict):
    watches: list[WatchInfo]


@with_config(_CONFIG)
class WatchProgress(TypedDict):
    source_count: Count
    pending_source_count: Count
    sources_this_call: Count
    rpc_pages_this_call: Count


@with_config(_CONFIG)
class WatchChangesResult(TypedDict):
    chats: dict[ChatID, ChatSummary]
    name: str
    batch_id: str | None
    changes: list[Change]
    returned_count: Count
    omitted_count: Count
    complete: bool
    progress: WatchProgress
    issues: Annotated[list[Issue], Field(max_length=20)]
    issue_count: Count


@with_config(_CONFIG)
class WatchAcknowledgement(TypedDict):
    name: str
    batch_id: str
    acknowledged: Literal[True]


@with_config(_CONFIG)
class WatchDeletion(TypedDict):
    name: str
    deleted: bool


# These are protocol controls, not unknown optional metadata. Their null values
# distinguish exhaustion, unavailable anchors and unverified authorization.
_NULL_CONTROLS = {
    "authorized",
    "next_cursor",
    "next_checkpoint",
    "next_text_offset",
    "anchor",
    "before_exhausted",
    "after_exhausted",
    "batch_id",
}
_EMPTY_METADATA = {"entities", "attachments"}


def _project(value: Any, chats: dict[str, Any]) -> Any:
    if isinstance(value, models.MessageRef):
        return value.key
    if isinstance(value, models.Scope):
        return {
            "folder_ids": value.folder_ids,
            "chat_ids": value.chat_ids[:20],
            "chat_count": len(value.chat_ids),
            "chat_ids_truncated": len(value.chat_ids) > 20,
            "resolved_at": value.resolved_at,
            "pending_chat_ids": value.pending_chat_ids[:20],
            "pending_chat_count": len(value.pending_chat_ids),
            "pending_chat_ids_truncated": len(value.pending_chat_ids) > 20,
            "complete": value.complete,
        }
    if isinstance(value, models.Model):
        data: dict[str, Any] = {}
        if isinstance(value, models.Message):
            # One response snapshot per canonical chat ID; message references carry
            # the key. Forwarded sources are not fetched or added to this map.
            summary = {"title": value.chat.title, "type": value.chat.type}
            if value.chat.username is not None:
                summary["username"] = value.chat.username
            chats[value.chat.id] = summary
        for name in type(value).model_fields:
            item = getattr(value, name)
            if isinstance(value, models.Message) and name == "chat":
                continue
            if item is None and name not in _NULL_CONTROLS:
                continue
            if name in _EMPTY_METADATA and item == []:
                continue
            if isinstance(item, models.Sender) and item == models.Sender():
                continue
            data[name] = _project(item, chats)
        return data
    if isinstance(value, dict):
        return {
            key: _project(item, chats)
            for key, item in value.items()
            if item is not None or key in _NULL_CONTROLS
        }
    if isinstance(value, list):
        return [_project(item, chats) for item in value]
    return value


def encode(value: models.Model | dict[str, object] | list[models.Folder]) -> CallToolResult:
    """Return identical compact JSON in structured and text MCP content.

    Validation uses the public SDK's Annotated[CallToolResult, DTO] contract; it
    never reserializes the payload or fills in omitted optional fields.
    """
    chats: dict[str, Any] = {}
    payload = _project(value, chats)
    if isinstance(
        value,
        (
            models.Page,
            models.MessageResult,
            models.AlbumResult,
            models.ContextResult,
            changes.ChangesResult,
            watches.WatchChangesResult,
        ),
    ):
        payload = {"chats": chats, **payload}
    elif isinstance(value, list):
        payload = {"folders": payload}
    # Domain values can contain datetime objects. Pydantic's public JSON encoder
    # preserves the established UTC spelling and Unicode without a second model.
    text = to_json(payload).decode("utf-8")
    result = CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=json.loads(text),
    )
    if isinstance(value, models.DownloadResult):
        result.content.append(
            ResourceLink(
                type="resource_link",
                uri=value.resource_uri,
                name=value.name,
                mime_type=value.mime_type,
                size=value.size,
                description="Temporary attachment; read before expires_at from the tool result.",
            )
        )
    if (
        isinstance(value, (changes.ChangesResult, changes.ExportHistoryResult))
        and value.export is not None
    ):
        exported = value.export
        result.content.append(
            ResourceLink(
                type="resource_link",
                uri=exported.resource_uri,
                name=exported.name,
                mime_type=exported.mime_type,
                size=exported.size,
                description="Temporary source-linked export; read before expires_at.",
            )
        )
    return result
