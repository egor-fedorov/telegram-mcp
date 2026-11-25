"""Read and bind the actual Telegram peer before building a single-message RPC."""

from typing import Any

from telethon import functions, types, utils

from telegram_mcp.adapters.telegram.action_rights import (
    check_ordinary,
    native_actions,
    target_class,
)
from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import chat_from_entity, normalize_message, peer_id
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.outbound_ports import ActionSnapshot
from telegram_mcp.domain.message_actions import Action
from telegram_mcp.domain.models import MessageRef, ToolError
from telegram_mcp.domain.policy import SERVICE_NOTIFICATIONS_ID
from telegram_mcp.domain.references import is_canonical_chat_id


class MessageActionGateway:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime, self.chats = runtime, chats

    async def inspect(self, ref: MessageRef, account: str) -> ActionSnapshot:
        if not is_canonical_chat_id(ref.chat_id) or ref.chat_id == SERVICE_NOTIFICATIONS_ID:
            raise ToolError("ACCESS_DENIED", "Service notifications are inaccessible.")
        client = await self.runtime.ensure()
        entity = await self.chats.resolve_entity(ref.chat_id)
        if peer_id(entity) != ref.chat_id:
            raise ToolError("MESSAGE_UNAVAILABLE", "Telegram returned a different peer.")
        raw = await self.runtime.call(client.get_messages(entity, ids=ref.message_id))
        # Ordinary private/basic-group IDs have account-wide scope. Even get_messages
        # with an explicit peer can return another dialog's message for this ID.
        if (
            not isinstance(raw, types.Message)
            or raw.id != ref.message_id
            or peer_id(raw.peer_id) != ref.chat_id
        ):
            raise ToolError("MESSAGE_UNAVAILABLE", "The exact ordinary message is unavailable.")
        now = self.runtime.clock.utc_now()
        chat = chat_from_entity(entity, now=now)
        check_ordinary(entity, raw, chat)
        if chat.is_supergroup:
            # The sender of the discussion copy is the linked channel even when
            # that channel itself forwarded a post from a different original author.
            saved_peer = getattr(raw.fwd_from, "saved_from_peer", None)
            channel_senders = {
                peer_id(p) for p in (raw.from_id, saved_peer) if isinstance(p, types.PeerChannel)
            }
            if channel_senders:
                details = await self.chats.chat_details(chat)
                if details["linked_chat_id"] in channel_senders:
                    raise ToolError(
                        "ACTION_UNSUPPORTED", "Automatic linked discussion copies are excluded."
                    )
        message = normalize_message(raw, chat)
        if message.omission_reason:
            raise ToolError(
                "AUTH_NOTIFICATION_FILTERED", "Authentication notices cannot be acted on."
            )
        target = target_class(raw, chat, account)
        config = await self.runtime.call(client(functions.help.GetConfigRequest()))
        supported = native_actions(entity, raw, chat, target, config, now)
        peer = utils.get_input_peer(entity, allow_self=False)

        async def dispatch(action: Action, text: str | None) -> None:
            if action not in supported:
                raise ToolError("ACTION_NOT_ALLOWED", "Telegram does not permit this action.")
            await self._apply(client, peer, ref, action, text)

        return ActionSnapshot(
            message,
            target,
            supported,
            isinstance(raw.media, (types.MessageMediaPhoto, types.MessageMediaDocument)),
            dispatch,
        )

    async def _apply(
        self, client: Any, peer: Any, ref: MessageRef, action: Action, text: str | None
    ) -> None:
        if peer_id(peer) != ref.chat_id:
            raise ToolError(
                "MESSAGE_UNAVAILABLE", "The dispatch peer no longer matches the target."
            )
        request: Any
        if action == "edit":
            request = functions.messages.EditMessageRequest(
                peer=peer, id=ref.message_id, message=text, no_webpage=True
            )
        elif isinstance(peer, types.InputPeerChannel):
            if action != "delete_for_everyone":
                raise ToolError("ACTION_UNSUPPORTED", "Channel deletions are visible to everyone.")
            request = functions.channels.DeleteMessagesRequest(
                channel=utils.get_input_channel(peer), id=[ref.message_id]
            )
        elif isinstance(peer, (types.InputPeerChat, types.InputPeerUser)):
            request = functions.messages.DeleteMessagesRequest(
                id=[ref.message_id], revoke=action == "delete_for_everyone"
            )
        else:
            raise ToolError("ACTION_UNSUPPORTED", "Unsupported Telegram peer.")
        result = await self.runtime.write_call(
            client(request), scope=f"message_action:{ref.chat_id}"
        )
        accepted = (
            isinstance(result, (types.Updates, types.UpdatesCombined, types.UpdateShort))
            if action == "edit"
            else isinstance(result, types.messages.AffectedMessages)
        )
        if not accepted:
            raise ToolError(
                "ACTION_UNCERTAIN", "Telegram did not return a recognized acknowledgement."
            )
