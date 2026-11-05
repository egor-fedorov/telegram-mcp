"""Narrow ports for outbound storage, Telegram writes and client confirmation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from telegram_mcp.domain.models import Message, MessageRef
from telegram_mcp.domain.outbound import (
    CallbackReceipt,
    Draft,
    JournalState,
    OperationKind,
    OutboundOperation,
    RegisteredFile,
    SendReceipt,
    SendRequest,
)

Confirm = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class OperationFilter:
    chat_id: str | None = None
    kind: OperationKind | None = None
    status: JournalState | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


@dataclass
class OperationBatch:
    operations: list[OutboundOperation]
    before: int | None


class ConfirmationPending(Exception):
    """A transport requested a further client round; no remote action has run."""


class OutboundPort(Protocol):
    async def account_id(self) -> str: ...
    async def draft(self, chat_id: str) -> Draft: ...
    async def save_draft(self, chat_id: str, text: str) -> None: ...
    async def send(
        self, operation: OutboundOperation, request: SendRequest, data: bytes | None
    ) -> SendReceipt: ...
    async def callback_message(self, ref: MessageRef) -> Message: ...
    async def callback(
        self, operation: OutboundOperation, ref: MessageRef, button_id: str, revision: str
    ) -> CallbackReceipt: ...


class OutboundStorePort(Protocol):
    async def register(
        self,
        account: str,
        name: str,
        mime: str,
        data: bytes,
        now: datetime,
        attachment_id: str | None = None,
        source_identity: str | None = None,
    ) -> RegisteredFile: ...
    async def import_local(self, account: str, path: str, now: datetime) -> RegisteredFile: ...
    async def files(self, account: str) -> list[RegisteredFile]: ...
    async def file(self, account: str, file_id: str) -> tuple[RegisteredFile, bytes]: ...
    async def operation(self, account: str, key: str) -> OutboundOperation | None: ...
    async def lookup_operation(self, account: str, operation_id: str) -> OutboundOperation: ...
    async def save_operation(self, operation: OutboundOperation) -> None: ...
    async def create_operation(self, operation: OutboundOperation) -> OutboundOperation: ...
    async def operations(
        self,
        account: str,
        filters: OperationFilter,
        *,
        before: int | None,
        limit: int,
        active_ids: tuple[str, ...],
    ) -> OperationBatch: ...
    async def owned_draft(self, account: str, chat: str) -> Draft | None: ...
    async def save_draft(self, account: str, draft: Draft, previous: Draft) -> None: ...
    async def close(self) -> None: ...
    async def cooldowns(self, account: str, now: datetime) -> dict[str, datetime]: ...
    async def set_cooldown(self, account: str, scope: str, until: datetime) -> None: ...
