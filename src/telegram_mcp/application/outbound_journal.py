"""Persist intent before dispatch and never guess whether a remote write succeeded."""

import asyncio
import math
import secrets
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Literal

from telegram_mcp.application.outbound_access import OutboundContext, require_confirmation
from telegram_mcp.application.outbound_ports import Confirm, ConfirmationPending
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.outbound import (
    CallbackReceipt,
    OutboundOperation,
    SendReceipt,
    check_key,
    request_digest,
)

Receipt = SendReceipt | CallbackReceipt


class OperationJournal:
    def __init__(self, context: OutboundContext):
        self.context = context
        self._lock = asyncio.Lock()
        self._active: set[str] = set()

    async def status(self, operation_id: str) -> Receipt:
        account = await self.context.backend.account_id()
        operation = await self.context.store.lookup_operation(account, operation_id)
        await self.context.recipient(
            operation.chat_id, self.capability(operation.kind), bot=operation.kind == "callback"
        )
        if (
            operation.receipt.status == "pending"
            and operation.dispatched
            and operation_id not in self._active
        ):
            return operation.receipt.model_copy(update={"status": "unknown"})
        return operation.receipt

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
        description: str,
        confirm: Confirm | None,
        validate: Callable[[], Awaitable[None]],
        dispatch: Callable[[OutboundOperation], Awaitable[Receipt]],
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
                )
            )
            replay = self._replay(operation, digest)
            if replay is not None:
                return replay
            self._active.add(operation.operation_id)
        try:
            return await self._execute(operation, description, confirm, validate, dispatch)
        finally:
            self._active.discard(operation.operation_id)

    async def _execute(
        self,
        operation: OutboundOperation,
        description: str,
        confirm: Confirm | None,
        validate: Callable[[], Awaitable[None]],
        dispatch: Callable[[OutboundOperation], Awaitable[Receipt]],
    ) -> Receipt:
        was_dispatched = operation.dispatched
        try:
            scope = "callback" if operation.kind == "callback" else "send"
            await self.context.check_cooldown(operation.account_id, f"{scope}:{operation.chat_id}")
            if not operation.confirmed:
                await require_confirmation(
                    confirm, f"{description}\nOperation: {operation.operation_id}"
                )
                operation.confirmed = True
            await validate()
            if await self.context.backend.account_id() != operation.account_id:
                raise ToolError("ACCOUNT_CHANGED", "Account changed while confirming this action.")
            operation.dispatched = True
            await self.context.store.save_operation(operation)
            operation.receipt = await dispatch(operation)
        except ConfirmationPending:
            await self.context.store.save_operation(operation)
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
            await asyncio.shield(self.context.store.save_operation(operation))
            raise
        if operation.receipt.status in ("sent", "completed", "failed"):
            operation.payload = {}
        await self.context.store.save_operation(operation)
        return operation.receipt

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
