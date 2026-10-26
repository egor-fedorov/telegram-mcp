"""Typed read operations; invalid combinations are not a universal flag list."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

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
    ordering: Literal["page", "global"] = "global"


@dataclass(frozen=True)
class SearchRead:
    query: str
    filters: ReadFilters = field(default_factory=ReadFilters)
    sender_id: str | None = None
    ordering: Literal["page", "global"] = "page"


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


DEFAULT_MAX_SOURCES = 20
DEFAULT_MAX_MESSAGES = 100


@dataclass(frozen=True)
class BudgetRequest:
    """Traversal limits; max_messages=None keeps a cursor's value or uses the default."""

    max_sources: int
    max_messages: int | None = None
