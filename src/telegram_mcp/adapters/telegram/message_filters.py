"""Local predicates shared by native search and topic-history readers."""

from telegram_mcp.application.requests import (
    ReadFilters,
)
from telegram_mcp.domain.models import Message


def matches_filters(message: Message, filters: ReadFilters) -> bool:
    return (
        (filters.content_type is None or message.content_type == filters.content_type)
        and (filters.has_link is None or message.has_link is filters.has_link)
        and (filters.date_from is None or message.sent_at >= filters.date_from)
        and (filters.date_to is None or message.sent_at < filters.date_to)
    )
