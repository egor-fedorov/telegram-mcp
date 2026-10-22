"""Typed read operations; invalid combinations are not a universal flag list."""

from dataclasses import dataclass, field
from datetime import datetime

from telegram_mcp.domain.models import ContentType


@dataclass(frozen=True)
class ReadFilters:
    date_from: datetime | None = None
    date_to: datetime | None = None
    content_type: ContentType | None = None
    oldest_first: bool = False
    has_link: bool | None = None


@dataclass(frozen=True)
class HistoryRead:
    filters: ReadFilters = field(default_factory=ReadFilters)


@dataclass(frozen=True)
class SearchRead:
    query: str
    filters: ReadFilters = field(default_factory=ReadFilters)
    sender_id: str | None = None


@dataclass(frozen=True)
class PinnedRead:
    filters: ReadFilters = field(default_factory=ReadFilters)


@dataclass(frozen=True)
class ThreadRead:
    thread_id: int
    filters: ReadFilters = field(default_factory=ReadFilters)


@dataclass(frozen=True)
class TopicRead:
    topic_id: int
    filters: ReadFilters = field(default_factory=ReadFilters)
    pinned_only: bool = False


type MessageRead = HistoryRead | SearchRead | PinnedRead | ThreadRead | TopicRead
