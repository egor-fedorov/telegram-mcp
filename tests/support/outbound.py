"""Offline native-write substitute and an isolated real outbound database."""

import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.outbound_store import OutboundStore
from telegram_mcp.application.service import Service
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Chat, Message, MessageRef, ToolError
from telegram_mcp.domain.outbound import (
    CallbackReceipt,
    Draft,
    OutboundOperation,
    SendReceipt,
    SendRequest,
)
from tests.support.fakes import BASE, FakeBackend, FakeClock


class FakeOutbound:
    def __init__(self):
        self.account = "101"
        self.drafts: dict[str, Draft] = {}
        self.sent: dict[int, SendReceipt] = {}
        self.send_calls: list[tuple[int, SendRequest, bytes | None]] = []
        self.send_error: ToolError | None = None
        self.callback_error: ToolError | None = None
        self.callback_calls = 0
        self.button_message: Message | None = None

    async def account_id(self) -> str:
        return self.account

    async def draft(self, chat_id: str) -> Draft:
        return self.drafts.get(chat_id, Draft(chat_id=chat_id, revision="empty")).model_copy(
            deep=True
        )

    async def save_draft(self, chat_id: str, text: str) -> None:
        self.drafts[chat_id] = Draft(
            chat_id=chat_id,
            text=text,
            file=None,
            revision=hashlib.sha256(text.encode()).hexdigest(),
            exists=bool(text),
        )

    async def send(
        self, operation: OutboundOperation, request: SendRequest, data: bytes | None
    ) -> SendReceipt:
        self.send_calls.append((operation.random_id, request, data))
        if self.send_error:
            raise self.send_error
        if operation.random_id not in self.sent:
            self.sent[operation.random_id] = SendReceipt(
                operation_id=operation.operation_id,
                status="sent",
                chat_id=operation.chat_id,
                message_ref=MessageRef(chat_id=operation.chat_id, message_id=len(self.sent) + 10),
                sent_at=BASE,
            )
        return self.sent[operation.random_id].model_copy(deep=True)

    async def callback_message(self, ref: MessageRef) -> Message:
        assert self.button_message is not None
        return self.button_message

    async def callback(
        self, operation: OutboundOperation, ref: MessageRef, button_id: str, revision: str
    ) -> CallbackReceipt:
        self.callback_calls += 1
        if self.callback_error:
            raise self.callback_error
        return CallbackReceipt(
            operation_id=operation.operation_id, status="completed", message="Done"
        )


@dataclass
class OutboundFixture:
    service: Service
    store: OutboundStore
    telegram: FakeOutbound
    backend: FakeBackend
    settings: Settings
    clock: FakeClock


def outbound_service(path, *, telegram=None, settings=None, clock=None) -> OutboundFixture:
    settings = (
        settings
        if settings is not None
        else Settings(
            allowed_chat_ids=["1", "2"],
            allowed_write_chat_ids=["1", "2"],
            allow_drafts=True,
            allow_send_messages=True,
            allow_send_files=True,
            allow_bot_callbacks=True,
            allowed_upload_directories=[path],
        )
    )
    settings.session_path = path / "account.session"
    clock = clock or FakeClock()
    telegram = telegram or FakeOutbound()
    backend = FakeBackend(
        chats=[
            Chat(id="1", title="Recipient", type="private"),
            Chat(id="2", title="Bot", type="bot"),
        ]
    )
    store = OutboundStore(settings.session_path, tuple(settings.allowed_upload_directories))
    service = build_service(
        backend, settings, clock=clock, outbound_backend=telegram, outbound_store=store
    )
    return OutboundFixture(service, store, telegram, backend, settings, clock)


@asynccontextmanager
async def outbound_fixture(path, **kwargs):
    env = outbound_service(path, **kwargs)
    try:
        yield env
    except Exception as exc:
        # These fixtures contain only synthetic data. Preserve the underlying
        # OS/SQLite failure in CI while production errors remain sanitized.
        cause = exc
        while cause is not None:
            cause.__suppress_context__ = False
            cause = cause.__context__
        raise
    finally:
        await env.service.close()


async def approve(description: str) -> bool:
    return True
