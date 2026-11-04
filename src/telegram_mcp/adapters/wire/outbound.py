"""Compact public outbound metadata: no bytes, callback payloads or local paths."""

from typing import Literal, NotRequired

from pydantic import AwareDatetime, with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.common import CONFIG, ChatID, Issue, MessageKey


@with_config(CONFIG)
class RegisteredFile(TypedDict):
    file_id: str
    name: str
    mime_type: str
    size: int
    sha256: str
    created_at: AwareDatetime
    attachment_id: NotRequired[str]
    source_identity: NotRequired[str]


@with_config(CONFIG)
class FileList(TypedDict):
    files: list[RegisteredFile]


@with_config(CONFIG)
class Draft(TypedDict):
    chat_id: ChatID
    text: str
    file: NotRequired[RegisteredFile]
    has_media: bool
    media_identity: NotRequired[str]
    revision: str
    updated_at: NotRequired[AwareDatetime]
    exists: bool
    owned: bool
    supported: bool


@with_config(CONFIG)
class DraftList(TypedDict):
    drafts: list[Draft]
    next_cursor: str | None
    issues: list[Issue]


@with_config(CONFIG)
class SendReceipt(TypedDict):
    operation_id: str
    status: Literal["pending", "sent", "failed", "unknown"]
    chat_id: ChatID
    message_ref: NotRequired[MessageKey]
    sent_at: NotRequired[AwareDatetime]
    file: NotRequired[RegisteredFile]
    error: NotRequired[Issue]
    retry_at: NotRequired[AwareDatetime]


@with_config(CONFIG)
class CallbackReceipt(TypedDict):
    operation_id: str
    status: Literal["pending", "completed", "failed", "unknown"]
    message: NotRequired[str]
    alert: bool
    url: NotRequired[str]
    observed_message_refs: list[MessageKey]
    observation_complete: Literal[False]
    error: NotRequired[Issue]
