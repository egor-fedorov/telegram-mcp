"""Compact typed extraction responses with original-document Unicode offsets."""

from typing import Annotated, Literal, NotRequired

from pydantic import AwareDatetime, Field, with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.common import CONFIG, Count, Issue, MessageKey


@with_config(CONFIG)
class PageSpan(TypedDict):
    page_number: Annotated[int, Field(ge=1, le=500)]
    start: Count
    end: Count


@with_config(CONFIG)
class ExtractionResult(TypedDict):
    extraction_id: str
    attachment_id: str
    message_ref: MessageKey
    name: str
    format: Literal["pdf", "docx"]
    source_sha256: str
    text: str
    text_offset: Count
    text_total_length: Count
    next_text_offset: Count | None
    page_count: NotRequired[Annotated[int, Field(ge=1)]]
    page_spans: list[PageSpan]
    expires_at: AwareDatetime
    issues: list[Issue]
