"""Persist intent before dispatch and never guess whether a remote write succeeded."""

import asyncio
import math
import secrets
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Literal

from telegram_mcp.application.outbound_access import OutboundContext, require_confirmation
from telegram_mcp.application.outbound_ports import Confirm, ConfirmationPending
from telegram_mcp.domain.models import MessageRef, ToolError
from telegram_mcp.domain.outbound import (
    CallbackReceipt,
    OutboundOperation,
    RecipientBinding,
    SendReceipt,
    check_key,
    request_digest,
    text_digest,
)

Receipt = SendReceipt | CallbackReceipt


class OperationJournal:
    def __init__(self, context: OutboundContext):
        self.context = context
        self._lock = asyncio.Lock()
        self._active: set[str] = set()

    async def lookup(
        self, operation_id: str | None = None, idempotency_key: str | None = None
    ) -> OutboundOperation:
        if (operation_id is None) == (idempotency_key is None):
            raise ToolError("INVALID_ARGUMENT", "Supply operation_id XOR idempotency_key.")
        if idempotency_key is not None:
            check_key(idempotency_key)
        account = await self.context.backend.account_id()
        if operation_id is not None:
            return await self.context.store.lookup_operation(account, operation_id)
        assert idempotency_key is not None
        operation = await self.context.store.operation(account, idempotency_key)
        if operation is None:
            raise ToolError("OPERATION_UNAVAILABLE", "Unknown operation for this account.")
        return operation

    async def status(
        self, operation_id: str | None = None, idempotency_key: str | None = None
    ) -> Receipt:
        return self.visible_receipt(await self.lookup(operation_id, idempotency_key))

    def visible_receipt(self, operation: OutboundOperation) -> Receipt:
        if (
            operation.receipt.status == "pending"
            and operation.dispatched
            and operation.operation_id not in self._active
        ):
            return operation.receipt.model_copy(update={"status": "unknown"})
        return operation.receipt

    @property
    def active_ids(self) -> tuple[str, ...]:
        return tuple(self._active)

    async def _save(self, operation: OutboundOperation) -> None:
        operation.updated_at = self.context.clock.utc_now()
        await self.context.store.save_operation(operation)

    async def authorize(self, operation: OutboundOperation) -> None:
        capability = self.capability(operation.kind)
        if operation.recipient is not None:
            if operation.recipient.chat_id != operation.chat_id:
                raise ToolError("WRITE_ACCESS_DENIED", "Stored recipient identity is inconsistent.")
            await self.context.revalidate(operation.recipient, capability)
        else:
            await self.context.recipient(
                operation.chat_id, capability, bot=operation.kind == "callback", grant="allowlist"
            )

    async def cooldowns(self) -> list[dict[str, object]]:
        account = await self.context.backend.account_id()
        now = self.context.clock.utc_now()
        return [
            {"scope": scope, "retry_after_seconds": math.ceil((until - now).total_seconds())}
            for scope, until in (await self.context.store.cooldowns(account, now)).items()
        ]

    @staticmethod
    def capability(kind: str) -> str:
        return {
            "message": "allow_send_messages",
            "file": "allow_send_files",
            "callback": "allow_bot_callbacks",
        }[kind]

    @staticmethod
    def digest(kind: str, chat: str, identity: dict) -> str:
        return request_digest({"kind": kind, "chat": chat, "request": identity})

    async def replay(
        self, account: str, chat: str, kind: str, key: str, identity: dict
    ) -> Receipt | None:
        check_key(key)
        operation = await self.context.store.operation(account, key)
        if operation is None:
            return None
        return self._replay(operation, self.digest(kind, chat, identity))

    def _replay(self, operation: OutboundOperation, digest: str) -> Receipt | None:
        if operation.request_hash != digest:
            raise ToolError(
                "IDEMPOTENCY_CONFLICT", "This idempotency_key belongs to a different request."
            )
        if (
            operation.receipt.status in ("sent", "completed", "failed")
            or operation.operation_id in self._active
        ):
            return operation.receipt
        if operation.kind == "callback" and operation.dispatched:
            return operation.receipt.model_copy(update={"status": "unknown"})
        if isinstance(operation.receipt, SendReceipt) and operation.receipt.retry_at:
            if operation.receipt.retry_at > self.context.clock.utc_now():
                return operation.receipt
        return None

    async def run(
        self,
        *,
        account: str,
        chat: str,
        kind: Literal["message", "file", "callback"],
        key: str,
        payload: dict,
        identity: dict,
        description: str | Callable[[OutboundOperation], str],
        confirm: Confirm | None,
        validate: Callable[[OutboundOperation], Awaitable[None]],
        dispatch: Callable[[OutboundOperation], Awaitable[Receipt]],
        recipient: RecipientBinding | None = None,
        reply_selector: str | None = None,
        reply_ref: MessageRef | None = None,
    ) -> Receipt:
        check_key(key)
        digest = self.digest(kind, chat, identity)
        async with self._lock:
            operation_id = secrets.token_urlsafe(32)
            receipt = (
                CallbackReceipt(operation_id=operation_id, status="pending")
                if kind == "callback"
                else SendReceipt(
                    operation_id=operation_id,
                    status="pending",
                    chat_id=chat,
                    file=payload.get("file"),
                )
            )
            operation = await self.context.store.create_operation(
                OutboundOperation(
                    operation_id=operation_id,
                    account_id=account,
                    idempotency_key=key,
                    kind=kind,
                    request_hash=digest,
                    payload=payload,
                    chat_id=chat,
                    random_id=secrets.randbits(63) or 1,
                    receipt=receipt,
                    recipient=recipient,
                    reply_selector=reply_selector,
                    reply_ref=reply_ref,
                    created_at=self.context.clock.utc_now(),
                    updated_at=self.context.clock.utc_now(),
                    expected_text_sha256=(
                        text_digest(payload["text"]) if kind in ("message", "file") else None
                    ),
                )
            )
            replay = self._replay(operation, digest)
            if replay is not None:
                return replay
            self._active.add(operation.operation_id)
        try:
            text = description if isinstance(description, str) else description(operation)
            return await self._execute(operation, text, confirm, validate, dispatch)
        finally:
            self._active.discard(operation.operation_id)

    async def _execute(
        self,
        operation: OutboundOperation,
        description: str,
        confirm: Confirm | None,
        validate: Callable[[OutboundOperation], Awaitable[None]],
        dispatch: Callable[[OutboundOperation], Awaitable[Receipt]],
    ) -> Receipt:
        was_dispatched = operation.dispatched
        try:
            scope = "callback" if operation.kind == "callback" else "send"
            await self.context.check_cooldown(operation.account_id, f"{scope}:{operation.chat_id}")
            await self.authorize(operation)
            await validate(operation)
            if not operation.confirmed and self._requires_confirmation(operation):
                await require_confirmation(
                    confirm, f"{description}\nOperation: {operation.operation_id}"
                )
                operation.confirmed = True
            operation.confirmation_source = "form" if operation.confirmed else "config"
            await self.authorize(operation)
            await validate(operation)
            if not operation.confirmed and self._requires_confirmation(operation):
                raise ToolError(
                    "CONFIRMATION_REQUIRED", "Confirmation policy changed before dispatch."
                )
            if await self.context.backend.account_id() != operation.account_id:
                raise ToolError("ACCOUNT_CHANGED", "Account changed while confirming this action.")
            operation.dispatched = True
            await self._save(operation)
            operation.receipt = await dispatch(operation)
        except ConfirmationPending:
            await self._save(operation)
            raise
        except ToolError as exc:
            self._failure(operation, exc, was_dispatched)
            if exc.code == "RATE_LIMITED" and exc.retry_after:
                await self.context.store.set_cooldown(
                    operation.account_id,
                    exc.scope or "account",
                    self.context.clock.utc_now() + timedelta(seconds=exc.retry_after),
                )
        except BaseException:
            self._failure(
                operation,
                ToolError(
                    "OPERATION_INTERRUPTED", "The operation was interrupted; inspect its status."
                ),
                was_dispatched,
            )
            await asyncio.shield(self._save(operation))
            raise
        if operation.receipt.status in ("sent", "completed", "failed"):
            operation.payload = {}
        await self._save(operation)
        return operation.receipt

    def _requires_confirmation(self, operation: OutboundOperation) -> bool:
        grant = operation.recipient.grant if operation.recipient else "allowlist"
        return self.context.policies().requires_confirmation(operation.kind, grant)

    def _failure(
        self, operation: OutboundOperation, error: ToolError, was_dispatched: bool
    ) -> None:
        uncertain = operation.dispatched and error.code in (
            "CONNECTION_ERROR",
            "TELEGRAM_UNAVAILABLE",
            "OPERATION_INTERRUPTED",
            "SEND_UNCERTAIN",
            "CALLBACK_UNCERTAIN",
        )
        if uncertain or was_dispatched:
            operation.receipt.status = "unknown"
        elif error.code == "RATE_LIMITED":
            operation.receipt.status = "pending"
            operation.dispatched = False
        else:
            operation.receipt.status = "failed"
        operation.receipt.error = error.issue(chat_id=operation.chat_id)
        if error.retry_after and isinstance(operation.receipt, SendReceipt):
            operation.receipt.retry_at = self.context.clock.utc_now() + timedelta(
                seconds=error.retry_after
            )
