"""User-selected files, optimistic drafts and durable outbound operation contracts."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import AwareDatetime, Field

from telegram_mcp.domain.models import Issue, MessageRef, Model, ToolError

OperationState = Literal["pending", "sent", "failed", "unknown"]


@dataclass(frozen=True)
class WritePolicy:
    allow_drafts: bool = False
    allow_send_messages: bool = False
    allow_send_files: bool = False
    allow_bot_callbacks: bool = False
    allowed_write_chat_ids: tuple[str, ...] = ()

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


class OutboundOperation(Model):
    operation_id: str
    account_id: str
    idempotency_key: str
    kind: Literal["message", "file", "callback"]
    request_hash: str
    payload: dict = Field(default_factory=dict)
    chat_id: str
    random_id: int
    confirmed: bool = False
    dispatched: bool = False
    receipt: SendReceipt | CallbackReceipt


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
