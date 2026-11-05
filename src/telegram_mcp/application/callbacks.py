"""Confirmed private-bot callbacks, with conservative handling of uncertain outcomes."""

from telegram_mcp.application.outbound_access import OutboundContext
from telegram_mcp.application.outbound_journal import OperationJournal
from telegram_mcp.application.outbound_ports import Confirm
from telegram_mcp.application.ports import MessagePort
from telegram_mcp.domain.models import MessageButton, MessageRef, ToolError
from telegram_mcp.domain.outbound import CallbackReceipt, OutboundOperation


class CallbackService:
    def __init__(self, context: OutboundContext, journal: OperationJournal, messages: MessagePort):
        self.context, self.journal, self.messages = context, journal, messages

    async def _button(self, ref: MessageRef, button_id: str, revision: str) -> MessageButton:
        message = await self.context.backend.callback_message(ref)
        if message.omission_reason:
            raise ToolError(
                "AUTH_NOTIFICATION_FILTERED", "Authentication notices cannot be acted on."
            )
        if message.revision != revision:
            raise ToolError("MESSAGE_CONFLICT", "The message or buttons changed; read them again.")
        for row in message.reply_markup.rows if message.reply_markup else []:
            for button in row:
                if button.button_id == button_id:
                    if button.type != "callback":
                        raise ToolError(
                            "BUTTON_UNSUPPORTED", "Only ordinary callback buttons are supported."
                        )
                    return button
        raise ToolError("BUTTON_UNAVAILABLE", "The selected button is no longer present.")

    async def invoke(
        self,
        message_ref: str,
        button_id: str,
        revision: str,
        key: str,
        confirm: Confirm | None = None,
    ) -> CallbackReceipt:
        self.context.enabled("allow_bot_callbacks")
        ref = await self.messages.resolve_message(message_ref)
        binding = await self.context.bind(ref.chat_id, "allow_bot_callbacks", bot=True)
        account = await self.context.backend.account_id()
        identity = {"ref": ref.key, "button_id": button_id, "revision": revision}
        replay = await self.journal.replay(account, binding.chat_id, "callback", key, identity)
        if replay is not None:
            assert isinstance(replay, CallbackReceipt)
            return replay
        button = await self._button(ref, button_id, revision)

        async def validate(operation: OutboundOperation) -> None:
            await self._button(ref, button_id, revision)

        async def dispatch(operation: OutboundOperation) -> CallbackReceipt:
            return await self.context.backend.callback(operation, ref, button_id, revision)

        result = await self.journal.run(
            account=account,
            chat=binding.chat_id,
            kind="callback",
            key=key,
            payload=identity,
            identity=identity,
            description=f"Invoke bot button {button.text!r} in {binding.title!r} "
            f"({binding.chat_id}), "
            f"message {ref.key}? The bot may change remote state.",
            confirm=confirm,
            validate=validate,
            dispatch=dispatch,
            recipient=binding,
        )
        assert isinstance(result, CallbackReceipt)
        return result
