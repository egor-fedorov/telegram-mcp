"""Selected M6 checks that return counts/codes, never document content."""

from dataclasses import dataclass

from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import Issue, ToolError


@dataclass
class ExtractionCheck:
    returned_count: int
    issues: list[Issue]


async def check_extraction(service: Service, attachment_id: str) -> ExtractionCheck:
    first = await service.extract_attachment_text(attachment_id, text_limit=1)
    if first.text_total_length < 2 or first.next_text_offset != 1:
        raise ToolError(
            "VALIDATION_MISMATCH", "Select a document with at least two text characters."
        )
    second = await service.extract_attachment_text(
        extraction_id=first.extraction_id,
        text_offset=1,
        text_limit=128,
    )
    replay = await service.extract_attachment_text(
        extraction_id=first.extraction_id, text_limit=129
    )
    if (
        not (first.text + second.text).startswith(replay.text)
        or first.source_sha256 != second.source_sha256
        or first.expires_at != second.expires_at
        or first.message_ref != second.message_ref
        or first.text_total_length != second.text_total_length
    ):
        raise ToolError("VALIDATION_MISMATCH", "Extraction continuation or identity differs.")
    if first.format == "pdf" and (not first.page_count or not replay.page_spans):
        raise ToolError("VALIDATION_MISMATCH", "PDF page provenance is missing.")
    if first.format == "docx" and (first.page_count is not None or first.page_spans):
        raise ToolError("VALIDATION_MISMATCH", "DOCX invented physical page numbers.")
    return ExtractionCheck(len(replay.text), replay.issues)
