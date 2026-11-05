"""Recipient previews, account journal queries and exact sent-message verification."""

import math
from dataclasses import dataclass

from telegram_mcp.application.common import check_limit, fingerprint
from telegram_mcp.application.outbound_access import OutboundContext
from telegram_mcp.application.outbound_journal import OperationJournal
from telegram_mcp.application.outbound_ports import OperationFilter
from telegram_mcp.application.ports import CursorPort, MessagePort
from telegram_mcp.domain.models import Chat, Message, ToolError
from telegram_mcp.domain.outbound import (
    OperationPage,
    OperationSummary,
    OutboundOperation,
    RecipientPermission,
    RecipientResolution,
    SendReceipt,
    SendVerification,
    SentAttachment,
    SentMessage,
    text_digest,
)
from telegram_mcp.domain.policy import SERVICE_NOTIFICATIONS_ID
from telegram_mcp.domain.references import is_canonical_chat_id


@dataclass(frozen=True)
class JournalCursor:
    before: int


class OutboundInspection:
    def __init__(
        self,
        context: OutboundContext,
        journal: OperationJournal,
        messages: MessagePort,
        cursors: CursorPort,
    ):
        self.context, self.journal, self.messages, self.cursors = (
            context,
            journal,
            messages,
            cursors,
        )

    async def resolve(self, chat_ref: str) -> RecipientResolution:
        account = await self.context.backend.account_id()
        chat = await self.context.chats.resolve_chat(chat_ref, refresh=True)
        if chat.id == SERVICE_NOTIFICATIONS_ID or not is_canonical_chat_id(chat.id):
            raise ToolError("ACCESS_DENIED", "Telegram service notifications are inaccessible.")
        message = await self._permission(chat, "message")
        file = await self._permission(chat, "file")
        now = self.context.clock.utc_now()
        waits = await self.context.store.cooldowns(account, now)
        retry = max(
            (
                (until - now).total_seconds()
                for scope, until in waits.items()
                if scope in ("account", f"send:{chat.id}")
            ),
            default=0,
        )
        return RecipientResolution(
            chat_id=chat.id,
            title=chat.title,
            type=chat.type,
            username=chat.username,
            send_message=message,
            send_file=file,
            retry_after_seconds=max(0, math.ceil(retry)),
            resolved_at=now,
        )

    async def _permission(self, chat: Chat, kind: str) -> RecipientPermission:
        try:
            grant = await self.context.authorize(chat, self.journal.capability(kind))
        except ToolError as exc:
            if exc.code not in (
                "WRITES_DISABLED",
                "WRITE_ACCESS_DENIED",
                "ACCESS_DENIED",
                "WRITE_UNSUPPORTED",
            ):
                raise
            return RecipientPermission(allowed=False, error=exc.issue(chat_id=chat.id))
        return RecipientPermission(
            allowed=True,
            grant=grant,
            confirmation_required=self.context.policies().requires_confirmation(kind, grant),
        )

    async def list(
        self, filters: OperationFilter, limit: int = 30, cursor: str | None = None
    ) -> OperationPage:
        check_limit(limit)
        self._check_filters(filters)
        account = await self.context.backend.account_id()
        fp = fingerprint(
            [
                account,
                filters.chat_id,
                filters.kind,
                filters.status,
                filters.date_from,
                filters.date_to,
            ]
        )
        before = None
        if cursor is not None:
            state = self.cursors.get(cursor, "operations", fp)
            if not isinstance(state, JournalCursor):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
            before = state.before
        batch = await self.context.store.operations(
            account, filters, before=before, limit=limit, active_ids=self.journal.active_ids
        )
        token = (
            self.cursors.put("operations", fp, JournalCursor(batch.before))
            if batch.before is not None
            else None
        )
        return OperationPage(
            operations=[self._summary(op) for op in batch.operations], next_cursor=token
        )

    @staticmethod
    def _check_filters(filters: OperationFilter) -> None:
        if filters.chat_id is not None and not is_canonical_chat_id(filters.chat_id):
            raise ToolError("INVALID_ARGUMENT", "chat_id must be a canonical numeric ID.")
        if filters.kind not in (None, "message", "file", "callback") or filters.status not in (
            None,
            "pending",
            "sent",
            "completed",
            "failed",
            "unknown",
        ):
            raise ToolError("INVALID_ARGUMENT", "Unknown operation kind or status.")
        for date in (filters.date_from, filters.date_to):
            if date is not None and date.utcoffset() is None:
                raise ToolError("INVALID_ARGUMENT", "Operation dates require a timezone.")
        if filters.date_from and filters.date_to and filters.date_from >= filters.date_to:
            raise ToolError("INVALID_ARGUMENT", "date_from must precede date_to.")

    def _summary(self, operation: OutboundOperation) -> OperationSummary:
        receipt = self.journal.visible_receipt(operation)
        return OperationSummary(
            operation_id=operation.operation_id,
            idempotency_key=operation.idempotency_key,
            kind=operation.kind,
            status=receipt.status,
            chat_id=operation.chat_id,
            recipient=operation.recipient,
            created_at=operation.created_at,
            updated_at=operation.updated_at,
            error=receipt.error,
            message_ref=receipt.message_ref if isinstance(receipt, SendReceipt) else None,
            sent_at=receipt.sent_at if isinstance(receipt, SendReceipt) else None,
        )

    async def verify(self, operation_id: str) -> SendVerification:
        operation = await self.journal.lookup(operation_id)
        receipt = self.journal.visible_receipt(operation)
        if operation.kind == "callback" or not isinstance(receipt, SendReceipt):
            raise ToolError("INVALID_ARGUMENT", "Only message/file sends can be verified.")
        result = SendVerification(receipt=receipt, verification="not_checked")
        if receipt.status != "sent" or receipt.message_ref is None:
            return result
        ref = receipt.message_ref
        if (
            ref.chat_id != operation.chat_id
            or receipt.chat_id != operation.chat_id
            or operation.chat_id == SERVICE_NOTIFICATIONS_ID
            or not is_canonical_chat_id(operation.chat_id)
        ):
            raise ToolError("OPERATION_UNAVAILABLE", "Stored message identity is inconsistent.")
        result.checked_at = self.context.clock.utc_now()
        try:
            message = await self.messages.message(ref)
            if await self.context.backend.account_id() != operation.account_id:
                raise ToolError("ACCOUNT_CHANGED", "Account changed during verification.")
            if message is not None:
                message = Message.model_validate(message.model_dump())
            self._check_message(message, operation)
        except ToolError as exc:
            result.verification = "unavailable"
            result.issues.append(exc.issue(ref=ref))
            return result
        assert message is not None
        result.message = SentMessage(
            message_ref=ref,
            text=message.text[:12000],
            text_truncated=len(message.text) > 12000,
            outgoing=True,
            sent_at=message.sent_at,
            edited_at=message.edited_at,
            attachments=[
                SentAttachment(type=a.type, name=a.name, mime_type=a.mime_type, size=a.size)
                for a in message.attachments
            ],
        )
        result.text_matches = (
            text_digest(message.text) == operation.expected_text_sha256
            if operation.expected_text_sha256 is not None
            else None
        )
        result.attachment_metadata_matches = (
            None
            if operation.kind == "file" and receipt.file is None
            else self._attachments_match(message, receipt)
        )
        comparisons = (result.text_matches, result.attachment_metadata_matches)
        result.verification = (
            "mismatch"
            if False in comparisons
            else "read_back"
            if None in comparisons
            else "verified"
        )
        return result

    @staticmethod
    def _check_message(message: Message | None, operation: OutboundOperation) -> None:
        if message is None:
            raise ToolError("MESSAGE_UNAVAILABLE", "The sent message could not be retrieved.")
        receipt = operation.receipt
        assert isinstance(receipt, SendReceipt)
        if message.message_ref != receipt.message_ref or message.chat.id != operation.chat_id:
            raise ToolError(
                "MESSAGE_UNAVAILABLE", "Telegram returned a different message identity."
            )
        if message.omission_reason:
            raise ToolError(message.omission_reason, "Authentication notifications are filtered.")
        if message.outgoing is not True:
            raise ToolError("MESSAGE_UNAVAILABLE", "The message is not confirmed as outgoing.")

    @staticmethod
    def _attachments_match(message: Message, receipt: SendReceipt) -> bool | None:
        if receipt.file is None:
            return not message.attachments
        if len(message.attachments) != 1:
            return False
        observed, expected = message.attachments[0], receipt.file
        pairs = (
            (observed.name, expected.name),
            (observed.size, expected.size),
            (observed.mime_type, expected.mime_type),
        )
        if any(actual is not None and actual != wanted for actual, wanted in pairs):
            return False
        return True if all(actual is not None for actual, _ in pairs) else None
