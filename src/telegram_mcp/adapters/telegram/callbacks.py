"""Bounded same-peer observations around a single ordinary bot callback."""

import asyncio
from typing import Any

from telethon import events, functions, types

from telegram_mcp.adapters.telegram.buttons import button_id as identify_button
from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import normalize_message, peer_id
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.domain.auth_notifications import authentication_notice
from telegram_mcp.domain.models import Message, MessageRef, ToolError
from telegram_mcp.domain.outbound import CallbackReceipt, OutboundOperation


class CallbackGateway:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime, self.chats = runtime, chats

    async def raw(self, ref: MessageRef) -> tuple[Any, Message]:
        client = await self.runtime.ensure()
        chat = await self.chats.resolve_chat(ref.chat_id, refresh=True)
        if chat.type != "bot":
            raise ToolError("BUTTON_UNSUPPORTED", "Only private bot dialogs support callbacks.")
        raw = await self.runtime.call(client.get_messages(int(ref.chat_id), ids=ref.message_id))
        if (
            not isinstance(raw, types.Message)
            or raw.id != ref.message_id
            or peer_id(raw.peer_id) != ref.chat_id
        ):
            raise ToolError("MESSAGE_UNAVAILABLE", "The button message is unavailable.")
        message = normalize_message(raw, chat)
        if message.omission_reason:
            raise ToolError(
                "AUTH_NOTIFICATION_FILTERED", "Authentication notices cannot be acted on."
            )
        return raw, message

    async def message(self, ref: MessageRef) -> Message:
        return (await self.raw(ref))[1]

    async def invoke(
        self, operation: OutboundOperation, ref: MessageRef, button_id: str, revision: str
    ) -> CallbackReceipt:
        client = await self.runtime.ensure()
        self.runtime.require_write_ready(f"callback:{ref.chat_id}")
        raw, message = await self.raw(ref)
        if message.revision != revision:
            raise ToolError("MESSAGE_CONFLICT", "The confirmed message changed before dispatch.")
        data = self._payload(raw, button_id)
        observed: list[MessageRef] = []

        async def observe(update: Any) -> None:
            if not isinstance(update, (types.UpdateNewMessage, types.UpdateEditMessage)):
                return
            candidate = update.message
            if peer_id(candidate.peer_id) == ref.chat_id and len(observed) < 20:
                normalized = normalize_message(candidate, message.chat)
                if not normalized.omission_reason and normalized.message_ref not in observed:
                    observed.append(normalized.message_ref)

        client.add_event_handler(observe, events.Raw())
        try:
            peer = await self.runtime.call(client.get_input_entity(int(ref.chat_id)))
            response = await self.runtime.write_call(
                client(
                    functions.messages.GetBotCallbackAnswerRequest(
                        peer=peer,
                        msg_id=ref.message_id,
                        data=data,
                    )
                ),
                scope=f"callback:{ref.chat_id}",
            )
            # This is an observation window, not an automatic retry or a claim of causality.
            await asyncio.sleep(5)
        finally:
            client.remove_event_handler(observe)
        filtered = authentication_notice(f"{response.message or ''} {response.url or ''}")
        return CallbackReceipt(
            operation_id=operation.operation_id,
            status="completed",
            message=None if filtered else response.message,
            alert=bool(response.alert),
            url=None if filtered else response.url,
            observed_message_refs=observed,
            error=ToolError(
                "AUTH_NOTIFICATION_FILTERED", "Authentication callback content was omitted."
            ).issue()
            if filtered
            else None,
        )

    @staticmethod
    def _payload(raw: Any, selected: str) -> bytes:
        if not isinstance(raw.reply_markup, types.ReplyInlineMarkup):
            raise ToolError("BUTTON_UNSUPPORTED", "Only inline callback buttons are supported.")
        for ri, row in enumerate(raw.reply_markup.rows):
            for ci, button in enumerate(row.buttons):
                if identify_button(button, ri, ci) != selected:
                    continue
                kind = button.type
                if not isinstance(kind, types.InlineButtonTypeCallback) or kind.requires_password:
                    raise ToolError(
                        "BUTTON_UNSUPPORTED",
                        "This button requires an unsupported action or password.",
                    )
                return kind.data
        raise ToolError("BUTTON_UNAVAILABLE", "The selected button is unavailable.")
