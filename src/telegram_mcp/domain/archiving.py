"""Provenance captured from an already authorized read, without additional lookups."""

from typing import Literal

from pydantic import AwareDatetime

from telegram_mcp.domain.models import MessageRef, Model


class ArchiveSource(Model):
    tool: Literal["download_attachment", "export_history", "get_changes", "get_watch_changes"]
    chat_id: str | None = None
    message_ref: MessageRef | None = None
    url: str | None = None
    title: str | None = None
    username: str | None = None
    format: Literal["jsonl", "markdown"] | None = None
    date_from: AwareDatetime | None = None
    date_to: AwareDatetime | None = None
    upper_message_id: int | None = None
    watch_name: str | None = None
    batch_id: str | None = None
    source_ids: list[str] | None = None
