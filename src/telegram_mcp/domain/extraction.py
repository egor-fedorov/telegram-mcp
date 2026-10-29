"""Immutable extracted text and Unicode-offset pagination, independent of parsers."""

from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from telegram_mcp.domain.models import Issue, MessageRef, Model, ToolError

MAX_TEXT_CHARACTERS = 1_000_000
MAX_PDF_PAGES = 500
MAX_PAGE_SPANS = 50
MAX_TEXT_LIMIT = 24_000

DocumentFormat = Literal["pdf", "docx"]


class PageSpan(Model):
    page_number: int = Field(ge=1, le=MAX_PDF_PAGES)
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class ExtractedDocument(Model):
    format: DocumentFormat
    text: str = Field(max_length=MAX_TEXT_CHARACTERS)
    page_count: int | None = Field(default=None, ge=1, le=MAX_PDF_PAGES)
    page_spans: list[PageSpan] = Field(default_factory=list, max_length=MAX_PDF_PAGES)
    issues: list[Issue] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def consistent_pages(self) -> Self:
        if not self.text.strip():
            raise ValueError("An extraction must contain text")
        if self.format == "docx":
            if self.page_count is not None or self.page_spans:
                raise ValueError("DOCX has no physical page mapping")
            return self
        if self.page_count != len(self.page_spans):
            raise ValueError("PDF page mapping must cover every page")
        offset = 0
        for number, span in enumerate(self.page_spans, 1):
            if span.page_number != number or span.start != offset or span.end < span.start:
                raise ValueError("PDF page mapping is not contiguous")
            offset = span.end
        if offset != len(self.text):
            raise ValueError("PDF page mapping does not cover the extracted text")
        return self


class ExtractionResult(Model):
    extraction_id: str
    attachment_id: str
    message_ref: MessageRef
    name: str
    format: DocumentFormat
    source_sha256: str
    text: str
    text_offset: int = Field(ge=0)
    text_total_length: int = Field(ge=0)
    next_text_offset: int | None = Field(default=None, ge=0)
    page_count: int | None = Field(default=None, ge=1)
    page_spans: list[PageSpan] = Field(default_factory=list)
    expires_at: AwareDatetime
    issues: list[Issue] = Field(default_factory=list)


def text_slice(
    document: ExtractedDocument, offset: int, limit: int
) -> tuple[str, int | None, list[PageSpan]]:
    """Limit both text and page metadata without losing a continuation position."""
    if offset < 0 or offset > len(document.text) or not 1 <= limit <= MAX_TEXT_LIMIT:
        raise ToolError("INVALID_ARGUMENT", "Invalid extraction text_offset or text_limit.")
    end = min(offset + limit, len(document.text))
    spans = [span for span in document.page_spans if span.end > offset and span.start < end]
    if len(spans) > MAX_PAGE_SPANS:
        end = spans[MAX_PAGE_SPANS - 1].end
        spans = spans[:MAX_PAGE_SPANS]
    return (
        document.text[offset:end],
        end if end < len(document.text) else None,
        [
            span.model_copy(update={"start": max(offset, span.start), "end": min(end, span.end)})
            for span in spans
        ],
    )
