"""User-selected files, optimistic drafts and durable outbound operation contracts."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import AwareDatetime, Field

from telegram_mcp.domain.models import ChatType, ContentType, Issue, MessageRef, Model, ToolError

OperationState = Literal["pending", "sent", "failed", "unknown"]
OperationKind = Literal["message", "file", "callback"]
JournalState = Literal["pending", "sent", "completed", "failed", "unknown"]
WriteGrant = Literal["allowlist", "one_off", "all_chats"]


@dataclass(frozen=True)
class WritePolicy:
    allow_drafts: bool = False
    allow_send_messages: bool = False
    allow_send_files: bool = False
    allow_bot_callbacks: bool = False
    allowed_write_chat_ids: tuple[str, ...] = ()
    allow_all_write_chats: bool = False
    allow_one_off_sends: bool = False
    require_send_message_confirmation: bool = True
    require_send_file_confirmation: bool = True
    require_bot_callback_confirmation: bool = True
    require_draft_overwrite_confirmation: bool = True

    def requires_confirmation(self, kind: str, grant: WriteGrant = "allowlist") -> bool:
        if grant == "one_off":
            return True
        return {
            "message": self.require_send_message_confirmation,
            "file": self.require_send_file_confirmation,
            "callback": self.require_bot_callback_confirmation,
        }[kind]

    @property
    def confirmation_required(self) -> bool:
        one_off = self.allow_one_off_sends and not self.allow_all_write_chats
        return any(
            (
                self.allow_send_messages and (self.require_send_message_confirmation or one_off),
                self.allow_send_files and (self.require_send_file_confirmation or one_off),
                self.allow_bot_callbacks and self.require_bot_callback_confirmation,
                self.allow_drafts and self.require_draft_overwrite_confirmation,
            )
        )

    @property
    def enabled(self) -> bool:
        return any(
            (
                self.allow_drafts,
                self.allow_send_messages,
                self.allow_send_files,
                self.allow_bot_callbacks,
            )
        )


class RegisteredFile(Model):
    file_id: str
    name: str
    mime_type: str
    size: int = Field(ge=0, le=20 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: AwareDatetime
    attachment_id: str | None = None
    source_identity: str | None = None


class FileList(Model):
    files: list[RegisteredFile]


class Draft(Model):
    chat_id: str
    text: str = ""
    file: RegisteredFile | None = None
    has_media: bool = False
    media_identity: str | None = None
    revision: str
    updated_at: AwareDatetime | None = None
    exists: bool = False
    owned: bool = False
    supported: bool = True


class DraftList(Model):
    drafts: list[Draft]
    next_cursor: str | None = None
    issues: list[Issue] = Field(default_factory=list)


class SendRequest(Model):
    chat_ref: str
    text: str = ""
    file_id: str | None = None
    reply_to: MessageRef | None = None


class SendReceipt(Model):
    operation_id: str
    status: OperationState
    chat_id: str
    message_ref: MessageRef | None = None
    sent_at: AwareDatetime | None = None
    file: RegisteredFile | None = None
    error: Issue | None = None
    retry_at: AwareDatetime | None = None


class CallbackReceipt(Model):
    operation_id: str
    status: Literal["pending", "completed", "failed", "unknown"]
    message: str | None = None
    alert: bool = False
    url: str | None = None
    observed_message_refs: list[MessageRef] = Field(default_factory=list, max_length=20)
    observation_complete: Literal[False] = False
    error: Issue | None = None


class RecipientBinding(Model):
    """The selected peer and permission basis survive aliases and payload cleanup."""

    chat_id: str
    title: str
    type: ChatType
    username: str | None = None
    selector: str
    grant: WriteGrant


class RecipientPermission(Model):
    allowed: bool
    grant: WriteGrant | None = None
    confirmation_required: bool | None = None
    error: Issue | None = None


class RecipientResolution(Model):
    chat_id: str
    title: str
    type: ChatType
    username: str | None = None
    send_message: RecipientPermission
    send_file: RecipientPermission
    confirmation_supported: bool | None = None
    retry_after_seconds: int = 0
    resolved_at: AwareDatetime


class SentAttachment(Model):
    type: ContentType
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None


class SentMessage(Model):
    message_ref: MessageRef
    text: str
    text_truncated: bool = False
    outgoing: bool
    sent_at: AwareDatetime
    edited_at: AwareDatetime | None = None
    attachments: list[SentAttachment] = Field(default_factory=list)


class SendVerification(Model):
    receipt: SendReceipt
    verification: Literal["not_checked", "read_back", "verified", "mismatch", "unavailable"]
    checked_at: AwareDatetime | None = None
    message: SentMessage | None = None
    text_matches: bool | None = None
    attachment_metadata_matches: bool | None = None
    attachment_bytes_verified: Literal[False] = False
    issues: list[Issue] = Field(default_factory=list)


class OperationSummary(Model):
    operation_id: str
    idempotency_key: str
    kind: OperationKind
    status: JournalState
    chat_id: str
    recipient: RecipientBinding | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    message_ref: MessageRef | None = None
    sent_at: AwareDatetime | None = None
    error: Issue | None = None


class OperationPage(Model):
    scope: Literal["mcp_journal"] = "mcp_journal"
    operations: list[OperationSummary]
    next_cursor: str | None = None


class OutboundOperation(Model):
    operation_id: str
    account_id: str
    idempotency_key: str
    kind: OperationKind
    request_hash: str
    payload: dict = Field(default_factory=dict)
    chat_id: str
    random_id: int
    confirmed: bool = False
    confirmation_source: Literal["form", "config"] | None = None
    recipient: RecipientBinding | None = None
    reply_selector: str | None = None
    reply_ref: MessageRef | None = None
    dispatched: bool = False
    receipt: SendReceipt | CallbackReceipt
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    expected_text_sha256: str | None = None


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def check_text(text: str, *, file: bool = False) -> None:
    maximum = 1024 if file else 4096
    try:
        size = len(text.encode("utf-16-le")) // 2
    except UnicodeError:
        raise ToolError("INVALID_ARGUMENT", "Text contains invalid Unicode.") from None
    if size > maximum or (not file and not text.strip()):
        raise ToolError("INVALID_ARGUMENT", f"Use {'0' if file else '1'}..{maximum} UTF-16 units.")


def check_key(key: str) -> None:
    if not key.strip() or len(key) > 200:
        raise ToolError("INVALID_ARGUMENT", "idempotency_key must contain 1..200 characters.")
