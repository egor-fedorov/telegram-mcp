"""Outbound gateways share the application's single owned Telegram connection."""

from telegram_mcp.adapters.telegram.action_folders import action_folders
from telegram_mcp.adapters.telegram.callbacks import CallbackGateway
from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.drafts import DraftGateway
from telegram_mcp.adapters.telegram.message_actions import MessageActionGateway
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.adapters.telegram.sending import SendGateway
from telegram_mcp.application.outbound_ports import ActionSnapshot
from telegram_mcp.domain.models import Message, MessageRef, ToolError
from telegram_mcp.domain.outbound import (
    CallbackReceipt,
    Draft,
    OutboundOperation,
    SendReceipt,
    SendRequest,
)


class OutboundGateway:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.drafts = DraftGateway(runtime)
        self.sends = SendGateway(runtime)
        self.callbacks = CallbackGateway(runtime, chats)
        self.actions = MessageActionGateway(runtime, chats)

    async def action_folders(self, chat_id: str, account: str, wanted: set[int]) -> set[int]:
        return await action_folders(self.runtime, chat_id, account, wanted)

    async def action_target(self, ref: MessageRef, account: str) -> ActionSnapshot:
        return await self.actions.inspect(ref, account)

    async def account_id(self) -> str:
        client = await self.runtime.ensure()
        me = await self.runtime.call(client.get_me())
        if me is None or not me.id:
            raise ToolError("AUTH_REQUIRED", "No authorized Telegram account is available.")
        return str(me.id)

    async def draft(self, chat_id: str) -> Draft:
        return await self.drafts.get(chat_id)

    async def save_draft(self, chat_id: str, text: str) -> None:
        await self.drafts.save(chat_id, text)

    async def send(
        self, operation: OutboundOperation, request: SendRequest, data: bytes | None
    ) -> SendReceipt:
        return await self.sends.send(operation, request, data)

    async def callback_message(self, ref: MessageRef) -> Message:
        return await self.callbacks.message(ref)

    async def callback(
        self, operation: OutboundOperation, ref: MessageRef, button_id: str, revision: str
    ) -> CallbackReceipt:
        return await self.callbacks.invoke(operation, ref, button_id, revision)
