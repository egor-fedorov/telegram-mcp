"""Preserve original message metadata while bounding returned text."""

from telegram_mcp.domain.models import Message


def clip(message: Message, maximum: int = 12000, offset: int = 0) -> Message:
    total = len(message.text)
    end = min(offset + maximum, total)
    return message.model_copy(
        update={
            "text": message.text[offset:end],
            "text_offset": offset,
            "text_total_length": total,
            "next_text_offset": end if end < total else None,
        }
    )
