"""Messages and their metadata, pages, albums and context."""

from datetime import datetime
from typing import Annotated, Literal, NotRequired

from pydantic import AwareDatetime, Field, with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.common import (
    CONFIG,
    ChatID,
    ChatSummary,
    Count,
    Issue,
    MessageKey,
    Scope,
    TopicID,
)
from telegram_mcp.domain import models


@with_config(CONFIG)
class Sender(TypedDict):
    id: NotRequired[str]
    name: NotRequired[str]
    type: Literal["user", "chat", "unknown"]


@with_config(CONFIG)
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


@with_config(CONFIG)
class FormattedText(TypedDict):
    text: str
    entities: NotRequired[list[Entity]]


@with_config(CONFIG)
class ReactionCount(TypedDict):
    type: Literal["emoji", "custom_emoji", "paid", "empty", "unknown"]
    emoji: NotRequired[str]
    document_id: NotRequired[str]
    count: Count
    is_chosen: NotRequired[bool]
    chosen_order: NotRequired[Count]


@with_config(CONFIG)
class ReactionSummary(TypedDict):
    is_min: bool
    reactions_as_tags: bool
    results: list[ReactionCount]


@with_config(CONFIG)
class PollOption(TypedDict):
    option_id: str
    text: FormattedText


@with_config(CONFIG)
class PollAnswerResult(TypedDict):
    option_id: str
    voter_count: NotRequired[Count]
    is_chosen: NotRequired[bool]
    is_correct: NotRequired[bool]


@with_config(CONFIG)
class PollResults(TypedDict):
    is_min: bool
    total_voters: NotRequired[Count]
    answers: NotRequired[list[PollAnswerResult]]
    solution: NotRequired[FormattedText]


@with_config(CONFIG)
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


@with_config(CONFIG)
class Attachment(TypedDict):
    id: str
    type: models.ContentType
    name: NotRequired[str]
    mime_type: NotRequired[str]
    size: NotRequired[int]


@with_config(CONFIG)
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


@with_config(CONFIG)
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


@with_config(CONFIG)
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


@with_config(CONFIG)
class SearchProgress(TypedDict):
    sources_total: Count
    sources_started: Count
    sources_ready: Count
    sources_exhausted: Count
    rpc_pages_this_call: Count


@with_config(CONFIG)
class TraversalBudget(TypedDict):
    max_sources: Annotated[int, Field(ge=1, le=1000)]
    max_messages: Annotated[int, Field(ge=1, le=1000)]
    emitted_messages: Count
    exhausted: bool


@with_config(CONFIG)
class MessageResult(TypedDict):
    chats: dict[ChatID, ChatSummary]
    messages: list[Message]
    issues: list[Issue]


@with_config(CONFIG)
class Page(MessageResult):
    next_cursor: str | None
    scope: Scope
    order: Literal["newest_first", "oldest_first"]
    ordering: Literal["page", "global"]
    thread_root: NotRequired[MessageKey]
    topic_id: NotRequired[TopicID]
    progress: NotRequired[SearchProgress]
    budget: NotRequired[TraversalBudget]


@with_config(CONFIG)
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


@with_config(CONFIG)
class ContextResult(TypedDict):
    chats: dict[ChatID, ChatSummary]
    anchor: Message | None
    before: list[Message]
    after: list[Message]
    reply_chain: list[Message]
    issues: list[Issue]
