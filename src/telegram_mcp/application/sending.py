"""One locally permitted message or immutable file, bound to a durable key and peer."""

from telegram_mcp.application.files import FileService
from telegram_mcp.application.outbound_access import OutboundContext, recipient_selector
from telegram_mcp.application.outbound_journal import OperationJournal
from telegram_mcp.application.outbound_ports import Confirm
from telegram_mcp.application.ports import MessagePort
from telegram_mcp.domain.models import MessageRef, ToolError
from telegram_mcp.domain.outbound import (
    OutboundOperation,
    RecipientBinding,
    RegisteredFile,
    SendReceipt,
    SendRequest,
    check_key,
    check_text,
)


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
        check_key(key)
        kind = "file" if file_id is not None else "message"
        capability = self.journal.capability(kind)
        account = await self.context.backend.account_id()
        existing = await self.context.store.operation(account, key)
        if existing is not None and existing.kind != kind:
            raise ToolError("IDEMPOTENCY_CONFLICT", "This key belongs to another operation kind.")
        binding = await self._recipient(chat_ref, capability, existing)
        reply = await self._reply(reply_to, existing)
        if reply is not None and reply.chat_id != binding.chat_id:
            raise ToolError("INVALID_ARGUMENT", "reply_to must refer to the selected recipient.")
        request = SendRequest(chat_ref=binding.chat_id, text=text, file_id=file_id, reply_to=reply)
        identity = request.model_dump(mode="json")
        replay = await self.journal.replay(account, binding.chat_id, kind, key, identity)
        if replay is not None:
            assert isinstance(replay, SendReceipt)
            return replay
        file, data = await self.files.read(account, file_id) if file_id else (None, None)

        async def validate(operation: OutboundOperation) -> None:
            nonlocal data
            if file is not None:
                expected = RegisteredFile.model_validate(operation.payload["file"])
                current, data = await self.files.read(account, file.file_id)
                if current != expected:
                    raise ToolError("FILE_UNAVAILABLE", "The confirmed file has changed.")
            if reply is not None:
                await self.context.require_read(reply.chat_id)
                message = await self.messages.message(reply)
                if message is None or message.omission_reason:
                    raise ToolError(
                        "MESSAGE_UNAVAILABLE", "The selected reply target is unavailable."
                    )

        async def dispatch(operation: OutboundOperation) -> SendReceipt:
            result = await self.context.backend.send(operation, request, data)
            result.file = RegisteredFile.model_validate(operation.payload["file"]) if file else None
            return result

        payload = request.model_dump(mode="json") | {
            "file": file.model_dump(mode="json") if file else None
        }
        result = await self.journal.run(
            account=account,
            chat=binding.chat_id,
            kind=kind,
            key=key,
            payload=payload,
            identity=identity,
            description=self._description,
            confirm=confirm,
            validate=validate,
            dispatch=dispatch,
            recipient=binding,
            reply_selector=reply_to,
            reply_ref=reply,
        )
        assert isinstance(result, SendReceipt)
        return result

    async def _recipient(
        self, ref: str, capability: str, operation: OutboundOperation | None
    ) -> RecipientBinding:
        if operation is None:
            return await self.context.bind(ref, capability)
        binding = operation.recipient
        if binding is None:
            # Legacy records retain their original permission requirement and digest.
            chat = await self.context.recipient(ref, capability, grant="allowlist")
            if chat.id != operation.chat_id:
                raise ToolError("IDEMPOTENCY_CONFLICT", "This key belongs to another recipient.")
            return RecipientBinding(
                chat_id=chat.id,
                title=chat.title,
                type=chat.type,
                username=chat.username,
                selector=recipient_selector(ref),
                grant="allowlist",
            )
        if recipient_selector(ref) not in (binding.selector, binding.chat_id):
            chat = await self.context.chats.resolve_chat(ref, refresh=True)
            if chat.id != binding.chat_id:
                raise ToolError("IDEMPOTENCY_CONFLICT", "This key belongs to another recipient.")
        if binding.grant == "allowlist":
            await self.context.revalidate(binding, capability)
        return binding

    async def _reply(
        self, ref: str | None, operation: OutboundOperation | None
    ) -> MessageRef | None:
        if ref is None:
            return None
        if operation is not None and operation.reply_ref is not None:
            if ref in (operation.reply_selector, operation.reply_ref.key):
                return operation.reply_ref
        return await self.messages.resolve_message(ref)

    @staticmethod
    def _description(operation: OutboundOperation) -> str:
        binding = operation.recipient
        recipient = (
            f"{binding.title!r} ({binding.chat_id})\n"
            f"Username: {binding.username!r}; type: {binding.type}\n"
            if binding
            else f"({operation.chat_id})\n"
        )
        scope = (
            "One-time permission for this send only; no recipient is added to configuration.\n"
            if binding and binding.grant == "one_off"
            else ""
        )
        file = operation.payload.get("file")
        file_text = RegisteredFile.model_validate(file).model_dump_json() if file else "none"
        reply = operation.payload.get("reply_to")
        reply_text = MessageRef.model_validate(reply).key if reply else "none"
        return (
            f"Send to {recipient}{scope}Text/caption:\n{operation.payload['text']}\n"
            f"File: {file_text}\nReply to: {reply_text}"
        )
