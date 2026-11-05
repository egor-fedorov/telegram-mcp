"""Explicit MTProto random IDs and exact receipt mapping without private Telethon APIs."""

from typing import Any

from telethon import functions, types

from telegram_mcp.adapters.telegram.mapping import peer_id, utc
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.domain.models import MessageRef, ToolError
from telegram_mcp.domain.outbound import OutboundOperation, RegisteredFile, SendReceipt, SendRequest


def send_receipt(response: Any, operation: OutboundOperation) -> SendReceipt:
    if isinstance(response, types.UpdateShortSentMessage):
        return SendReceipt(
            operation_id=operation.operation_id,
            status="sent",
            chat_id=operation.chat_id,
            message_ref=MessageRef(chat_id=operation.chat_id, message_id=response.id),
            sent_at=utc(response.date) if response.date else None,
        )
    updates = getattr(response, "updates", [])
    ids = {
        update.id
        for update in updates
        if isinstance(update, types.UpdateMessageID) and update.random_id == operation.random_id
    }
    if len(ids) != 1:
        raise ToolError(
            "SEND_UNCERTAIN",
            "Telegram did not return an exact random_id receipt; retry this same key.",
        )
    message_id = ids.pop()
    raw = next(
        (
            update.message
            for update in updates
            if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage))
            and update.message.id == message_id
            and peer_id(update.message.peer_id) == operation.chat_id
        ),
        None,
    )
    return SendReceipt(
        operation_id=operation.operation_id,
        status="sent",
        chat_id=operation.chat_id,
        message_ref=MessageRef(chat_id=operation.chat_id, message_id=message_id),
        sent_at=utc(raw.date) if raw is not None else None,
    )


async def upload_document(
    runtime: TelegramRuntime, peer: Any, file: RegisteredFile, data: bytes
) -> types.InputMediaDocument:
    client = await runtime.ensure()
    uploaded = await runtime.call(client.upload_file(data, file_name=file.name))
    result = await runtime.write_call(
        client(
            functions.messages.UploadMediaRequest(
                peer=peer,
                media=types.InputMediaUploadedDocument(
                    file=uploaded,
                    mime_type=file.mime_type,
                    attributes=[types.DocumentAttributeFilename(file_name=file.name)],
                    force_file=True,
                ),
            )
        ),
        scope="uploads",
    )
    if not isinstance(result, types.MessageMediaDocument) or not isinstance(
        result.document, types.Document
    ):
        raise ToolError("FILE_UNAVAILABLE", "Telegram did not return the uploaded document.")
    document = result.document
    return types.InputMediaDocument(
        types.InputDocument(document.id, document.access_hash, document.file_reference)
    )


class SendGateway:
    def __init__(self, runtime: TelegramRuntime):
        self.runtime = runtime

    async def send(
        self, operation: OutboundOperation, request: SendRequest, data: bytes | None
    ) -> SendReceipt:
        client = await self.runtime.ensure()
        self.runtime.require_write_ready(f"send:{operation.chat_id}")
        peer = await self.runtime.call(client.get_input_entity(int(operation.chat_id)))
        reply = types.InputReplyToMessage(request.reply_to.message_id) if request.reply_to else None
        if request.file_id is not None:
            file = RegisteredFile.model_validate(operation.payload["file"])
            if data is None:
                raise ToolError("FILE_UNAVAILABLE", "Registered file bytes are unavailable.")
            media = await upload_document(self.runtime, peer, file, data)
            rpc = functions.messages.SendMediaRequest(
                peer=peer,
                media=media,
                message=request.text,
                random_id=operation.random_id,
                reply_to=reply,
                clear_draft=False,
                allow_paid_floodskip=False,
                allow_paid_stars=0,
            )
        else:
            rpc = functions.messages.SendMessageRequest(
                peer=peer,
                message=request.text,
                random_id=operation.random_id,
                reply_to=reply,
                no_webpage=True,
                clear_draft=False,
                allow_paid_floodskip=False,
                allow_paid_stars=0,
            )
        response = await self.runtime.write_call(client(rpc), scope=f"send:{operation.chat_id}")
        return send_receipt(response, operation)
