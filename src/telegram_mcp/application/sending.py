"""One confirmed plain-text message or immutable file, bound to a durable key."""

from telegram_mcp.application.files import FileService
from telegram_mcp.application.outbound_access import OutboundContext
from telegram_mcp.application.outbound_journal import OperationJournal
from telegram_mcp.application.outbound_ports import Confirm
from telegram_mcp.application.ports import MessagePort
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.outbound import OutboundOperation, SendReceipt, SendRequest, check_text


class SendService:
    def __init__(
        self,
        context: OutboundContext,
        files: FileService,
        journal: OperationJournal,
        messages: MessagePort,
    ):
        self.context, self.files, self.journal, self.messages = context, files, journal, messages

    async def send(
        self,
        chat_ref: str,
        text: str,
        key: str,
        file_id: str | None = None,
        reply_to: str | None = None,
        confirm: Confirm | None = None,
    ) -> SendReceipt:
        check_text(text, file=file_id is not None)
        kind = "file" if file_id is not None else "message"
        capability = self.journal.capability(kind)
        chat = await self.context.recipient(chat_ref, capability)
        account = await self.context.backend.account_id()
        reply = await self.messages.resolve_message(reply_to) if reply_to else None
        if reply is not None and reply.chat_id != chat.id:
            raise ToolError("INVALID_ARGUMENT", "reply_to must refer to the selected recipient.")
        request = SendRequest(chat_ref=chat.id, text=text, file_id=file_id, reply_to=reply)
        identity = request.model_dump(mode="json")
        replay = await self.journal.replay(account, chat.id, kind, key, identity)
        if replay is not None:
            assert isinstance(replay, SendReceipt)
            return replay
        file, data = await self.files.read(account, file_id) if file_id else (None, None)

        async def validate() -> None:
            nonlocal data
            await self.context.recipient(chat.id, capability)
            if file is not None:
                current, data = await self.files.read(account, file.file_id)
                if current.sha256 != file.sha256:
                    raise ToolError("FILE_UNAVAILABLE", "The confirmed file has changed.")
            if reply is not None:
                message = await self.messages.message(reply)
                if message is None or message.omission_reason:
                    raise ToolError(
                        "MESSAGE_UNAVAILABLE", "The selected reply target is unavailable."
                    )

        async def dispatch(operation: OutboundOperation) -> SendReceipt:
            result = await self.context.backend.send(operation, request, data)
            result.file = file
            return result

        await validate()
        payload = request.model_dump(mode="json") | {
            "file": file.model_dump(mode="json") if file else None
        }
        result = await self.journal.run(
            account=account,
            chat=chat.id,
            kind=kind,
            key=key,
            payload=payload,
            identity=identity,
            description=f"Send to {chat.title} ({chat.id})?\nText/caption:\n{text}\n"
            f"File: {file.model_dump_json() if file else 'none'}\n"
            f"Reply to: {reply.key if reply else 'none'}",
            confirm=confirm,
            validate=validate,
            dispatch=dispatch,
        )
        assert isinstance(result, SendReceipt)
        return result
