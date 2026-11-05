"""Compact public outbound metadata: no bytes, callback payloads or local paths."""

from typing import Literal, NotRequired

from pydantic import AwareDatetime, with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.common import CONFIG, ChatID, Issue, MessageKey
from telegram_mcp.domain.models import ChatType, ContentType
from telegram_mcp.domain.outbound import JournalState, OperationKind, WriteGrant


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


@with_config(CONFIG)
class RecipientPermission(TypedDict):
    allowed: bool
    grant: NotRequired[WriteGrant]
    confirmation_required: bool | None
    error: NotRequired[Issue]


@with_config(CONFIG)
class RecipientResolution(TypedDict):
    chat_id: ChatID
    title: str
    type: ChatType
    username: NotRequired[str]
    send_message: RecipientPermission
    send_file: RecipientPermission
    confirmation_supported: bool | None
    retry_after_seconds: int
    resolved_at: AwareDatetime


@with_config(CONFIG)
class RecipientBinding(TypedDict):
    chat_id: ChatID
    title: str
    type: ChatType
    username: NotRequired[str]
    selector: str
    grant: WriteGrant


@with_config(CONFIG)
class SentAttachment(TypedDict):
    type: ContentType
    name: NotRequired[str]
    mime_type: NotRequired[str]
    size: NotRequired[int]


@with_config(CONFIG)
class SentMessage(TypedDict):
    message_ref: MessageKey
    text: str
    text_truncated: bool
    outgoing: bool
    sent_at: AwareDatetime
    edited_at: NotRequired[AwareDatetime]
    attachments: NotRequired[list[SentAttachment]]


@with_config(CONFIG)
class SendVerification(TypedDict):
    receipt: SendReceipt
    verification: Literal["not_checked", "read_back", "verified", "mismatch", "unavailable"]
    checked_at: NotRequired[AwareDatetime]
    message: NotRequired[SentMessage]
    text_matches: bool | None
    attachment_metadata_matches: bool | None
    attachment_bytes_verified: Literal[False]
    issues: list[Issue]


@with_config(CONFIG)
class OperationSummary(TypedDict):
    operation_id: str
    idempotency_key: str
    kind: OperationKind
    status: JournalState
    chat_id: ChatID
    recipient: NotRequired[RecipientBinding]
    created_at: NotRequired[AwareDatetime]
    updated_at: NotRequired[AwareDatetime]
    message_ref: NotRequired[MessageKey]
    sent_at: NotRequired[AwareDatetime]
    error: NotRequired[Issue]


@with_config(CONFIG)
class OperationPage(TypedDict):
    scope: Literal["mcp_journal"]
    operations: list[OperationSummary]
    next_cursor: str | None
